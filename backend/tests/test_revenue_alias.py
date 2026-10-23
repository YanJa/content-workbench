"""广告位别名测试：_alias_map / slots 列表 / 批量设置与清除 / 读路径替换。

背景：微信 publisher_adpos_general 对部分广告位（如留言区广告）不返回
ad_slot 枚举，本地兜底成「其他」。别名按 (owner, slot_id) 覆盖 ad_slot_name，
看板与导出同源生效。

约定同 test_skill.py：无 pytest-asyncio，异步用例写成 `def test_x(): asyncio.run(_run())`。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from types import SimpleNamespace

from app.db.session import Base
from app.models.revenue import WechatAdDaily, WechatAdSlotAlias  # noqa: F401
from app.models.settings import WeChatAccount  # noqa: F401
from app.routers import revenue as revenue_router


def _run(coro):
    return asyncio.run(coro)


async def _mk_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _perm():
    return SimpleNamespace(id="u1")


async def _seed(db):
    """一个账号 + 4 个 slot（2 个有枚举、2 个空枚举）的天级数据。"""
    db.add(
        WeChatAccount(
            id="acc1", owner="o1", name="主号", appid="wx1", secret="s1", is_default=1
        )
    )
    rows = [
        # (date, slot_id, ad_slot, ad_slot_name, income_fen)
        ("2026-10-01", 9020229299926746, "SLOT_ID_BIZ_MID_CONTEXT", "文中广告", 1000),
        ("2026-10-01", 72058780271891663, "SLOT_ID_BIZ_BOTTOM", "底部广告", 500),
        ("2026-10-01", 5156210557287366, "", "其他", 145),
        ("2026-10-01", 6133035694593306, "", "其他", 14),
        ("2026-10-02", 5156210557287366, "", "其他", 10),
    ]
    for date, slot_id, ad_slot, name, income in rows:
        db.add(
            WechatAdDaily(
                owner="o1",
                account_id="acc1",
                date=date,
                slot_id=slot_id,
                ad_slot=ad_slot,
                ad_slot_name=name,
                req_succ_count=10,
                exposure_count=5,
                click_count=1,
                income_fen=income,
            )
        )
    await db.flush()


# ---------------------------------------------------------------- 纯函数


def test_row_to_dict_alias_overrides_name():
    r = SimpleNamespace(
        date="2026-10-01",
        slot_id=5156210557287366,
        ad_slot_name="其他",
        req_succ_count=10,
        exposure_count=5,
        click_count=1,
        income_fen=145,
    )
    d = revenue_router._row_to_dict(r, {5156210557287366: "留言区广告"})
    assert d["ad_slot_name"] == "留言区广告"
    # 没有别名时保持原名
    assert revenue_router._row_to_dict(r)["ad_slot_name"] == "其他"


# ---------------------------------------------------------------- 接口


def test_slots_list_groups_by_slot_id(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await _seed(db)
            out = await revenue_router.list_revenue_slots(db=db, owner="o1")
            items = out["items"]
            assert len(items) == 4
            # 按累计收益降序：文中 > 底部 > 5156 > 6133
            assert [i["slot_id"] for i in items][:2] == [9020229299926746, 72058780271891663]
            # 两个空枚举 slot 各自独立成行，不被合并
            unnamed = [i for i in items if not i["ad_slot"]]
            assert len(unnamed) == 2
            # 累计收益 = 145 + 10
            slot_5156 = next(i for i in items if i["slot_id"] == 5156210557287366)
            assert slot_5156["income_fen"] == 155
            assert slot_5156["name"] == "其他"
            assert slot_5156["alias"] == ""
        await engine.dispose()

    _run(go())


def test_put_aliases_upsert_then_clear(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await _seed(db)

            await revenue_router.put_revenue_slot_aliases(
                body=revenue_router.SlotAliasPatch(
                    aliases=[
                        revenue_router.SlotAliasItem(slot_id=5156210557287366, name="留言区广告"),
                        # 空白名等价于不设置
                        revenue_router.SlotAliasItem(slot_id=6133035694593306, name="   "),
                    ]
                ),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            aliases = await revenue_router._alias_map(db, "o1")
            assert aliases == {5156210557287366: "留言区广告"}

            # 读路径替换：收益构成按别名分组
            rows = await revenue_router._collect_rows(
                db, "o1", [SimpleNamespace(id="acc1")], "2026-10-01", "2026-10-02", False
            )
            agg = revenue_router._aggregate(rows)
            slots = {c["slot"]: c["income_fen"] for c in agg["composition"]}
            assert slots == {
                "文中广告": 1000,
                "底部广告": 500,
                "留言区广告": 155,
                "其他": 14,
            }

            # 清空别名 → 恢复原名
            await revenue_router.put_revenue_slot_aliases(
                body=revenue_router.SlotAliasPatch(
                    aliases=[revenue_router.SlotAliasItem(slot_id=5156210557287366, name="")]
                ),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            assert await revenue_router._alias_map(db, "o1") == {}
        await engine.dispose()

    _run(go())


def test_put_aliases_owner_isolated(tmp_path):
    """o1 设的别名不影响 o2。"""
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await revenue_router.put_revenue_slot_aliases(
                body=revenue_router.SlotAliasPatch(
                    aliases=[revenue_router.SlotAliasItem(slot_id=5156210557287366, name="留言区广告")]
                ),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            assert await revenue_router._alias_map(db, "o2") == {}
        await engine.dispose()

    _run(go())
