"""收益看板路由：流量主广告数据的拉取 / 缓存 / 聚合。

数据来源是微信 `publisher/stat`（广告分析接口），按 owner 隔离——
每个用户只看到自己公众号账号（wechat_accounts.owner == 当前 owner）的收益。

三条接口：
  GET /revenue/accounts   当前用户可见的公众号列表（看板顶部下拉用）
  GET /revenue/overview   汇总 + 收益趋势 + 收益构成（支持多账号聚合）
  GET /revenue/settlement 结算口径（累计收入 / 已结算 / 扣除）

缓存策略：
  微信数据 T+1 才出，且按 90 天整段重拉慢又易限频，所以落库。
  overview 默认优先读库（只要该账号在范围内有数据就不打微信）；
  传 refresh=1 强制重拉并 upsert。前端「刷新」按钮走 refresh=1。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.models.revenue import WechatAdDaily
from app.models.settings import WeChatAccount
from app.services.wechat import WeChatClient, slot_name

router = APIRouter(prefix="/revenue", tags=["收益看板"])

# 库里有数据多久内视为「新鲜」、可免打微信（秒）。6 小时足够覆盖 T+1 节奏。
_DB_FRESH_TTL = 6 * 3600


# ---------------------------------------------------------------- 数据访问
async def _list_accounts(db: AsyncSession, owner: str) -> list[WeChatAccount]:
    """当前 owner 名下的公众号（默认号排前）。"""
    stmt = (
        select(WeChatAccount)
        .where(WeChatAccount.owner == owner)
        .order_by(WeChatAccount.is_default.desc(), WeChatAccount.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def _resolve_accounts(
    db: AsyncSession, owner: str, account_id: str | None
) -> list[WeChatAccount]:
    """解析要查哪些账号：指定 id 必须属于当前 owner，否则取其全部账号。"""
    all_acc = await _list_accounts(db, owner)
    if not all_acc:
        raise HTTPException(404, "你还没有配置任何公众号账号，请先到「系统设置 → 公众号」添加")
    if account_id:
        acc = next((a for a in all_acc if a.id == account_id), None)
        if not acc:
            raise HTTPException(404, "公众号不存在或无权限")
        return [acc]
    return all_acc


async def _rows_from_db(
    db: AsyncSession, owner: str, account_ids: list[str], start: str, end: str
) -> list[WechatAdDaily]:
    return list(
        (
            await db.execute(
                select(WechatAdDaily).where(
                    WechatAdDaily.owner == owner,
                    WechatAdDaily.account_id.in_(account_ids),
                    WechatAdDaily.date >= start,
                    WechatAdDaily.date <= end,
                )
            )
        ).scalars().all()
    )


async def _upsert_rows(db: AsyncSession, owner: str, account_id: str, items: list[dict]) -> None:
    """按 (owner, account_id, date, ad_slot) 唯一键 upsert 天级数据。"""
    if not items:
        return
    now = int(time.time())
    rows = [
        {
            "owner": owner,
            "account_id": account_id,
            "date": it.get("date", ""),
            "slot_id": it.get("slot_id") or 0,
            "ad_slot": it.get("ad_slot") or "",
            "ad_slot_name": slot_name(it.get("ad_slot")),
            "req_succ_count": it.get("req_succ_count") or 0,
            "exposure_count": it.get("exposure_count") or 0,
            "exposure_rate": it.get("exposure_rate") or 0.0,
            "click_count": it.get("click_count") or 0,
            "click_rate": it.get("click_rate") or 0.0,
            "income_fen": it.get("income") or 0,
            "ecpm_fen": it.get("ecpm") or 0,
            "created_at": now,
        }
        for it in items
    ]
    stmt = sqlite_insert(WechatAdDaily)
    update_cols = [
        "ad_slot",
        "ad_slot_name",
        "req_succ_count",
        "exposure_count",
        "exposure_rate",
        "click_count",
        "click_rate",
        "income_fen",
        "ecpm_fen",
        "created_at",
    ]
    stmt = stmt.on_conflict_do_update(
        index_elements=["owner", "account_id", "date", "slot_id"],
        set_={c: stmt.excluded[c] for c in update_cols},
    )
    for r in rows:
        await db.execute(stmt.values(**r))
    await db.flush()


async def _collect_rows(
    db: AsyncSession,
    owner: str,
    accounts: list[WeChatAccount],
    start: str,
    end: str,
    refresh: bool,
) -> list[dict]:
    """拿到范围内全部天级数据（dict 列表），优先读库、必要时重拉微信。"""
    account_ids = [a.id for a in accounts]

    if not refresh:
        cached = await _rows_from_db(db, owner, account_ids, start, end)
        if cached:
            return [_row_to_dict(r) for r in cached]

    # 重拉：逐账号打微信、upsert 入库，再读回（或直接用内存里的数据聚合）
    all_items: list[dict] = []
    for acc in accounts:
        client = WeChatClient(appid=acc.appid, secret=acc.secret)
        try:
            items = await client.fetch_adpos_all(start, end)
        except Exception as exc:  # noqa: BLE001
            # 某个号拉失败不影响其它号；把错误带出去让前端可见
            raise HTTPException(502, f"拉取公众号「{acc.name}」广告数据失败：{exc}") from exc
        await _upsert_rows(db, owner, acc.id, items)
        all_items.extend(_dedupe(items))

    if all_items:
        return [
            {
                "date": it.get("date", ""),
                "ad_slot_name": slot_name(it.get("ad_slot")),
                "exposure": it.get("exposure_count") or 0,
                "click": it.get("click_count") or 0,
                "income_fen": it.get("income") or 0,
            }
            for it in all_items
        ]

    # 微信也没数据（新号 / 区间无曝光）：回落读库（可能仍是空）
    cached = await _rows_from_db(db, owner, account_ids, start, end)
    return [_row_to_dict(r) for r in cached]


def _dedupe(items: list[dict]) -> list[dict]:
    """微信接口偶尔返回重复的 (date, slot_id) 行，按唯一键去重，避免重复计收益。"""
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    for it in items:
        key = (it.get("date", ""), it.get("slot_id") or 0)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _row_to_dict(r: WechatAdDaily) -> dict:
    return {
        "date": r.date,
        "ad_slot_name": r.ad_slot_name,
        "exposure": r.exposure_count,
        "click": r.click_count,
        "income_fen": r.income_fen,
    }


# ---------------------------------------------------------------- 聚合
def _aggregate(rows: list[dict]) -> dict:
    """把天级数据聚合成 汇总 / 趋势 / 收益构成。金额单位：分。"""
    exposure = sum(r["exposure"] for r in rows)
    click = sum(r["click"] for r in rows)
    income_fen = sum(r["income_fen"] for r in rows)
    click_rate = round(click / exposure, 6) if exposure else 0.0
    ecpm_fen = round(income_fen / exposure * 1000) if exposure else 0

    # 趋势：按日期
    by_date: dict[str, dict[str, int]] = {}
    for r in rows:
        e = by_date.setdefault(r["date"], {"income_fen": 0, "exposure": 0, "click": 0})
        e["income_fen"] += r["income_fen"]
        e["exposure"] += r["exposure"]
        e["click"] += r["click"]
    trend = sorted(
        (
            {"date": d, "income_fen": v["income_fen"], "exposure": v["exposure"], "click": v["click"]}
            for d, v in by_date.items()
        ),
        key=lambda x: x["date"],
    )

    # 收益构成：按广告位类型
    by_slot: dict[str, dict[str, int]] = {}
    for r in rows:
        s = by_slot.setdefault(r["ad_slot_name"], {"income_fen": 0, "exposure": 0, "click": 0})
        s["income_fen"] += r["income_fen"]
        s["exposure"] += r["exposure"]
        s["click"] += r["click"]
    composition = sorted(
        (
            {"slot": k, "income_fen": v["income_fen"], "exposure": v["exposure"], "click": v["click"]}
            for k, v in by_slot.items()
        ),
        key=lambda x: x["income_fen"],
        reverse=True,
    )

    summary = {
        "income_fen": income_fen,
        "exposure": exposure,
        "click": click,
        "click_rate": click_rate,
        "ecpm_fen": ecpm_fen,
        "rows": len(rows),
    }
    return {"summary": summary, "trend": trend, "composition": composition}


# ---------------------------------------------------------------- 接口
@router.get("/accounts")
async def list_revenue_accounts(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> list[dict]:
    """当前用户可见的公众号列表（看板顶部「全部账号 / 指定号」下拉用）。"""
    return [{"id": a.id, "name": a.name} for a in await _list_accounts(db, owner)]


@router.get("/overview")
async def revenue_overview(
    account_id: str | None = Query(None, description="指定公众号 id；留空=全部账号聚合"),
    start: str = Query(..., description="开始日期 YYYY-MM-DD"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD"),
    refresh: bool = Query(False, description="true=强制重拉微信并刷新缓存"),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """收益看板主接口：汇总指标 + 收益趋势 + 收益构成。

    支持多账号聚合（默认全部），按 owner 隔离。
    """
    accounts = await _resolve_accounts(db, owner, account_id)
    rows = await _collect_rows(db, owner, accounts, start, end, refresh)
    agg = _aggregate(rows)
    return {
        "range": {"start": start, "end": end},
        "account_count": len(accounts),
        "account_ids": [a.id for a in accounts],
        **agg,
    }


@router.get("/settlement")
async def revenue_settlement(
    account_id: str | None = Query(None, description="指定公众号 id；留空=全部账号聚合"),
    start: str = Query(..., description="开始日期 YYYY-MM-DD"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """结算口径：累计收入 / 已结算 / 扣除（来自 publisher_settlement）。

    结算是累计口径，日期区间不影响累计总额，仅用于圈定重合的结算区间明细。
    """
    accounts = await _resolve_accounts(db, owner, account_id)
    items: list[dict] = []
    tot_revenue = tot_settled = tot_penalty = 0
    for acc in accounts:
        client = WeChatClient(appid=acc.appid, secret=acc.secret)
        try:
            data = await client.get_settlement(start, end)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"拉取公众号「{acc.name}」结算数据失败：{exc}") from exc
        rev = data.get("revenue_all", 0) or 0
        settled = data.get("settled_revenue_all", 0) or 0
        penalty = data.get("penalty_all", 0) or 0
        tot_revenue += rev
        tot_settled += settled
        tot_penalty += penalty
        items.append(
            {
                "account_id": acc.id,
                "name": acc.name,
                "revenue_all_fen": rev,
                "settled_revenue_all_fen": settled,
                "penalty_all_fen": penalty,
                "settlement_list": data.get("settlement_list") or [],
            }
        )
    return {
        "range": {"start": start, "end": end},
        "items": items,
        "totals": {
            "revenue_all_fen": tot_revenue,
            "settled_revenue_all_fen": tot_settled,
            "penalty_all_fen": tot_penalty,
        },
    }
