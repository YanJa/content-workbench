"""公众号贴图：一键排版时调用「起标题」技能（v1.10.11）。

用户上传的 wechat-hot-title 技能是给「对话里让人挑标题」写的（要求一次给
5 个候选 + 每个说明理由），而排版链路只要一行能直接用的首图标题。这里锁死：
  1. 技能正文确实被注入排版 system 指令，且带覆盖规则
  2. 覆盖规则压住「多候选 / 解释」，仍要求单行纯文本、≤20 字、不标题党
  3. 按 owner 激活：别人的技能读不到；停用 / invalid / 不存在 / 抛错
     都静默回落默认规则，排版照常成功
  4. 走 skill.activate 语义：use_count 会涨，技能页能看到被调用
  5. 不额外增加模型调用次数（一次排版仍是一次 chat）
  6. layouts.gzh.title_skill 记录本次用的技能名；首行标题成为草稿 title
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # create_all 要看到所有表
from app.db.session import Base
from app.models.content import Content, QueueItem
from app.models.skill import Skill
from app.services import layout as layout_mod

SKILL_NAME = "wechat-hot-title"
SKILL_BODY = """# 公众号爆款标题

## 结构
- 用具体数字开头（3 个 / 90% / 第 2 天），不要用「一些」「几个」
- 前 10 个字必须出现利益点或冲突点，因为列表页只露这么多
- 长度控制在 16-24 字，手机上不超过两行

## 语气
- 说人话，不用「赋能」「闭环」「抓手」这类词
- 可以有情绪，但不能标题党到与正文不符

