"""知乎数据开放平台客户端（热榜抓取 + 趋势计算）。

鉴权
----
两个请求头缺一不可（2026-10-19 实测，漏了时间戳会被直接拒）：

    Authorization: Bearer <access_secret>
    X-Request-Timestamp: <秒级 Unix 时间戳>

接口形态（均已实跑验证）
----------------------
热榜  GET  /api/v1/content/hot_list?Limit=N      （N ≤ 30）
搜索  GET  /api/v1/content/zhihu_search?Query=&Count=  （≤ 10）
全网  GET  /api/v1/content/global_search?Query=&Count= （≤ 20）
直答  POST /v1/chat/completions                   （完全兼容 OpenAI 协议）

统一返回外壳：{"Code": 0, "Message": "success", "Data": {...}}

热榜返回的重要细节（决定了数据建模）
----------------------------------
Items 里每项只有 4 个字段：Title / Url / ThumbnailUrl / Summary。

1. **没有热度值字段**。这意味着「热度趋势」不能直接从返回值读出来，
   只能靠**同一话题在不同时间点的名次序列**来构造。这是本模块每张表都
   设计为「一条记录 = 一次露面」的根本原因（详见 models/zhihu.py）。
2. **Url 带 utm 追踪参数**，且 `utm_source` 跟 Access Secret 绑定
   （实测同一账号不同 secret 该值不同）。所以绝不能用完整 Url 当话题
   主键，必须解析出里面的 question id。
3. **Summary 是平台白送的 ~200 字摘要**，是零成本的选题素材：判断这个
   热点值不值得写，不用再调任何接口。

额度纪律
--------
免费额度是**整个账号一个池**（开放平台的页面效果测试也扣同一个池）。定时
抓取在用水位达 _QUOTA_SAFETY 之后必须主动让路 —— 少抓一次无所谓，把池子
烧穿导致实时查询全挂才是事故。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import settings
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.settings import AppSetting
from app.models.zhihu import HotSnapshot, HotTopic, ZhihuApiUsage

API = "https://developer.zhihu.com"
CST = timezone(timedelta(hours=8))

# 平台错误码 → 人类可读提示（配便利性 > 精确性，够定位问题就行）
_ERR_HINT = {
    30001: "触发平台限流：热榜不宜高频调用",
    40001: "Access Secret 无效或已失效",
    40003: "Access Secret 格式不正确",
}


def _today_key() -> str:
    """当日 key（CST 自然日，与 core/quota.py 的 period_key 口径一致）。"""
    return datetime.now(CST).strftime("%Y-%m-%d")


def _configured() -> bool:
    return bool((settings.ZHIHU_ACCESS_SECRET or "").strip())


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.ZHIHU_ACCESS_SECRET}",
        # 秒级时间戳，每次请求都得重新算 —— 不能缓存成常量
        "X-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json",
    }


def _client_kwargs() -> dict:
    kw: dict = {"timeout": settings.ZHIHU_TIMEOUT}
    proxy = (settings.ZHIHU_PROXY or "").strip()
    if proxy:
        kw["proxy"] = proxy
    return kw


# ---------------------------------------------------------------- URL 处理
# https://www.zhihu.com/question/2094350166518249285?utm_... → question/<id>
# https://zhuanlan.zhihu.com/p/2089828274960602968?utm_...   → p/<id>
# https://www.zhihu.com/question/xxx/answer/yyy              → question/<id>
_QID_RE = re.compile(r"zhihu\.com/(question|p|answer)/(\d+)")


def parse_question_id(url: str) -> str:
    """从知乎链接里取出稳定的内容 id。

    拿不到就用清洗后的 url 兜底 —— 宁可归并得差一点，也不能丢条目。
    """
    m = _QID_RE.search(url or "")
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    return (url or "").split("?")[0] or uuid.uuid4().hex


def clean_url(url: str) -> str:
    """去掉 utm_* 追踪参数，留给用户看的干净链接。"""
    return (url or "").split("?")[0]


# ---------------------------------------------------------------- 关键词过滤
def _block_keywords() -> list[str]:
    """解析屏蔽词：逗号（中英文）、顿号、空白都认。

    一开始只按逗号切，`"选举，战争 trump"` 会被切成 `[... "战争 trump"]` ——
    空格被当成词的一部分，"战争" 再也匹配不上。既然是从环境变量手工配的，
    分隔符就该宽容一点，别让用户去猜。
    """
    raw = (settings.ZHIHU_BLOCK_KEYWORDS or "").strip()
    if not raw:
        return []
    return [w for w in re.split(r"[,，、\s]+", raw) if w]


def is_blocked(title: str, summary: str = "") -> bool:
    """标题或摘要命中屏蔽词 → 丢弃。"""
    words = _block_keywords()
    if not words:
        return False
    text = f"{title or ''}\n{summary or ''}"
    return any(w in text for w in words)


# ---------------------------------------------------------------- 额度计数
async def _bump_usage(session, n: int = 1) -> None:
    """累加当日调用次数。

    刻意写成「读-改-写」而不是 SQLite 的 `ON CONFLICT DO UPDATE`：后者要用
    `sqlalchemy.dialects.sqlite.insert()`，那样这段在 PostgreSQL 上就跑不了
    （项目 DATABASE_URL 也支持 pg）。竞态方面：写入方只有后台定时抓取这一个
    循环（单线程）加偶尔的手动刷新，撞上的概率可以忽略，不值得为此锁方言。
    """
    key = _today_key()
    row = (
        await session.execute(select(ZhihuApiUsage).where(ZhihuApiUsage.day == key))
    ).scalar_one_or_none()
    if row:
        row.count += n
        row.updated_at = int(time.time())
    else:
        session.add(ZhihuApiUsage(day=key, count=n, updated_at=int(time.time())))


async def usage_today() -> dict:
    """当日额度使用情况。

    `safe_limit` = 自动抓取的停用水位（用量达到它之后定时任务让路，把余量
    留给手动刷新与连通性自检）；`remaining` = 池子里还剩多少，管理后台直接展示。
    """
    async with SessionLocal() as session:
        row = (
            await session.execute(select(ZhihuApiUsage).where(ZhihuApiUsage.day == _today_key()))
        ).scalar_one_or_none()
        used = row.count if row else 0
    quota = max(1, settings.ZHIHU_DAILY_QUOTA)
    safe_limit = min(settings.ZHIHU_QUOTA_SAFETY, quota)
    return {
        "used": used,
        "quota": quota,
        "safe_limit": safe_limit,
        # 定时抓取的准入条件：留给实时查询与重试的余量还在
        "safe_to_fetch": used < safe_limit,
        "remaining": max(0, quota - used),
    }


# ---------------------------------------------------------------- 运行时凭证
# Access Secret 原来只能改 .env 再重启。管理后台要能在线设置（2026-10-19 的
# 需求），所以多了一条 DB 通道：AppSetting 里存一份，启动时与 PUT 时覆盖到
# settings 对象（pydantic-settings 的实例运行时可变，已实测）。
#
# 优先级：**DB 里有这一行（哪怕是空串）= 管理后台说了算；没有这一行 = 回退
# .env**。这样「后台清空凭证」能真正生效，不会被 .env 里的旧值阴魂不散。
_SECRET_OWNER = "system"
_SECRET_KEY = "zhihu_access_secret"


async def load_runtime_secret() -> None:
    """启动时调用：DB 里存过的 secret 覆盖到 settings（DB 优先于 .env）。"""
    async with SessionLocal() as session:
        val = (
            await session.execute(
                select(AppSetting.value).where(
                    AppSetting.owner == _SECRET_OWNER, AppSetting.key == _SECRET_KEY
                )
            )
        ).scalar_one_or_none()
    if val is not None:
        settings.ZHIHU_ACCESS_SECRET = val


async def set_runtime_secret(secret: str) -> None:
    """设置（传空串 = 清除）并立即生效，不用重启后端。"""
    secret = (secret or "").strip()
    async with SessionLocal() as session:
        # upsert 手法与 routers/settings.py 的 _set_setting 同源：先插
        # （on_conflict_do_nothing）再 update。刻意不用通用 insert 的
        # on_conflict_do_update —— 那是 sqlite 方言专属方法，项目还要兼容
        # Postgres（见该文件注释）。
        await session.execute(
            sqlite_insert(AppSetting)
            .values(
                id=uuid.uuid4().hex,
                owner=_SECRET_OWNER,
                key=_SECRET_KEY,
                value=secret,
                updated_at=int(time.time()),
            )
            .on_conflict_do_nothing(index_elements=["owner", "key"])
        )
        await session.execute(
            update(AppSetting)
            .where(AppSetting.owner == _SECRET_OWNER, AppSetting.key == _SECRET_KEY)
            .values(value=secret, updated_at=int(time.time()))
        )
        await session.commit()
    settings.ZHIHU_ACCESS_SECRET = secret


def secret_info() -> dict:
    """凭证状态。**永远不回传明文**，只给脱敏预览（后 4 位）——
    与公众号 secret 的 secret_set 布尔相比多给了 4 位，因为轮换密钥时
    用户需要确认「我现在挂的是哪一把」。"""
    s = (settings.ZHIHU_ACCESS_SECRET or "").strip()
    return {"configured": bool(s), "masked": f"****{s[-4:]}" if s else ""}


async def last_snapshot_info() -> dict:
    """最近一次抓取批次（不论成败）：管理后台「上次抓取」用。"""
    async with SessionLocal() as session:
        snap = (
            await session.execute(
                select(HotSnapshot).order_by(HotSnapshot.fetched_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    if not snap:
        return {"fetched_at": None, "ok": None, "item_count": 0}
    return {"fetched_at": snap.fetched_at, "ok": snap.ok, "item_count": snap.item_count}


# ---------------------------------------------------------------- 单次抓取
async def fetch_raw_hot(limit: int | None = None) -> dict:
    """调一次热榜接口，原始结构化结果。返回 {ok, items?, error?}。

    内部已做退避重试；失败不抛异常（调用方往往是定时器，不能让循环炸掉）。
    """
    if not _configured():
        return {"ok": False, "error": "未配置 ZHIHU_ACCESS_SECRET"}

    n = max(1, min(int(limit or settings.ZHIHU_HOT_LIMIT), 30))
    last_err = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(**_client_kwargs()) as client:
                resp = await client.get(
                    f"{API}/api/v1/content/hot_list",
                    headers=_headers(),
                    params={"Limit": n},
                )
            data = resp.json() if resp.content else {}
            code = data.get("Code", resp.status_code)
            if code == 0:
                items = (data.get("Data") or {}).get("Items") or []
                return {"ok": True, "items": items}
            last_err = _ERR_HINT.get(code) or f"知乎返回 Code={code}：{data.get('Message', '')}"
            if resp.status_code == 401 or code in (40001, 40003):
                break  # 凭证问题是永久性的，重试没意义
            if code == 30001:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            await asyncio.sleep(1.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last_err = f"请求失败：{exc}"
            await asyncio.sleep(1.5 * (attempt + 1))
    return {"ok": False, "error": last_err or "未知错误"}


async def fetch_hot_once(force: bool = False) -> dict:
    """抓一次热榜并入库。返回 {ok, saved?, skipped?, error?}。

    `force=True` 时跳过额度水位保护（只给「用户手动刷新」用）。
    """
    if not _configured():
        return {"ok": False, "skipped": True, "error": "未配置 ZHIHU_ACCESS_SECRET"}

    usage = await usage_today()
    if not force and not usage["safe_to_fetch"]:
        msg = f"已达当日额度水位 {usage['used']}/{usage['quota']}，跳过本次抓取"
        logger.warning("[zhihu] %s", msg)
        await _record_snapshot(ok=False, error=msg, count=0, calls=0)
        return {"ok": False, "skipped": True, "error": msg}

    raw = await fetch_raw_hot()
    calls = 1  # 调了一次 API（无论成败都保守计数：平台侧的口径我们说了不算）
    now = int(time.time())

    if not raw["ok"]:
        await _record_snapshot(ok=False, error=raw["error"], count=0, calls=calls)
        async with SessionLocal() as session:
            await _bump_usage(session, calls)
            await session.commit()
        return {"ok": False, "error": raw["error"]}

    items = raw["items"]
    blocked = 0
    parsed: list[dict] = []
    for idx, it in enumerate(items, start=1):
        title = (it.get("Title") or "").strip()
        summary = (it.get("Summary") or "").strip()
        if not title:
            continue
        if is_blocked(title, summary):
            blocked += 1
            continue
        url = clean_url(it.get("Url") or "")
        parsed.append(
            {
                "question_id": parse_question_id(it.get("Url") or ""),
                "fetched_at": now,
                "rank": idx,
                "title": title,
                "url": url,
                "summary": summary or None,
                "thumbnail_url": (it.get("ThumbnailUrl") or "").strip() or None,
            }
        )

    # 快照 + 话题 + 额度计数**同一个事务**落库。
    #
    # 曾经分成两个事务（先 _record_snapshot 再写话题），进程在两次 commit
    # 之间被杀会留下「没有话题的孤儿快照」——实测踩到：重启 uvicorn 的
    # 瞬间正好卡在这个窗口，审计表里多出一条 item_count=30 但 0 话题的
    # 记录，额度计数也少 1。窗口只有毫秒级，但原子写不要钱：要么整批
    # 落库，要么什么都没有，不会出现半个批次。
    async with SessionLocal() as session:
        snap_id = uuid.uuid4().hex
        session.add(
            HotSnapshot(
                id=snap_id,
                fetched_at=now,
                item_count=len(parsed),
                ok=True,
                error=None,
                api_calls=calls,
            )
        )
        for row in parsed:
            session.add(HotTopic(id=uuid.uuid4().hex, snapshot_id=snap_id, **row))
        await _bump_usage(session, calls)
        await session.commit()

    logger.info("[zhihu] 热榜抓取成功：%d 条入库，%d 条被屏蔽词过滤", len(parsed), blocked)
    return {"ok": True, "saved": len(parsed), "blocked": blocked}


async def _record_snapshot(*, ok: bool, error: str | None, count: int, calls: int) -> str:
    """写一条抓取批次记录，返回 snapshot id（失败也写，否则无从发现漏采）。"""
    snap_id = uuid.uuid4().hex
    async with SessionLocal() as session:
        session.add(
            HotSnapshot(
                id=snap_id,
                fetched_at=int(time.time()),
                item_count=count,
                ok=ok,
                error=error,
                api_calls=calls,
            )
        )
        await session.commit()
    return snap_id


# ---------------------------------------------------------------- 趋势出口
async def hot_with_trend(limit: int = 20, lookback: int = 24) -> dict:
    """当前榜单 + 每个话题的趋势信号，给灵感卡片用。

    信号定义（这是「半小时一次采样」真正换来的东西）：
      - climb       与**上一次成功抓取**的名次之差，正数 = 在往上冲
      - is_new      今天首次出现（此前从未进过榜）
      - staying_hours  首次进榜距今小时数 —— 越大越是长线议题，不抢时效
      - heat_score  综合分：正在冲 + 名次靠前，给「最值得写」排序

    `lookback` = 回看多少个历史批次用于算 climb（每批间隔即抓取间隔）。
    """
    async with SessionLocal() as session:
        # 1) 最新一次成功批次的榜单
        snap = (
            await session.execute(
                select(HotSnapshot).where(HotSnapshot.ok.is_(True)).order_by(HotSnapshot.fetched_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if not snap:
            return {"ok": False, "error": "还没有任何成功抓取记录", "items": []}

        topics = (
            await session.execute(
                select(HotTopic).where(HotTopic.snapshot_id == snap.id).order_by(HotTopic.rank).limit(limit)
            )
        ).scalars().all()
        if not topics:
            return {"ok": False, "error": "最新批次没有条目", "items": []}

        qids = [t.question_id for t in topics]

        # 2) 历史批次：找每个话题上一次露面的名次（算 climb）
        past = (
            await session.execute(
                select(HotTopic)
                .where(HotTopic.question_id.in_(qids), HotTopic.fetched_at < snap.fetched_at)
                .order_by(HotTopic.fetched_at.desc())
                .limit(max(200, lookback * 30))
            )
        ).scalars().all()
        prev_rank: dict[str, int] = {}
        for t in past:  # past 已按时间倒序，第一次遇到即「最近一次」
            prev_rank.setdefault(t.question_id, t.rank)

        # 3) 首次进榜时间 / 出现次数（跨全天的准确值，不受 lookback 限制）
        agg = (
            await session.execute(
                select(
                    HotTopic.question_id,
                    func.min(HotTopic.fetched_at),
                    func.count(HotTopic.id),
                )
                .where(HotTopic.question_id.in_(qids))
                .group_by(HotTopic.question_id)
            )
        ).all()
        first_seen = {r[0]: r[1] for r in agg}
        seen_times = {r[0]: r[2] for r in agg}

        day_start = int(datetime.now(CST).replace(hour=0, minute=0, second=0).timestamp())

    now = int(time.time())
    out: list[dict[str, Any]] = []
    for t in topics:
        first = first_seen.get(t.question_id, t.fetched_at)
        prev = prev_rank.get(t.question_id)
        climb = (prev - t.rank) if prev else 0
        rank_score = max(0.0, (30 - t.rank) / 30)  # 名次越靠前越接近 1
        climb_score = min(climb / 10, 1.0) if climb > 0 else 0.0
        out.append(
            {
                "question_id": t.question_id,
                "title": t.title,
                "url": t.url,
                "summary": t.summary,
                "thumbnail_url": t.thumbnail_url,
                "rank": t.rank,
                "climb": climb,
                "is_new": first >= day_start,
                "staying_hours": round((now - first) / 3600, 1),
                "seen_times": seen_times.get(t.question_id, 1),
                # 在爬升的权重略高于名次 —— 对追时效的创作来说，变化率比
                # 绝对热度更能指导「现在写还是晚点写」
                "heat_score": round(climb_score * 0.6 + rank_score * 0.4, 3),
            }
        )

    out.sort(key=lambda x: x["heat_score"], reverse=True)
    return {"ok": True, "fetched_at": snap.fetched_at, "items": out}


# ---------------------------------------------------------------- 定时循环
async def loop_hot_fetch() -> None:
    """后台定时抓取循环（挂 lifespan，详见 main.py）。

    四条纪律：
      1. 循环体内吞掉一切异常 —— 一个热点定时器没资格把服务拖垮；
      2. 启动先补抓：距上次成功抓取超过间隔就立刻抓一次（这台沙箱会休眠，
         不补的话每次唤醒都要等满一个间隔才有数据）；
      3. 没配凭证就**待命**而不是退出：secret 现在可以在管理后台运行时配置
         （见 set_runtime_secret），启动时没配不代表待会不会配。空转成本
         只是每 5 分钟读一次内存属性；
      4. 抓取批次不论成败都写审计，漏采要能被发现。
    """
    if not _configured():
        logger.info("[zhihu] 未配置 ZHIHU_ACCESS_SECRET，抓取任务待命（每 5 分钟探一次）")
        while not _configured():
            await asyncio.sleep(300)
        logger.info("[zhihu] 凭证已配置，抓取任务启动")

    interval = max(1, settings.ZHIHU_HOT_INTERVAL_MINUTES) * 60
    await asyncio.sleep(25)  # 让 init_db / recover_* 先跑完

    try:
        # 启动补抓：先看看上次成功抓取是什么时候
        async with SessionLocal() as session:
            last = (
                await session.execute(
                    select(HotSnapshot.fetched_at)
                    .where(HotSnapshot.ok.is_(True))
                    .order_by(HotSnapshot.fetched_at.desc())
                    .limit(1)
                )
            ).scalar()
        stale = last is None or (int(time.time()) - last) > interval
        if stale:
            reason = "无历史记录" if last is None else f"距上次已 {(int(time.time()) - last) // 60} 分钟"
            logger.info("[zhihu] 启动补抓（%s）", reason)
            await fetch_hot_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[zhihu] 启动补抓失败：%s", exc)

    while True:
        try:
            await fetch_hot_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[zhihu] 定时抓取异常：%s", exc)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- 自检
async def check() -> dict:
    """凭证连通性自检（给系统状态页用）。用 Limit=1 省额度。"""
    if not _configured():
        return {"ok": False, "configured": False, "error": "未配置 ZHIHU_ACCESS_SECRET"}
    usage = await usage_today()
    raw = await fetch_raw_hot(limit=1)
    async with SessionLocal() as session:
        await _bump_usage(session, 1)
        await session.commit()
    if raw["ok"]:
        async with SessionLocal() as session:
            total = (
                await session.execute(select(func.count(HotSnapshot.id)).where(HotSnapshot.ok.is_(True)))
            ).scalar() or 0
            last = (
                await session.execute(select(HotSnapshot.fetched_at).order_by(HotSnapshot.fetched_at.desc()).limit(1))
            ).scalar()
        return {
            "ok": True,
            "configured": True,
            "snapshots": total,
            "last_fetched_at": last,
            **usage,
        }
    return {"ok": False, "configured": True, "error": raw["error"], **usage}
