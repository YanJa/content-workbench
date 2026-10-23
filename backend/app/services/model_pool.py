"""模型池：解析（功能槽 → 具体模型）+ 额度 + 熔断 + 降级。

--------------------------------------------------------------------------
这个模块要回答的唯一问题
--------------------------------------------------------------------------
「这次调用该用哪个模型？」—— 以前答案是 `MODEL_ROUTES[task].model`
（写死在代码里的两个 DeepSeek 模型名，或者 CF 的一个字符串）。现在答案是
「查功能槽的绑定，按健康度挑第一个能用的」。

--------------------------------------------------------------------------
三条硬约束（违反任何一条都会把系统改得更差）
--------------------------------------------------------------------------
1. **resolve 必须是纯内存、无 IO、不抛异常。**
   改造前 `get_llm()` 是纯内存单例，`stream_call` 一次都不碰数据库。如果每次
   对话开头去查 4 次库，SQLite 一抖动整个对话就不可用 —— 比改造前更差。
   所以配置一次性读进内存快照（TTL 60s），额度读内存计数器，解析全程零 IO。

2. **解析不出结果就返回 None，调用方回落环境变量通道。**
   池是空的、DB 挂了、模型全熔断 —— 都要退回改造前的行为，绝不能让
   「加了模型池」变成「模型池坏了就全线瘫痪」。

3. **额度判定以「调用次数」为主口径，token 只作参考。**
   Cloudflare 流式拿不到 usage（llm.py 已注明，且有测试固化），token 额度在
   免费模型上永远触发不了 —— 而免费模型恰恰是要管的对象。生图也只能按张算。
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.logging import logger
from app.core.pricing import CST
from app.db.session import SessionLocal
from app.models.model_pool import (
    ModelBinding,
    ModelCatalog,
    ModelProvider,
    ModelProviderKey,
    ModelQuotaUsage,
)

# ---------------------------------------------------------------- 常量

SYSTEM_OWNER = "system"

#: 8 个功能槽。key 是槽位 id，value 是展示名。
FEATURES: dict[str, str] = {
    "chat": "AI 对话",
    "summary": "会话摘要压缩",
    "layout": "排版生成与回修",
    "layout_tags": "话题标签",
    "xhs_format": "小红书文本规整",
    "image_prompt_translate": "生图提示词翻译",
    "image": "文生图",
    "embedding": "知识库向量化",
}

#: 槽位 → 默认任务档位（模型条目没配 temperature/max_tokens 时用它兜底）
FEATURE_TASK: dict[str, str] = {
    "chat": "copywriting",
    "summary": "lightweight",
    "layout": "reasoning",
    "layout_tags": "lightweight",
    "xhs_format": "lightweight",
    "image_prompt_translate": "lightweight",
    "image": "lightweight",
    "embedding": "lightweight",
}

#: 换 embedding 模型不是「降级」，是「已有向量全部失效」。这个槽不参与自动切换。
NO_FALLBACK_FEATURES = frozenset({"embedding"})

_POOL_TTL = 60  # 快照有效期（秒）
_WARN_RATIO = 0.8  # 额度用到这个比例就预警
_CIRCUIT_BREAK_SEC = 300  # 调用失败后该模型停用的秒数

UTC = timezone.utc


@dataclass
class ModelSpec:
    """一次调用要用哪个模型、怎么调它。

    temperature / max_tokens 为 None 表示「沿用 MODEL_ROUTES[task] 里那些调过的值」
    —— 池里新加的模型没配参数时，不该让它退化成 OpenAI SDK 的默认值。
    """

    model: str = ""
    provider_key: str = "deepseek"
    protocol: str = "openai_chat"
    base_url: str = ""
    api_key: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    supports_tools: bool = True
    supports_stream_usage: bool = True
    capabilities: tuple[str, ...] = ("chat",)
    catalog_id: str = ""
    label: str = ""
    cred_owner: str = SYSTEM_OWNER
    quota_scope: str = "model"

    @property
    def display(self) -> str:
        return self.label or self.model


# ---------------------------------------------------------------- 快照

@dataclass
class PoolSnapshot:
    """一次读进内存的全套池配置 + 额度计数。

    存的是 dict 不是 ORM 对象：session 一关，ORM 对象上的懒加载属性就废了，
    而解析发生在任意时刻（可能在 session 之外）。
    """

    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (bucket, cred_owner, period, period_key) -> {"calls","tokens","images"}
    quota: dict[tuple[str, str, str, str], dict[str, int]] = field(default_factory=dict)
    at: float = 0.0


_snapshot: PoolSnapshot | None = None
#: RPM 滑动窗口：(bucket, cred_owner) -> 最近 60s 的调用时间戳
_rpm_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
#: 预警去重：(bucket, cred_owner, period, period_key) -> 已发过的级别
_warned: dict[tuple[str, str, str, str], int] = {}


def _now() -> int:
    return int(time.time())


def _period_key(period: str, tz: str = "cst", now: int | None = None) -> str:
    """周期键。CF 的免费额度在 00:00 UTC 重置，国内厂商按北京时间。"""
    zone = UTC if (tz or "cst").lower() == "utc" else CST
    d = datetime.fromtimestamp(now if now is not None else _now(), zone)
    return d.strftime("%Y-%m-%d") if period == "day" else d.strftime("%Y-%m")


def _row_dict(obj: Any, cols: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in cols:
        out[c] = getattr(obj, c, None)
    return out


_PROVIDER_COLS = (
    "id", "owner", "key", "name", "kind", "base_url", "api_key", "enabled",
    "daily_calls_limit", "daily_token_limit", "monthly_token_limit", "rpm_limit",
    "quota_reset_tz",
)
_CATALOG_COLS = (
    "id", "provider_key", "model_id", "label", "capabilities", "protocol",
    "context_window", "max_tokens", "temperature", "supports_tools",
    "supports_stream_usage", "dimensions", "billing", "price_in", "price_out",
    "quota_scope", "daily_calls_limit", "daily_token_limit", "monthly_token_limit",
    "rpm_limit", "enabled", "last_error", "disabled_until", "sort",
)


async def refresh_snapshot(db: Any = None) -> PoolSnapshot:
    """把池配置与额度计数一次性读进内存。失败时保留旧快照（宁可旧，不可无）。"""
    global _snapshot

    async def _load(session: Any) -> PoolSnapshot:
        providers = {
            r.key: _row_dict(r, _PROVIDER_COLS)
            for r in (
                await session.execute(select(ModelProvider).where(ModelProvider.enabled.is_(True)))
            ).scalars()
        }
        catalog = {
            r.id: _row_dict(r, _CATALOG_COLS)
            for r in (
                await session.execute(select(ModelCatalog).where(ModelCatalog.enabled.is_(True)))
            ).scalars()
        }
        bindings = {
            r.feature: {"primary": r.primary_model_id, "fallbacks": list(r.fallbacks or [])}
            for r in (await session.execute(select(ModelBinding))).scalars()
        }
        # 额度只加载「当前日 + 当前月」两个周期 —— 历史桶对判定没有意义
        keys: set[str] = set()
        for tz in ("cst", "utc"):
            keys.add(_period_key("day", tz))
            keys.add(_period_key("month", tz))
        rows = (
            await session.execute(
                select(ModelQuotaUsage).where(ModelQuotaUsage.period_key.in_(list(keys)))
            )
        ).scalars()
        quota: dict[tuple[str, str, str, str], dict[str, int]] = {}
        for r in rows:
            quota[(r.bucket, r.cred_owner, r.period, r.period_key)] = {
                "calls": int(r.calls or 0),
                "tokens": int(r.tokens or 0),
                "images": int(r.images or 0),
            }
        return PoolSnapshot(providers, catalog, bindings, quota, at=time.time())

    try:
        if db is not None:
            snap = await _load(db)
        else:
            async with SessionLocal() as session:
                snap = await _load(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型池快照刷新失败，沿用旧快照: %s", exc)
        return _snapshot or PoolSnapshot()

    _snapshot = snap
    return snap


def snapshot() -> PoolSnapshot:
    """取当前快照（可能是空的，绝不是 None —— 调用方不用判空）。"""
    return _snapshot or PoolSnapshot()


def invalidate() -> None:
    """让快照立即失效（改了配置后调一次，下次解析会重读）。"""
    global _snapshot
    _snapshot = None


async def ensure_fresh() -> None:
    """TTL 到期就刷新一次。放在 lifespan 的后台循环里周期性调用。"""
    snap = _snapshot
    if snap is None or time.time() - snap.at > _POOL_TTL:
        await refresh_snapshot()


# ---------------------------------------------------------------- 额度判定

def _limit(primary: Any, secondary: Any) -> int | None:
    """取两个限额里更严格的那个。None 表示不限，0/负数一律当不限（fail-open）。"""
    vals = [v for v in (primary, secondary) if isinstance(v, int) and v > 0]
    return min(vals) if vals else None


def _used(snap: PoolSnapshot, bucket: str, cred_owner: str, period: str, period_key: str, metric: str) -> int:
    return int(snap.quota.get((bucket, cred_owner, period, period_key), {}).get(metric, 0) or 0)


def _quota_ok(
    entry: dict[str, Any],
    prov: dict[str, Any],
    cred_owner: str,
    snap: PoolSnapshot,
    now: int,
) -> bool:
    """这个候选的额度还够吗。model 桶与 provider 桶任一耗尽就跳过。"""
    tz = prov.get("quota_reset_tz") or "cst"
    day_key = _period_key("day", tz, now)
    month_key = _period_key("month", tz, now)

    # 有两个桶要查：账号级（provider_key）与模型级（model_id）。
    # quota_scope="provider" 时两个桶是同一个，查一次就够 —— 这正是 CF
    # 「10k Neurons/天、跨模型共享」的口径。
    model_bucket = entry.get("model_id") or ""
    buckets: list[tuple[str, dict[str, Any]]] = [(prov.get("key") or "", prov)]
    if (entry.get("quota_scope") or "model") == "model":
        buckets.append((model_bucket, entry))

    for bucket, conf in buckets:
        for period, pkey in (("day", day_key), ("month", month_key)):
            calls_limit = _limit(
                conf.get("daily_calls_limit") if period == "day" else None, None
            )
            token_limit = (
                conf.get("daily_token_limit") if period == "day"
                else conf.get("monthly_token_limit")
            )
            token_limit = token_limit if isinstance(token_limit, int) and token_limit > 0 else None
            if calls_limit is None and token_limit is None:
                continue
            if calls_limit is not None and _used(snap, bucket, cred_owner, period, pkey, "calls") >= calls_limit:
                return False
            if token_limit is not None and _used(snap, bucket, cred_owner, period, pkey, "tokens") >= token_limit:
                return False
    return True


def _rpm_ok(entry: dict[str, Any], prov: dict[str, Any], cred_owner: str, now: int) -> bool:
    """每分钟请求数。免费档真正先撞的往往是 QPS 而不是总量。"""
    limit = _limit(prov.get("rpm_limit"), entry.get("rpm_limit"))
    if limit is None:
        return True
    key = (entry.get("model_id") or "", cred_owner)
    hits = _rpm_hits.get(key)
    if not hits:
        return True
    while hits and now - hits[0] > 60:
        hits.popleft()
    return len(hits) < limit


def _record_rpm(entry: dict[str, Any], cred_owner: str) -> None:
    key = (entry.get("model_id") or "", cred_owner)
    _rpm_hits[key].append(time.time())


def quota_status(spec: ModelSpec) -> dict[str, Any]:
    """某个模型当前的额度状态（给 API / 页面展示用）。"""
    snap = snapshot()
    prov = snap.providers.get(spec.provider_key, {})
    entry = next(
        (e for e in snap.catalog.values() if e.get("model_id") == spec.model),
        snap.catalog.get(spec.catalog_id, {}),
    )
    tz = prov.get("quota_reset_tz") or "cst"
    now = _now()
    day_key = _period_key("day", tz, now)
    month_key = _period_key("month", tz, now)
    bucket = prov.get("key") or ""
    if (entry.get("quota_scope") or "model") == "model":
        bucket = entry.get("model_id") or bucket

    out: dict[str, Any] = {"bucket": bucket, "cred_owner": spec.cred_owner, "limits": {}, "used": {}}
    for period, pkey, metric, limit_key in (
        ("day", day_key, "calls", "daily_calls_limit"),
        ("day", day_key, "tokens", "daily_token_limit"),
        ("month", month_key, "tokens", "monthly_token_limit"),
    ):
        limit = entry.get(limit_key) if (entry.get("quota_scope") or "model") == "model" else None
        limit = _limit(prov.get(limit_key), limit)
        used = _used(snap, bucket, spec.cred_owner, period, pkey, metric)
        out["limits"][f"{period}_{metric}"] = limit
        out["used"][f"{period}_{metric}"] = used
    ratios = [
        out["used"].get(key, 0) / limit
        for key, limit in out["limits"].items()
        if isinstance(limit, int) and limit > 0
    ]
    out["ratio"] = round(max(ratios), 3) if ratios else 0.0
    return out


def take_warnings(spec: ModelSpec, feature: str) -> list[dict[str, Any]]:
    """跨阈值才返回一次预警（进程内去重），否则每次对话都弹等于没弹。"""
    st = quota_status(spec)
    ratio = st.get("ratio") or 0.0
    level = 2 if ratio >= 1.0 else (1 if ratio >= _WARN_RATIO else 0)
    if level == 0:
        return []
    key = (st.get("bucket", ""), spec.cred_owner, "day", _period_key("day"))
    if _warned.get(key, 0) >= level:
        return []
    _warned[key] = level
    return [
        {
            "type": "quota_warning",
            "level": "exhausted" if level == 2 else "warn",
            "feature": feature,
            "model": spec.model,
            "label": spec.display,
            "ratio": ratio,
            "used": st.get("used"),
            "limits": st.get("limits"),
            "message": (
                f"模型 {spec.display} 的今日额度已用尽，已自动切换到备用模型"
                if level == 2
                else f"模型 {spec.display} 的今日额度已用 {int(ratio * 100)}%"
            ),
        }
    ]


# ---------------------------------------------------------------- 解析

def _spec_of(entry: dict[str, Any], prov: dict[str, Any], api_key: str, cred_owner: str) -> ModelSpec:
    caps = entry.get("capabilities") or ["chat"]
    return ModelSpec(
        model=entry.get("model_id") or "",
        provider_key=prov.get("key") or "",
        protocol=entry.get("protocol") or "openai_chat",
        base_url=prov.get("base_url") or "",
        api_key=api_key,
        temperature=entry.get("temperature"),
        max_tokens=entry.get("max_tokens"),
        supports_tools=bool(entry.get("supports_tools", True)),
        supports_stream_usage=bool(entry.get("supports_stream_usage", True)),
        capabilities=tuple(caps),
        catalog_id=entry.get("id") or "",
        label=entry.get("label") or "",
        cred_owner=cred_owner,
        quota_scope=entry.get("quota_scope") or "model",
    )


def resolve_candidates(
    feature: str,
    owner: str | None = None,
    personal_keys: dict[str, str] | None = None,
) -> list[ModelSpec]:
    """功能槽 → 所有当前健康的候选（primary 在前，fallbacks 依次在后）。

    纯内存、无 IO、不抛异常。空列表 = 池里没有能用的。
    """
    try:
        snap = snapshot()
        binding = snap.bindings.get(feature)
        if not binding:
            return []
        now = _now()
        out: list[ModelSpec] = []
        for cid in [binding.get("primary")] + list(binding.get("fallbacks") or []):
            if not cid:
                continue
            entry = snap.catalog.get(cid)
            if not entry or not entry.get("enabled"):
                continue
            prov = snap.providers.get(entry.get("provider_key") or "")
            if not prov or not prov.get("enabled"):
                continue
            # 熔断：错过的模型停用 5 分钟，避免每次对话都先撞它一次
            if int(entry.get("disabled_until") or 0) > now:
                continue
            # 凭证：个人 Key 优先（烧自己的额度），否则用全局
            mine = (personal_keys or {}).get(prov.get("key") or "") or ""
            api_key = mine or (prov.get("api_key") or "")
            if not api_key:
                continue
            cred_owner = (owner or SYSTEM_OWNER) if mine else SYSTEM_OWNER
            if not _quota_ok(entry, prov, cred_owner, snap, now):
                continue
            if not _rpm_ok(entry, prov, cred_owner, now):
                continue
            out.append(_spec_of(entry, prov, api_key, cred_owner))
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型池解析失败(feature=%s)，回落环境变量通道: %s", feature, exc)
        return []


def resolve_model(
    feature: str,
    owner: str | None = None,
    personal_keys: dict[str, str] | None = None,
) -> ModelSpec | None:
    """功能槽 → 第一个健康的模型。**纯内存、无 IO、绝不抛异常。**

    返回 None 表示「池里没有可用的」—— 调用方应当回落环境变量通道。
    这是刻意的设计：模型池是增强，不是必经之路。
    """
    cands = resolve_candidates(feature, owner, personal_keys)
    if not cands:
        return None
    _record_rpm({"model_id": cands[0].model}, cands[0].cred_owner)
    return cands[0]


async def load_personal_keys(db: Any, owner: str | None) -> dict[str, str]:
    """取该用户自己填的 Key（覆盖全局凭证用）。查不到就是空 dict，不抛。"""
    if not owner or owner == SYSTEM_OWNER:
        return {}
    try:
        rows = (
            await db.execute(
                select(ModelProviderKey).where(
                    ModelProviderKey.owner == owner,
                    ModelProviderKey.enabled.is_(True),
                )
            )
        ).scalars().all()
        return {r.provider_key: (r.api_key or "") for r in rows if (r.api_key or "").strip()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取个人凭证失败，改用全局凭证: %s", exc)
        return {}


async def resolve_for(
    feature: str,
    owner: str | None = None,
    db: Any = None,
) -> ModelSpec | None:
    """带个人凭证的解析（请求级用这个）。没有 db 就只用全局凭证。"""
    keys: dict[str, str] = {}
    if db is not None and owner:
        keys = await load_personal_keys(db, owner)
    return resolve_model(feature, owner, keys)


# ---------------------------------------------------------------- 记账与熔断

def _mirror(bucket: str, cred_owner: str, period: str, period_key: str, calls: int, tokens: int, images: int) -> None:
    """把已写库的增量同步到内存快照，避免一分钟内重复读库。"""
    snap = _snapshot
    if snap is None:
        return
    slot = snap.quota.setdefault(
        (bucket, cred_owner, period, period_key), {"calls": 0, "tokens": 0, "images": 0}
    )
    slot["calls"] += calls
    slot["tokens"] += tokens
    slot["images"] += images


async def _bump_bucket(
    session: Any,
    bucket: str,
    cred_owner: str,
    period: str,
    period_key: str,
    calls: int,
    tokens: int,
    images: int,
) -> None:
    """计数器 +N。照 core/quota.py 的套路：先条件 UPDATE，没有再 INSERT。"""
    now = _now()
    res = await session.execute(
        update(ModelQuotaUsage)
        .where(
            ModelQuotaUsage.bucket == bucket,
            ModelQuotaUsage.cred_owner == cred_owner,
            ModelQuotaUsage.period == period,
            ModelQuotaUsage.period_key == period_key,
        )
        .values(
            calls=ModelQuotaUsage.calls + calls,
            tokens=ModelQuotaUsage.tokens + tokens,
            images=ModelQuotaUsage.images + images,
            updated_at=now,
        )
    )
    if res.rowcount >= 1:
        _mirror(bucket, cred_owner, period, period_key, calls, tokens, images)
        return

    exists = (
        await session.execute(
            select(ModelQuotaUsage).where(
                ModelQuotaUsage.bucket == bucket,
                ModelQuotaUsage.cred_owner == cred_owner,
                ModelQuotaUsage.period == period,
                ModelQuotaUsage.period_key == period_key,
            )
        )
    ).scalar_one_or_none()
    if exists:
        _mirror(bucket, cred_owner, period, period_key, calls, tokens, images)
        return

    session.add(
        ModelQuotaUsage(
            bucket=bucket,
            cred_owner=cred_owner,
            period=period,
            period_key=period_key,
            calls=calls,
            tokens=tokens,
            images=images,
            updated_at=now,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # 并发首插撞唯一约束 → 退回条件 UPDATE 再抢一次
        await session.rollback()
        await _bump_bucket(session, bucket, cred_owner, period, period_key, calls, tokens, images)
        return
    _mirror(bucket, cred_owner, period, period_key, calls, tokens, images)


async def bump_quota(
    spec: ModelSpec, tokens: int = 0, images: int = 0, session: Any = None
) -> None:
    """一次调用结束后累加额度计数。

    默认用独立 session **立即提交**：用量是账单，不能搭业务事务的车（否则端点
    报错回滚，这次调用就白烧了）。调用方也可以传自己的 session（测试用）。
    写失败只记日志，绝不影响对话。
    """
    try:
        snap = snapshot()
        prov = snap.providers.get(spec.provider_key, {})
        entry = snap.catalog.get(spec.catalog_id, {})
        tz = prov.get("quota_reset_tz") or "cst"

        async def _all(s: Any) -> None:
            for period, pkey in (
                ("day", _period_key("day", tz)),
                ("month", _period_key("month", tz)),
            ):
                await _bump_bucket(
                    s, prov.get("key") or "", spec.cred_owner, period, pkey,
                    1, tokens, images,
                )
                if (entry.get("quota_scope") or "model") == "model":
                    await _bump_bucket(
                        s, spec.model, spec.cred_owner, period, pkey,
                        1, tokens, images,
                    )
            await s.commit()

        if session is not None:
            await _all(session)
        else:
            async with SessionLocal() as s:
                await _all(s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("额度计数写入失败（不影响对话）: %s", exc)


async def mark_failure(spec: ModelSpec, error: str, db: Any = None) -> None:
    """调用失败 → 熔断该模型 5 分钟，并同步到内存快照。"""
    until = _now() + _CIRCUIT_BREAK_SEC
    if _snapshot is not None and spec.catalog_id in _snapshot.catalog:
        _snapshot.catalog[spec.catalog_id]["disabled_until"] = until
        _snapshot.catalog[spec.catalog_id]["last_error"] = (error or "")[:500]

    async def _write(session: Any) -> None:
        await session.execute(
            update(ModelCatalog)
            .where(ModelCatalog.id == spec.catalog_id)
            .values(last_error=(error or "")[:500], disabled_until=until, updated_at=_now())
        )
        await session.commit()

    try:
        if db is not None:
            await _write(db)
        else:
            async with SessionLocal() as session:
                await _write(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("熔断状态写入失败（只影响下次选路）: %s", exc)


async def mark_success(spec: ModelSpec, db: Any = None) -> None:
    """调用成功 → 清掉熔断标记。

    常态下这是零开销：只有「当前确实处于熔断」的条目才落一次库，
    没熔断的成功调用不碰数据库。
    """
    if _snapshot is not None:
        cur = _snapshot.catalog.get(spec.catalog_id)
        if cur is None or not int(cur.get("disabled_until") or 0):
            return
        cur["disabled_until"] = 0
        cur["last_error"] = ""
    if not spec.catalog_id:
        return

    async def _write(session: Any) -> None:
        await session.execute(
            update(ModelCatalog)
            .where(ModelCatalog.id == spec.catalog_id)
            .where(ModelCatalog.disabled_until > 0)
            .values(disabled_until=0, last_error="", updated_at=_now())
        )
        await session.commit()

    try:
        if db is not None:
            await _write(db)
        else:
            async with SessionLocal() as session:
                await _write(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("熔断清除失败（只影响下次选路）: %s", exc)


# ---------------------------------------------------------------- 播种与迁移

def _cf_base_url() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.CLOUDFLARE_ACCOUNT_ID}/ai/v1"
    )


async def seed_model_pool(db: Any) -> dict[str, int]:
    """首次启动时按 .env 播种服务商与模型，并建立默认绑定。

    幂等：只在「表里一个服务商都没有」时做。之后管理员在页面上怎么改，
    重启都不覆盖（与 ensure_admin_account 同款的自愈哲学）。
    """
    has = (await db.execute(select(ModelProvider.id).limit(1))).first()
    if has:
        return {"seeded": 0}

    now = _now()
    providers: list[ModelProvider] = []
    catalog: list[ModelCatalog] = []
    # (逻辑名, 行对象)。id 是 Python 端 default，flush 之后才有值 ——
    # 不能在建行的时候就把 .id 抄进字典，那时候还是 None。
    pending: list[tuple[str, ModelCatalog]] = []

    if (settings.DEEPSEEK_API_KEY or "").strip():
        providers.append(
            ModelProvider(
                owner=SYSTEM_OWNER, key="deepseek", name="DeepSeek 官方", kind="openai",
                base_url=settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com",
                api_key=settings.DEEPSEEK_API_KEY, enabled=True,
                note="由 .env 的 DEEPSEEK_* 自动播种", created_at=now, updated_at=now,
            )
        )
        for mid, label, caps in (
            (settings.DEEPSEEK_MODEL_FLASH, "DeepSeek Flash", ["chat"]),
            (settings.DEEPSEEK_MODEL_PRO, "DeepSeek V4 Pro", ["chat"]),
        ):
            if not mid:
                continue
            row = ModelCatalog(
                provider_key="deepseek", model_id=mid, label=label, capabilities=caps,
                protocol="openai_chat", billing="paid", supports_tools=True,
                supports_stream_usage=True, quota_scope="model", enabled=True,
                sort=10, note="由 .env 播种", created_at=now, updated_at=now,
            )
            catalog.append(row)
            pending.append((f"deepseek:{mid}", row))

    cf_ready = bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID)
    if cf_ready:
        providers.append(
            ModelProvider(
                owner=SYSTEM_OWNER, key="cloudflare", name="Cloudflare Workers AI",
                kind="cloudflare", base_url=_cf_base_url(),
                api_key=settings.CLOUDFLARE_API_TOKEN, enabled=True,
                # 官方：免费计划 10,000 Neurons/天，账号级、跨模型共享、00:00 UTC 重置
                quota_reset_tz="utc",
                note="由 .env 的 CLOUDFLARE_* 自动播种；免费额度账号级共享（10k Neurons/天）",
                created_at=now, updated_at=now,
            )
        )
        if settings.CLOUDFLARE_CHAT_MODEL:
            row = ModelCatalog(
                provider_key="cloudflare", model_id=settings.CLOUDFLARE_CHAT_MODEL,
                label="Cloudflare 对话（免费）", capabilities=["chat"], protocol="openai_chat",
                supports_tools=False, supports_stream_usage=False, max_tokens=4096,
                temperature=0.7, billing="free", quota_scope="provider", enabled=True,
                sort=20, note="CF 不支持 function calling，也不认 stream_options",
                created_at=now, updated_at=now,
            )
            catalog.append(row)
            pending.append(("cf:chat", row))
        if settings.CLOUDFLARE_IMAGE_MODEL:
            row = ModelCatalog(
                provider_key="cloudflare", model_id=settings.CLOUDFLARE_IMAGE_MODEL,
                label="Cloudflare 文生图（免费）", capabilities=["image"],
                protocol="cf_ai_run_image", supports_tools=False,
                supports_stream_usage=False, billing="free", quota_scope="provider",
                enabled=True, sort=21, note="CF REST 端点 /ai/run/{model}",
                created_at=now, updated_at=now,
            )
            catalog.append(row)
            pending.append(("cf:image", row))
        if settings.CLOUDFLARE_EMBED_MODEL:
            row = ModelCatalog(
                provider_key="cloudflare", model_id=settings.CLOUDFLARE_EMBED_MODEL,
                label="Cloudflare 向量化（免费）", capabilities=["embedding"],
                protocol="cf_ai_run_embed", supports_tools=False,
                supports_stream_usage=False, dimensions=1024,
                billing="free", quota_scope="provider", enabled=True, sort=22,
                note="bge-m3，1024 维", created_at=now, updated_at=now,
            )
            catalog.append(row)
            pending.append(("cf:embed", row))

    for p in providers:
        db.add(p)
    for c in catalog:
        db.add(c)
    await db.flush()
    ids: dict[str, str] = {name: row.id for name, row in pending}

    # 默认绑定：与改造前的行为一一对应（chat/summary/layout 走 DeepSeek，
    # 生图与向量化走 CF）。有 CF 对话模型且旧设置指向 CF 时，迁移会改写 chat 槽。
    flash = ids.get(f"deepseek:{settings.DEEPSEEK_MODEL_FLASH}", "")
    pro = ids.get(f"deepseek:{settings.DEEPSEEK_MODEL_PRO}", "") or flash
    default_map = {
        "chat": flash,
        "summary": flash,
        "layout": pro,
        "layout_tags": flash,
        "xhs_format": flash,
        "image_prompt_translate": flash,
        "image": ids.get("cf:image", ""),
        "embedding": ids.get("cf:embed", ""),
    }
    for feature, cid in default_map.items():
        if cid:
            db.add(
                ModelBinding(
                    feature=feature, primary_model_id=cid, fallbacks=[], updated_at=now
                )
            )
    await db.flush()
    logger.info("模型池已按 .env 播种：%d 个服务商、%d 个模型", len(providers), len(catalog))
    return {"seeded": len(providers) + len(catalog)}


async def migrate_legacy_chat_provider(db: Any) -> bool:
    """把老口径的 app_settings（chat_provider / cloudflare_chat_model）迁到绑定。

    **不删旧 key**：留着当回滚兜底 —— 万一模型池有问题，把绑定一清，
    老代码路径立刻重新生效。
    """
    from sqlalchemy import select as _select

    from app.models.settings import AppSetting

    rows = (
        await db.execute(
            _select(AppSetting).where(
                AppSetting.key.in_(("chat_provider", "cloudflare_chat_model"))
            )
        )
    ).scalars().all()
    kv = {r.key: (r.value or "") for r in rows}
    if kv.get("chat_provider") != "cloudflare" or not kv.get("cloudflare_chat_model"):
        return False

    model_id = kv["cloudflare_chat_model"]
    entry = (
        await db.execute(
            _select(ModelCatalog).where(ModelCatalog.model_id == model_id)
        )
    ).scalar_one_or_none()
    if entry is None:
        return False
    binding = (
        await db.execute(_select(ModelBinding).where(ModelBinding.feature == "chat"))
    ).scalar_one_or_none()
    if binding:
        binding.primary_model_id = entry.id
        binding.updated_at = _now()
    else:
        db.add(ModelBinding(feature="chat", primary_model_id=entry.id, fallbacks=[], updated_at=_now()))
    await db.flush()
    logger.info("已把旧的 Cloudflare 对话设置迁移到模型池绑定：%s", model_id)
    return True


async def loop_pool_refresh(interval: int = _POOL_TTL) -> None:
    """后台周期性刷新快照（照 zhihu.loop_hot_fetch 的范式）。

    为什么要有这个循环：resolve 是纯内存的，配置改了不会自动生效。60s
    的延迟对「改完模型池立刻想试」是可接受的；管理员在页面上改完会主动
    invalidate()，那一刻就生效了。
    """
    while True:
        try:
            await asyncio.sleep(interval)
            await ensure_fresh()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型池快照刷新循环异常: %s", exc)
            await asyncio.sleep(interval)


async def bootstrap_pool(db: Any) -> None:
    """启动时调用：建默认数据 → 迁移旧设置 → 读进内存快照。"""
    try:
        await seed_model_pool(db)
        await migrate_legacy_chat_provider(db)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型池初始化失败（将回落环境变量通道）: %s", exc)
        await db.rollback()
    await refresh_snapshot(db)