## 输出
一次给 5 个候选，每个后面用一句话说明它靠什么吸引点击。
不要解释方法本身，直接给标题。
"""


def _run(coro):
    return asyncio.run(coro)


class _FakeLLM:
    """假 LLM：记录调用参数，返回固定文本，绝不真调外部 API。"""

    def __init__(self, text="3 个细节看出酱是不是自己熬\n\n今天路过一家煎饼摊。\n\n- 💡 酱要自己熬"):
        self.text = text
        self.calls: list[dict] = []

    async def chat(self, messages, task="", usage_sink=None, **kwargs):
        self.calls.append({"task": task, "usage_sink": usage_sink, "messages": messages})
        return SimpleNamespace(content=self.text)


@pytest.fixture()
def db_env(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 't.db'}", poolclass=NullPool
    )
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    return Maker


def _add_skill(Maker, *, owner="u1", name=SKILL_NAME, body=SKILL_BODY,
               enabled=True, status="ready"):
    async def go():
        async with Maker() as db:
            db.add(
                Skill(
                    owner=owner, name=name, description="起公众号爆款标题",
                    body=body, enabled=enabled, status=status, use_count=0,
                )
            )
            await db.commit()

    _run(go())


def _seed_content(Maker, *, owner="u1"):
    cid, qid = uuid.uuid4().hex, uuid.uuid4().hex

    async def go():
        async with Maker() as db:
            db.add(
                Content(
                    id=cid, platform="gzh", sub_type="tietu", kind="text",
                    title="贴图", body="# 草稿\n\n今天路过一家煎饼摊，老板说酱要自己熬。",
                    owner=owner,
                )
            )
            db.add(
                QueueItem(
                    id=qid, content_id=cid, platform="gzh", sub_type="tietu",
                    status="draft", owner=owner,
                )
            )
            await db.commit()

    _run(go())
    return cid


def _layout(Maker, cid, fake):
    async def go():
        async with Maker() as db:
            c = await db.get(Content, cid)
            res = await layout_mod.layout_content(db, c, theme_id="moyu-green", platform="gzh")
            await db.commit()
            return res

    return _run(go())


def _system(fake):
    assert fake.calls, "排版没有调用模型"
    return fake.calls[0]["messages"][0]["content"]


def _read_back(Maker, cid):
    async def go():
        async with Maker() as db:
            c = await db.get(Content, cid)
            s = (
                await db.execute(select(Skill).where(Skill.name == SKILL_NAME))
            ).scalar_one_or_none()
            return c.layouts["gzh"], (s.use_count if s else None)

    return _run(go())


# ---------------------------------------------------------------- 注入
def test_title_skill_injected_with_override_rules(db_env, monkeypatch):
    _add_skill(db_env)
    cid = _seed_content(db_env)
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)

    res = _layout(db_env, cid, fake)
    assert res["ok"] is True, res
    sys = _system(fake)
    # 技能正文真的进去了
    assert "用具体数字开头" in sys
    assert f"来自技能 {SKILL_NAME}" in sys
    # 覆盖规则：把「给 5 个候选」改成只吐一行，且守住长度与不标题党
    assert "本场景覆盖规则" in sys
    assert "只把最好的那一个写成第一行" in sys
    assert "不超过 20 字" in sys
    assert "不做震惊体" in sys
    # 原有排版要求没被挤掉
    assert "首图标题" in sys and "3-5 个短点" in sys
    # 关键：一次排版仍只调一次模型，不额外花钱
    assert len(fake.calls) == 1


def test_title_skill_records_name_and_bumps_use_count(db_env, monkeypatch):
    """走 skill.activate 语义：技能页能看到它被调用过。"""
    _add_skill(db_env)
    cid = _seed_content(db_env)
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)

    _layout(db_env, cid, fake)
    gzh, use_count = _read_back(db_env, cid)
    assert gzh["title_skill"] == SKILL_NAME
    assert use_count == 1


def test_generated_title_becomes_newspic_draft_title(db_env, monkeypatch):
    """首行标题经 split_newspic_text 后成为推草稿时的 title。"""
    from app.services.wechat import split_newspic_text

    _add_skill(db_env)
    cid = _seed_content(db_env)
    fake = _FakeLLM(text="3 个细节看出酱是不是自己熬\n\n今天路过一家煎饼摊。")
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)

    _layout(db_env, cid, fake)
    gzh, _ = _read_back(db_env, cid)
    title, body = split_newspic_text(gzh["text"], "兜底标题")
    assert title == "3 个细节看出酱是不是自己熬"
    assert body.startswith("今天路过")
    assert len(title) <= 32, "微信 newspic title 上限 32 字"


# ---------------------------------------------------------------- 回落（不能阻断排版）
def test_other_owner_skill_is_not_used(db_env, monkeypatch):
    _add_skill(db_env, owner="someone-else")
    cid = _seed_content(db_env, owner="u1")
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)

    res = _layout(db_env, cid, fake)
    assert res["ok"] is True, res
    sys = _system(fake)
    assert "用具体数字开头" not in sys
    assert "本场景覆盖规则" not in sys
    assert "首图标题" in sys


@pytest.mark.parametrize("enabled,status", [(False, "ready"), (True, "invalid")])
def test_disabled_or_invalid_skill_falls_back(db_env, monkeypatch, enabled, status):
    _add_skill(db_env, enabled=enabled, status=status)
    cid = _seed_content(db_env)
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)

    res = _layout(db_env, cid, fake)
    assert res["ok"] is True, res
    sys = _system(fake)
    assert "本场景覆盖规则" not in sys
    assert "首图标题" in sys


def test_missing_skill_falls_back(db_env, monkeypatch):
    """没传技能时行为与旧版完全一致。"""
    cid = _seed_content(db_env)
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)

    res = _layout(db_env, cid, fake)
    assert res["ok"] is True, res
    sys = _system(fake)
    assert "本场景覆盖规则" not in sys
    assert "首图标题" in sys


def test_activate_exception_never_breaks_layout(db_env, monkeypatch):
    _add_skill(db_env)
    cid = _seed_content(db_env)
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)
    monkeypatch.setattr(
        layout_mod.skill_svc, "activate",
        AsyncMock(side_effect=RuntimeError("skill db down")),
    )

    res = _layout(db_env, cid, fake)
    assert res["ok"] is True, res
    assert "本场景覆盖规则" not in _system(fake)


def test_config_can_disable_the_whole_feature(db_env, monkeypatch):
    """GZH_TIETU_TITLE_SKILL 留空 = 关闭，不查技能库。"""
    _add_skill(db_env)
    cid = _seed_content(db_env)
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)
    monkeypatch.setattr(layout_mod.settings, "GZH_TIETU_TITLE_SKILL", "")
    spy = AsyncMock(wraps=layout_mod.skill_svc.activate)
    monkeypatch.setattr(layout_mod.skill_svc, "activate", spy)

    res = _layout(db_env, cid, fake)
    assert res["ok"] is True, res
    assert spy.await_count == 0
    assert "本场景覆盖规则" not in _system(fake)


def test_long_skill_body_is_capped(db_env, monkeypatch):
    """技能正文再长也不能把排版 prompt 撑爆。"""
    _add_skill(db_env, body="标" * 10000)
    cid = _seed_content(db_env)
    fake = _FakeLLM()
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)

    res = _layout(db_env, cid, fake)
    assert res["ok"] is True, res
    sys = _system(fake)
    # 技能正文那一整段最多 4000 字，连续 4001 个「标」不该出现。
    # 不能用 sys.count("标") 直接比：基础指令里的「首图标题」也含这个字。
    assert "标" * 4001 not in sys, "技能正文没被截断"
    assert sys.count("标") < layout_mod._TIETU_TITLE_SKILL_BODY_MAX + 50
    assert "本场景覆盖规则" in sys
