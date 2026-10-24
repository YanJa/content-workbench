"""模型池管理路由（管理员维护，全员只读可见）。

--------------------------------------------------------------------------
权限口径
--------------------------------------------------------------------------
| 端点 | 权限 | 理由 |
|---|---|---|
| 所有 GET（providers / catalog / bindings / quota / features） | 登录 | 普通用户也该知道自己能用哪些模型；响应一律脱敏 |
| `PUT /my-key/{provider_key}` | 登录 | 用户填自己的 Key，只影响自己（`cred_owner`=自己） |
| 其余写操作 | `require_admin` | 动的是系统级凭证与功能策略 |

--------------------------------------------------------------------------
两处「必须拦住」的保存校验
--------------------------------------------------------------------------
1. **embedding 槽不允许有备选链。**
   换 embedding 模型不是「降级」，是「库里已有向量全部失效」——不同模型
   产出的向量不在同一个空间，混着比余弦得到的是噪声，而且**不报错**，
   表现为知识库突然什么都搜不到。所以这个槽只能有一个模型，换就要显式重建。

2. **备选链的能力必须与主模型一致。**
   chat 槽的主模型支持工具调用、备选不支持，那么降级发生的那一刻，
   Agent 的工具循环会在中途静默失效（不报错，只是再也叫不动工具）。
   宁可在保存时就拒掉，也不要等线上出怪事。

3. 换 embedding 模型且维度 / 模型名与现有向量不一致 → **409 EMBED_DIM_MISMATCH**
   带 `stale_chunks`，让前端弹「将失效 N 条，是否继续并立即重建？」，
   确认后带 `force=true` 重发。
"""
from __future__ import annotations

from typing import Any

import time
import uuid

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import current_user, require_admin
from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.models.model_pool import (
    ModelBinding,
    ModelCatalog,
    ModelProvider,
    ModelProviderKey,
)
from app.models.user import User
from app.services import model_pool as pool
from app.services import knowledge as kb

router = APIRouter(prefix="/model-pool", tags=["模型池"])

_PROTOCOLS = (
    "openai_chat", "openai_images", "openai_embeddings",
    "cf_ai_run_image", "cf_ai_run_embed",
)
_CAPABILITIES = ("chat", "vision", "image", "embedding")


# ---------------------------------------------------------------- 脱敏与输出

def _mask(key: str) -> str:
    """凭证脱敏：只留前 4 后 4。**接口响应里绝不能出现完整 Key。**

    与 WeChatAccount.secret 同款约定：明文存本机 SQLite 是可接受的，
    但任何一次 HTTP 响应把明文带出去，就等于把它写进了浏览器历史 / 日志。
    """
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "*" * max(0, len(k) - 2)
    return f"{k[:4]}{'*' * 8}{k[-4:]}"


def _prov_out(p: ModelProvider) -> dict:
    return {
        "id": p.id,
        "key": p.key,
        "name": p.name,
        "kind": p.kind,
        "base_url": p.base_url,
        "api_key": _mask(p.api_key),
        "has_key": bool((p.api_key or "").strip()),
        "enabled": p.enabled,
        "note": p.note,
        "daily_calls_limit": p.daily_calls_limit,
        "daily_token_limit": p.daily_token_limit,
        "monthly_token_limit": p.monthly_token_limit,
        "rpm_limit": p.rpm_limit,
        "quota_reset_tz": p.quota_reset_tz,
        # 这家默认算什么账（模型自己没标 billing 时用它兜底）
        "billing_default": p.billing_default or "paid",
        "updated_at": p.updated_at,
    }


def _cat_out(c: ModelCatalog, used: dict[str, int] | None = None) -> dict:
    return {
        "id": c.id,
        "provider_key": c.provider_key,
        "model_id": c.model_id,
        "label": c.label,
        "capabilities": c.capabilities or [],
        "protocol": c.protocol,
        "context_window": c.context_window,
        "max_tokens": c.max_tokens,
        "temperature": c.temperature,
        "supports_tools": c.supports_tools,
        "supports_stream_usage": c.supports_stream_usage,
        "dimensions": c.dimensions,
        "billing": c.billing,
        "price_in": c.price_in,
        "price_out": c.price_out,
        "quota_scope": c.quota_scope,
        "daily_calls_limit": c.daily_calls_limit,
        "daily_token_limit": c.daily_token_limit,
        "monthly_token_limit": c.monthly_token_limit,
        "rpm_limit": c.rpm_limit,
        "enabled": c.enabled,
        "last_error": c.last_error,
        # 熔断剩余秒数：>0 表示现在这个模型正被跳过（页面要能看出来）
        "disabled_for": max(0, int(c.disabled_until or 0) - int(time.time())),
        "sort": c.sort,
        "note": c.note,
        "usage": used or {},
    }


def _usage_of(spec_model: str, provider_key: str, cred_owner: str) -> dict[str, Any]:
    """取该模型当前周期（日/月）的用量与上限，供列表页显示「已用/上限」。"""
    try:
        st = pool.quota_status(
            pool.ModelSpec(
                model=spec_model, provider_key=provider_key,
                quota_scope="model", cred_owner=cred_owner,
            )
        )
        out: dict[str, Any] = {"ratio": st.get("ratio") or 0.0}
        for k, v in (st.get("used") or {}).items():
            out[f"used_{k}"] = int(v or 0)
        for k, v in (st.get("limits") or {}).items():
            if v:
                out[f"limit_{k}"] = int(v)
        return out
    except Exception:  # noqa: BLE001 - 用量显示是装饰，不能挡住列表
        return {}


# ---------------------------------------------------------------- 元信息

@router.get("/features")
async def features(_user: User = Depends(current_user)) -> dict:
    """8 个功能槽的中文名与默认任务档位（前端下拉用）。"""
    return {
        "features": [
            {
                "key": k,
                "name": v,
                "task": pool.FEATURE_TASK.get(k, "lightweight"),
                "no_fallback": k in pool.NO_FALLBACK_FEATURES,
            }
            for k, v in pool.FEATURES.items()
        ],
        "protocols": list(_PROTOCOLS),
        "capabilities": list(_CAPABILITIES),
    }


# ---------------------------------------------------------------- 服务商

@router.get("/providers")
async def list_providers(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(current_user),
):
    rows = (
        await db.execute(select(ModelProvider).order_by(ModelProvider.key))
    ).scalars().all()
    return {"items": [_prov_out(p) for p in rows]}


@router.post("/providers")
async def create_provider(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    key = str(payload.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "缺少 key（服务商短标识，如 zhipu / siliconflow）")
    exists = (
        await db.execute(
            select(ModelProvider).where(
                ModelProvider.owner == pool.SYSTEM_OWNER, ModelProvider.key == key
            )
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"服务商 {key} 已存在")
    now = int(time.time())
    row = ModelProvider(
        owner=pool.SYSTEM_OWNER,
        key=key,
        name=str(payload.get("name") or key),
        kind=str(payload.get("kind") or "openai"),
        base_url=str(payload.get("base_url") or "").strip(),
        api_key=str(payload.get("api_key") or "").strip(),
        enabled=bool(payload.get("enabled", True)),
        note=str(payload.get("note") or ""),
        daily_calls_limit=payload.get("daily_calls_limit"),
        daily_token_limit=payload.get("daily_token_limit"),
        monthly_token_limit=payload.get("monthly_token_limit"),
        rpm_limit=payload.get("rpm_limit"),
        quota_reset_tz=str(payload.get("quota_reset_tz") or "cst"),
        billing_default=str(payload.get("billing_default") or "paid"),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "item": _prov_out(row)}


@router.patch("/providers/{key}")
async def update_provider(
    key: str,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    row = (
        await db.execute(
            select(ModelProvider).where(
                ModelProvider.owner == pool.SYSTEM_OWNER, ModelProvider.key == key
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "服务商不存在")
    for field in (
        "name", "kind", "base_url", "enabled", "note", "daily_calls_limit",
        "daily_token_limit", "monthly_token_limit", "rpm_limit", "quota_reset_tz",
        "billing_default",
    ):
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    # api_key 单独处理：传空串表示**清空**，传 None/不传表示不动
    if "api_key" in payload:
        row.api_key = str(payload.get("api_key") or "").strip()
    row.updated_at = int(time.time())
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "item": _prov_out(row)}


@router.delete("/providers/{key}")
async def delete_provider(
    key: str,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """删服务商，连带删它的全部模型条目与引用它们的绑定。

    绑定必须一起清：留着指向已删模型的 binding，`resolve_candidates` 会静默
    返回空 → 该槽回落 .env 通道，页面上看不出任何异常，但「明明配了模型却不生效」。
    """
    prov = (
        await db.execute(
            select(ModelProvider).where(
                ModelProvider.owner == pool.SYSTEM_OWNER, ModelProvider.key == key
            )
        )
    ).scalar_one_or_none()
    if not prov:
        raise HTTPException(404, "服务商不存在")
    cats = (
        await db.execute(select(ModelCatalog).where(ModelCatalog.provider_key == key))
    ).scalars().all()
    dead_ids = {c.id for c in cats}
    for c in cats:
        await db.delete(c)
    bindings = (await db.execute(select(ModelBinding))).scalars().all()
    for b in bindings:
        fb = [i for i in (b.fallbacks or []) if i not in dead_ids]
        if b.primary_model_id in dead_ids:
            b.primary_model_id = fb.pop(0) if fb else ""
        if fb != (b.fallbacks or []):
            b.fallbacks = fb
        b.updated_at = int(time.time())
    await db.delete(prov)
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "deleted_models": len(dead_ids)}


@router.post("/providers/{key}/test")
async def test_provider(
    key: str,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
    api_key: str = Body("", embed=True),
):
    """连通性测试：拉一次模型列表（不花钱、不烧额度，但能验 Key 与端点）。"""
    prov = (
        await db.execute(
            select(ModelProvider).where(
                ModelProvider.owner == pool.SYSTEM_OWNER, ModelProvider.key == key
            )
        )
    ).scalar_one_or_none()
    if not prov:
        raise HTTPException(404, "服务商不存在")
    token = (api_key or "").strip() or (prov.api_key or "").strip()
    if not token:
        raise HTTPException(400, "没有可测试的 Key（既没传也没配）")
    try:
        items = await _fetch_models(prov.kind, prov.base_url, token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"连通性测试失败：{exc}") from exc
    return {"ok": True, "count": len(items), "sample": [i["model_id"] for i in items[:8]]}


async def _fetch_models(kind: str, base_url: str, token: str) -> list[dict]:
    """按服务商类型拉模型清单（sync 与 test 共用）。"""
    base = (base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("未配置 base_url")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=20) as cli:
        if kind == "cloudflare":
            if base.endswith("/ai/v1"):
                base = base[: -len("/ai/v1")]
            resp = await cli.get(f"{base}/models/search", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            out = []
            for m in (data.get("result") or []):
                mid = m.get("name") or ""
                if not mid:
                    continue
                out.append(
                    {
                        "model_id": mid,
                        "label": m.get("description") or mid,
                        "capabilities": _guess_caps(mid),
                        "raw": {
                            "task": (m.get("task") or {}).get("name") or "",
                            "properties": m.get("properties") or {},
                        },
                    }
                )
            return out
        resp = await cli.get(f"{base}/models", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        out = []
        for m in (data.get("data") or data.get("models") or []):
            mid = m.get("id") or m.get("name") or ""
            if not mid:
                continue
            out.append(
                {
                    "model_id": mid,
                    "label": mid,
                    "capabilities": _guess_caps(mid),
                    "raw": {"owned_by": m.get("owned_by") or ""},
                }
            )
        return out


def _guess_caps(model_id: str) -> list[str]:
    """从模型名猜能力。同步完管理员还能改——这只是省去 200 次手工勾选。"""
    m = (model_id or "").lower()
    if any(k in m for k in ("embed", "bge", "gte", "e5-", "text-similarity")):
        return ["embedding"]
    if any(k in m for k in ("flux", "sdxl", "sd3", "kolors", "seedream", "dall",
                            "wanx", "image", "stable-diffusion", "qwen-image")):
        return ["image"]
    caps = ["chat"]
    if any(k in m for k in ("vl", "vision", "4o", "omni", "glm-4v")):
        caps.append("vision")
    return caps


@router.post("/providers/{key}/sync")
async def sync_provider(
    key: str,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """拉远端模型清单，返回「池里还没有的」候选（不自动入库）。

    不自动入库是刻意的：厂商目录动辄上百个模型（含一堆你用不上的尺寸变体），
    全塞进来只会让下拉框没法用。管理员勾选后再 bulk 入库。
    """
    prov = (
        await db.execute(
            select(ModelProvider).where(
                ModelProvider.owner == pool.SYSTEM_OWNER, ModelProvider.key == key
            )
        )
    ).scalar_one_or_none()
    if not prov:
        raise HTTPException(404, "服务商不存在")
    if not (prov.api_key or "").strip():
        raise HTTPException(400, "该服务商没配 Key，无法拉取模型清单")
    try:
        items = await _fetch_models(prov.kind, prov.base_url, prov.api_key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"拉取模型清单失败：{exc}") from exc

    known = {
        r[0]
        for r in (
            await db.execute(
                select(ModelCatalog.model_id).where(ModelCatalog.provider_key == key)
            )
        ).all()
    }
    for it in items:
        it["exists"] = it["model_id"] in known
        it["protocol"] = _guess_protocol(it["capabilities"], prov.kind)
    return {"ok": True, "total": len(items), "items": items}


def _guess_protocol(caps: list[str], kind: str) -> str:
    cf = kind == "cloudflare"
    if "embedding" in caps:
        return "cf_ai_run_embed" if cf else "openai_embeddings"
    if "image" in caps:
        return "cf_ai_run_image" if cf else "openai_images"
    return "openai_chat"


# ---------------------------------------------------------------- 模型条目

@router.get("/catalog")
async def list_catalog(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    rows = (
        await db.execute(select(ModelCatalog).order_by(ModelCatalog.sort, ModelCatalog.model_id))
    ).scalars().all()
    items = []
    for c in rows:
        items.append(_cat_out(c, _usage_of(c.model_id, c.provider_key, pool.SYSTEM_OWNER)))
    # 个人覆盖标记：用户在哪几家填了自己的 Key
    mine = {
        r[0]
        for r in (
            await db.execute(
                select(ModelProviderKey.provider_key).where(
                    ModelProviderKey.owner == owner, ModelProviderKey.enabled.is_(True)
                )
            )
        ).all()
    }
    for it in items:
        it["using_my_key"] = it["provider_key"] in mine
    return {"items": items}


@router.post("/catalog")
async def create_catalog(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    provider_key = str(payload.get("provider_key") or "").strip()
    model_id = str(payload.get("model_id") or "").strip()
    if not provider_key or not model_id:
        raise HTTPException(400, "缺少 provider_key / model_id")
    prov = (
        await db.execute(
            select(ModelProvider).where(ModelProvider.key == provider_key)
        )
    ).scalar_one_or_none()
    if not prov:
        raise HTTPException(400, f"服务商 {provider_key} 不存在，请先添加服务商")
    exists = (
        await db.execute(
            select(ModelCatalog).where(
                ModelCatalog.provider_key == provider_key, ModelCatalog.model_id == model_id
            )
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"模型 {model_id} 已在池里")

    caps = payload.get("capabilities") or ["chat"]
    now = int(time.time())
    row = ModelCatalog(
        id=uuid.uuid4().hex,
        provider_key=provider_key,
        model_id=model_id,
        label=str(payload.get("label") or model_id),
        capabilities=list(caps),
        protocol=str(payload.get("protocol") or _guess_protocol(list(caps), prov.kind)),
        context_window=payload.get("context_window"),
        max_tokens=payload.get("max_tokens"),
        temperature=payload.get("temperature"),
        supports_tools=bool(payload.get("supports_tools", True)),
        supports_stream_usage=bool(payload.get("supports_stream_usage", True)),
        dimensions=payload.get("dimensions"),
        billing=str(payload.get("billing") or "free"),
        price_in=payload.get("price_in"),
        price_out=payload.get("price_out"),
        quota_scope=str(payload.get("quota_scope") or "model"),
        daily_calls_limit=payload.get("daily_calls_limit"),
        daily_token_limit=payload.get("daily_token_limit"),
        monthly_token_limit=payload.get("monthly_token_limit"),
        rpm_limit=payload.get("rpm_limit"),
        enabled=bool(payload.get("enabled", True)),
        sort=int(payload.get("sort") or 100),
        note=str(payload.get("note") or ""),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "item": _cat_out(row)}


@router.post("/catalog/bulk")
async def bulk_create_catalog(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """批量入库（sync 之后勾选的那些）。已存在的跳过，不报错。"""
    provider_key = str(payload.get("provider_key") or "").strip()
    items = payload.get("items") or []
    if not provider_key or not items:
        raise HTTPException(400, "缺少 provider_key / items")
    prov = (
        await db.execute(
            select(ModelProvider).where(ModelProvider.key == provider_key)
        )
    ).scalar_one_or_none()
    if not prov:
        raise HTTPException(400, f"服务商 {provider_key} 不存在")
    known = {
        r[0]
        for r in (
            await db.execute(
                select(ModelCatalog.model_id).where(ModelCatalog.provider_key == provider_key)
            )
        ).all()
    }
    now = int(time.time())
    added = 0
    for it in items:
        mid = str(it.get("model_id") or "").strip()
        if not mid or mid in known:
            continue
        caps = it.get("capabilities") or _guess_caps(mid)
        db.add(
            ModelCatalog(
                id=uuid.uuid4().hex,
                provider_key=provider_key,
                model_id=mid,
                label=str(it.get("label") or mid),
                capabilities=list(caps),
                protocol=str(it.get("protocol") or _guess_protocol(list(caps), prov.kind)),
                supports_tools=False,  # 新模型默认保守：不知道就当不支持，用起来再开
                supports_stream_usage=prov.kind != "cloudflare",
                billing=str(it.get("billing") or "free"),
                quota_scope=str(it.get("quota_scope") or "model"),
                enabled=True,
                sort=100,
                note="由模型清单同步入库",
                created_at=now,
                updated_at=now,
            )
        )
        known.add(mid)
        added += 1
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "added": added}


@router.patch("/catalog/{cid}")
async def update_catalog(
    cid: str,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    row = await db.get(ModelCatalog, cid)
    if not row:
        raise HTTPException(404, "模型不存在")
    simple = (
        "label", "protocol", "context_window", "max_tokens", "temperature",
        "supports_tools", "supports_stream_usage", "dimensions", "billing",
        "price_in", "price_out", "quota_scope", "daily_calls_limit",
        "daily_token_limit", "monthly_token_limit", "rpm_limit", "enabled",
        "sort", "note",
    )
    for field in simple:
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    if "capabilities" in payload and payload["capabilities"] is not None:
        row.capabilities = list(payload["capabilities"])
    if "disabled_until" in payload:
        # 前端「解除熔断」按钮：传 0 立即恢复
        row.disabled_until = int(payload["disabled_until"] or 0)
        if not row.disabled_until:
            row.last_error = ""
    row.updated_at = int(time.time())
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "item": _cat_out(row)}


@router.delete("/catalog/{cid}")
async def delete_catalog(
    cid: str,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    row = await db.get(ModelCatalog, cid)
    if not row:
        raise HTTPException(404, "模型不存在")
    bindings = (await db.execute(select(ModelBinding))).scalars().all()
    touched = []
    for b in bindings:
        fb = [i for i in (b.fallbacks or []) if i != cid]
        if b.primary_model_id == cid:
            b.primary_model_id = fb.pop(0) if fb else ""
            touched.append(b.feature)
        if fb != (b.fallbacks or []):
            b.fallbacks = fb
            if b.feature not in touched:
                touched.append(b.feature)
        b.updated_at = int(time.time())
    await db.delete(row)
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "affected_features": touched}


# ---------------------------------------------------------------- 绑定

@router.get("/bindings")
async def list_bindings(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(current_user),
):
    snap = pool.snapshot()
    rows = (await db.execute(select(ModelBinding))).scalars().all()
    by_feature = {b.feature: b for b in rows}
    out = []
    for feat, name in pool.FEATURES.items():
        b = by_feature.get(feat)
        primary = b.primary_model_id if b else ""
        fallbacks = list(b.fallbacks or []) if b else []
        # 解析结果（已过滤额度耗尽 / 熔断 / 停用的）：页面要显示「实际会用的是谁」
        cands = pool.resolve_candidates(feat)
        out.append(
            {
                "feature": feat,
                "name": name,
                "task": pool.FEATURE_TASK.get(feat, "lightweight"),
                "no_fallback": feat in pool.NO_FALLBACK_FEATURES,
                "primary": primary,
                "fallbacks": fallbacks,
                "resolved": cands[0].model if cands else "",
                "resolved_label": cands[0].display if cands else "",
                "primary_label": (snap.catalog.get(primary) or {}).get("label") or "",
                "updated_at": b.updated_at if b else 0,
            }
        )
    return {"items": out}


@router.put("/bindings/{feature}")
async def save_binding(
    feature: str,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _admin: User = Depends(require_admin),
):
    """保存某个功能槽的绑定（主模型 + 备选链）。

    校验见文件头。换 embedding 模型时会先算一遍 `stale_chunks`：
      - 为 0 → 直接保存；
      - 非 0 → 409 `EMBED_DIM_MISMATCH`，前端确认后带 `force=true` 重发。
    """
    if feature not in pool.FEATURES:
        raise HTTPException(400, f"未知功能槽：{feature}")
    primary = str(payload.get("primary") or "").strip()
    fallbacks = [str(x) for x in (payload.get("fallbacks") or []) if str(x).strip()]
    force = bool(payload.get("force"))

    snap = pool.snapshot()
    main = snap.catalog.get(primary) if primary else None
    if primary and not main:
        raise HTTPException(400, "主模型不在模型池里（可能已被删除）")

    # 1) embedding 槽不许有备选链
    if feature in pool.NO_FALLBACK_FEATURES and fallbacks:
        raise HTTPException(
            400,
            f"「{pool.FEATURES[feature]}」不支持备选模型：换 embedding 模型会让已有"
            f"向量全部失效，必须显式重建，不能靠自动降级悄悄换。",
        )

    # 2) 备选链能力必须与主模型一致
    if main:
        main_caps = set(main.get("capabilities") or [])
        for fid in fallbacks:
            fb = snap.catalog.get(fid)
            if not fb:
                raise HTTPException(400, f"备选模型 {fid} 不在模型池里")
            if set(fb.get("capabilities") or []) != main_caps:
                raise HTTPException(
                    400,
                    f"备选模型 {fb.get('model_id')} 的能力（{fb.get('capabilities')}）"
                    f"与主模型（{list(main_caps)}）不一致，降级后行为会突变。",
                )
            if bool(fb.get("supports_tools")) != bool(main.get("supports_tools")):
                raise HTTPException(
                    400,
                    f"备选模型 {fb.get('model_id')} 的工具调用能力与主模型不一致，"
                    "降级会让 Agent 的工具循环在中途静默失效。",
                )

    # 3) embedding 换模型 → 旧向量失效，先确认
    if feature == "embedding" and main:
        old = snap.catalog.get(
            (snap.bindings.get("embedding") or {}).get("primary") or ""
        )
        old_model = (old or {}).get("model_id") or ""
        new_model = main.get("model_id") or ""
        if old_model and old_model != new_model:
            stale = await kb.count_stale(db, owner, new_model)
            if stale and not force:
                raise HTTPException(
                    409,
                    {
                        "code": "EMBED_DIM_MISMATCH",
                        "message": (
                            f"换成 {new_model} 后，库里 {stale} 条已就绪的向量会失效"
                            "（不同模型产出的向量不在同一空间，检索会静默漏召回）。"
                            "继续将立即重建索引。"
                        ),
                        "stale_chunks": stale,
                        "from_model": old_model,
                        "to_model": new_model,
                    },
                )

    row = await db.get(ModelBinding, feature)
    now = int(time.time())
    if not row:
        row = ModelBinding(feature=feature)
        db.add(row)
    row.primary_model_id = primary
    row.fallbacks = fallbacks
    row.updated_at = now
    await db.commit()
    pool.invalidate()
    await pool.refresh_snapshot(db)
    return {"ok": True, "feature": feature, "primary": primary, "fallbacks": fallbacks}


# ---------------------------------------------------------------- 额度

@router.get("/quota")
async def quota_board(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """额度看板：每个模型当前周期用了多少、上限多少、谁烧的。

    平台 Key（cred_owner=system）与个人 Key 分开统计——用户用自己的 Key 时
    不该看到「额度快没了」的恐慌提示，那是平台的额度。
    """
    snap = pool.snapshot()
    items = []
    # dict.fromkeys 去重：owner 是 system（管理员自己）时只统计一次
    for cred in dict.fromkeys([pool.SYSTEM_OWNER, owner]):
        for cid, entry in snap.catalog.items():
            if not entry.get("enabled"):
                continue
            prov = snap.providers.get(entry.get("provider_key") or "") or {}
            st = pool.quota_status(
                pool.ModelSpec(
                    model=entry.get("model_id") or "",
                    provider_key=prov.get("key") or "",
                    quota_scope=entry.get("quota_scope") or "model",
                    cred_owner=cred,
                    catalog_id=cid,
                )
            )
            used = st.get("used") or {}
            if not any(int(v or 0) for v in used.values()):
                continue
            items.append(
                {
                    "model": entry.get("model_id"),
                    "label": entry.get("label") or "",
                    "provider": prov.get("key"),
                    "billing": entry.get("billing"),
                    "cred_owner": cred,
                    "used": used,
                    "limits": st.get("limits") or {},
                    "ratio": st.get("ratio") or 0.0,
                }
            )
    return {"items": items}


# ---------------------------------------------------------------- 个人凭证

@router.get("/my-keys")
async def my_keys(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """我填的个人 Key（脱敏），以及各家服务商的元数据（供表单下拉）。"""
    rows = (
        await db.execute(
            select(ModelProviderKey).where(ModelProviderKey.owner == owner)
        )
    ).scalars().all()
    mine = {r.provider_key: r for r in rows}
    provs = (
        await db.execute(select(ModelProvider).where(ModelProvider.enabled.is_(True)))
    ).scalars().all()
    return {
        "items": [
            {
                "provider_key": p.key,
                "provider_name": p.name,
                "using_my_key": p.key in mine and bool((mine[p.key].api_key or "").strip()),
                "api_key": _mask(mine[p.key].api_key) if p.key in mine else "",
                "enabled": bool(mine[p.key].enabled) if p.key in mine else True,
                "verified_at": mine[p.key].verified_at if p.key in mine else None,
            }
            for p in provs
        ]
    }


@router.put("/my-keys/{provider_key}")
async def save_my_key(
    provider_key: str,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """填/清自己的 Key。

    传空串 = 删除这条覆盖、回退到平台全局 Key（不是「禁用」，是「不覆盖了」）。
    填了之后这个服务商的模型都烧你自己的额度（`cred_owner` = 你）。
    """
    prov = (
        await db.execute(
            select(ModelProvider).where(
                ModelProvider.key == provider_key, ModelProvider.enabled.is_(True)
            )
        )
    ).scalar_one_or_none()
    if not prov:
        raise HTTPException(404, "服务商不存在或已停用")
    key = str(payload.get("api_key") or "").strip()
    row = (
        await db.execute(
            select(ModelProviderKey).where(
                ModelProviderKey.provider_key == provider_key,
                ModelProviderKey.owner == owner,
            )
        )
    ).scalar_one_or_none()
    if not key:
        if row:
            await db.delete(row)
            await db.commit()
        return {"ok": True, "using_my_key": False}
    if not row:
        row = ModelProviderKey(
            id=uuid.uuid4().hex, provider_key=provider_key, owner=owner
        )
        db.add(row)
    row.api_key = key
    row.enabled = True
    row.updated_at = int(time.time())
    await db.commit()
    return {"ok": True, "using_my_key": True, "api_key": _mask(key)}


@router.post("/my-keys/{provider_key}/verify")
async def verify_my_key(
    provider_key: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """验证我填的 Key 能不能用（拉一次模型列表，不烧额度）。"""
    prov = (
        await db.execute(
            select(ModelProvider).where(ModelProvider.key == provider_key)
        )
    ).scalar_one_or_none()
    if not prov:
        raise HTTPException(404, "服务商不存在")
    row = (
        await db.execute(
            select(ModelProviderKey).where(
                ModelProviderKey.provider_key == provider_key,
                ModelProviderKey.owner == owner,
            )
        )
    ).scalar_one_or_none()
    token = (row.api_key if row else "") or ""
    if not token.strip():
        raise HTTPException(400, "还没填 Key")
    try:
        items = await _fetch_models(prov.kind, prov.base_url, token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"验证失败：{exc}") from exc
    if row:
        row.verified_at = int(time.time())
        await db.commit()
    return {"ok": True, "count": len(items)}
