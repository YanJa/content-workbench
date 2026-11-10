"""公众号「贴图」子形态（v1.7.5）。

贴图（俗称小绿书）是微信里的**图片消息**形态：5-6 张图 + 100-400 字短文案，
发布端粘的是纯文本——没有富文本排版、没有 #话题# 文化；但**有草稿箱接口**
（draft/add 的 article_type=newspic，见 services/wechat.py 与 test_wechat_newspic.py）。
它与「图文文章」同属 platform=gzh（发布目的地/统计口径不变），只在
sub_type=tietu 上另立「怎么写」的一档。

本文件锁死这条链路上所有不能靠眼睛看出来的规则：
  1. 子形态归一（normalize_sub_type：tietu 原样、串错回落 article）
  2. 提示词分派（build_system_prompt 按子形态追加贴图段）
  3. 合规分档（check_compliance：贴图 100-400 字 / 0-1 个标签）
  4. 去 AI 味档位（scan 的 tips / PLATFORM_DEFAULT_LEVEL["gzh_tietu"]=heavy）
  5. 排版路由（layout_content 按 sub_type 走文本规整，且队列项 draft→ready）
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # 必须导入，create_all 才看得到所有表
from app.db.session import Base
from app.models.content import Content, QueueItem
from app.prompts.platforms import build_system_prompt, normalize_sub_type
from app.services import humanize as humanize_svc
from app.services import layout as layout_mod
from app.tools.builtin import check_compliance


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- 1. 子形态归一

def test_gzh_tietu_normalization():
    assert normalize_sub_type("gzh", "tietu") == "tietu"
    assert normalize_sub_type("gzh", "article") == "article"
    # 串错成别的平台的形态 → 回落 article（存量内容绝大多数是图文）
    assert normalize_sub_type("gzh", "answer") == "article"
    assert normalize_sub_type("gzh", "idea") == "article"
    # 漏传 / None → 默认图文
    assert normalize_sub_type("gzh", "") == "article"
    assert normalize_sub_type("gzh", None) == "article"
    # 别的平台没有子形态概念
    assert normalize_sub_type("xiaohongshu", "tietu") == ""
    assert normalize_sub_type("douyin", "tietu") == ""
    assert normalize_sub_type("zhihu", "tietu") == "answer"


# ------------------------------------------------------- 2. 提示词分派

def test_prompt_has_tietu_section():
    p = build_system_prompt("gzh", sub_type="tietu")
    # 贴图段的关键口径必须在
    assert "贴图" in p
    assert "20 字" in p  # 首图标题 ≤20 字
    assert "100-400 字" in p  # 正文只讲一个点
    assert "emoji" in p
    # 图文文章的篇幅要求不许混进来（混了模型会按长文写）
    assert "1200-3500" not in p


def test_prompt_article_section_unchanged():
    p = build_system_prompt("gzh", sub_type="article")
    assert "1200-3500" in p
    # 形态段里只有图文模板，没有贴图模板
    assert "【形态：贴图】" not in p


def test_prompt_default_sub_type_is_article():
    """老客户端不传子形态：按默认图文走，行为不退化。"""
    assert build_system_prompt("gzh") == build_system_prompt("gzh", sub_type="article")


# ------------------------------------------------------- 3. 合规分档

def test_compliance_tietu_tier():
    # 贴图 100-400 字合规
    ok_text = "标题\n\n" + "干" * 150
    r = _run(check_compliance("gzh", ok_text, "tietu"))
    assert r["passed"] is True
    assert r["sub_type"] == "tietu"
    # 太长 → 按贴图档报警（不是图文档的 3500）
    long_text = "标题\n\n" + "干" * 500
    r2 = _run(check_compliance("gzh", long_text, "tietu"))
    assert r2["passed"] is False
    assert "400" in r2["issues"][0]["msg"]


def test_compliance_tietu_allows_one_tag():
    """贴图标签可有可无（0-1 个），不堆一排——与小红书的 5-10 个完全不同。

    注意 tag_count 按 # 字符计：微信一个 #话题# 是 2 个 #，所以
    「最多 1 个标签」编码成 (0, 2)。
    """
    body = "标题\n\n" + "干" * 150 + "\n\n#话题#"
    r = _run(check_compliance("gzh", body, "tietu"))
    assert r["passed"] is True
    # 堆 2 个标签（4 个 #）就超贴图档
    body2 = "标题\n\n" + "干" * 150 + "\n\n#甲# #乙#"
    r2 = _run(check_compliance("gzh", body2, "tietu"))
    assert any("话题标签" in i["msg"] for i in r2["issues"])


def test_compliance_article_tier_unchanged():
    """图文档仍是 1200-3500：贴图短文案不得按图文判合格。"""
    r = _run(check_compliance("gzh", "标题\n\n" + "干" * 150, "article"))
    assert r["passed"] is False
    assert "1200" in r["issues"][0]["msg"]


# ------------------------------------------------------- 4. 去 AI 味档位

def test_tietu_humanize_tier():
    # 默认档 heavy（短文案要的就是口语和第一人称）
    assert humanize_svc.PLATFORM_DEFAULT_LEVEL["gzh_tietu"] == "heavy"
    # 复合 key 优先：gzh_tietu 命中自己的 tone，不回落 gzh 的
    assert humanize_svc._pick(humanize_svc._PLATFORM_TONE, "gzh", "tietu").startswith("公众号贴图")
    # 图文回落平台级
    assert humanize_svc._pick(humanize_svc._PLATFORM_TONE, "gzh", "article").startswith("公众号：")
    # 体检 tips 带贴图口径
    tips = humanize_svc._tips("gzh", "tietu")
    assert any("贴图" in t for t in tips)
    # scan 正常返回（走轻量路径，不打模型）
    rep = humanize_svc.scan("今天路过一家煎饼摊，老板说酱要自己熬。", "gzh", "tietu")
    assert rep["ok"] is True
    assert rep["platform"] == "gzh"


# ------------------------------------------------------- 5. 排版路由

class _FakeLLM:
    """假 LLM：记录调用参数，返回固定文本（绝不真调外部 API）。"""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    async def chat(self, messages, task="", usage_sink=None, **kwargs):
        self.calls.append({"task": task, "usage_sink": usage_sink, "messages": messages})
        return SimpleNamespace(content=self.text)


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool
    )
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    return Maker


def test_layout_gzh_tietu_writes_text_and_ready(db_env, monkeypatch):
    """贴图排版：写 layouts.gzh.text（纯文本）+ layout_status=ready + 队列项转 ready。"""
    fake = _FakeLLM("首图标题：煎饼摊的酱要自己熬\n\n今天路过一家煎饼摊，老板说酱要自己熬。")
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)
    Maker = db_env
    cid = uuid.uuid4().hex
    qid = uuid.uuid4().hex

    async def _seed():
        async with Maker() as db:
            db.add(
                Content(
                    id=cid, platform="gzh", sub_type="tietu", kind="text",
                    title="贴图", body="# 草稿\n\n今天路过一家煎饼摊。", owner="u1",
                )
            )
            db.add(QueueItem(id=qid, content_id=cid, platform="gzh", sub_type="tietu", status="draft"))
            await db.commit()

    async def _body():
        async with Maker() as db:
            c = await db.get(Content, cid)
            res = await layout_mod.layout_content(db, c, theme_id="moyu-green", platform="gzh")
            await db.commit()
            return res

    _run(_seed())
    res = _run(_body())

    assert res["ok"] is True
    assert res["theme_name"] == "贴图文本规整"
    assert res["text"].startswith("首图标题")
    # 用量记账：轻任务也必须带 sink（v1.7.4 对账事故的教训）
    assert fake.calls and fake.calls[0]["usage_sink"] is not None

    async def _check():
        async with Maker() as db:
            c = await db.get(Content, cid)
            item = await db.get(QueueItem, qid)
            return c, item

    c, item = _run(_check())
    assert c.layout_status == "ready"
    assert c.layouts["gzh"]["text"].startswith("首图标题")
    assert c.layouts["gzh"]["theme"] == "tietu"
    # 小红书分支当年漏了状态转换，贴图分支不许再漏
    assert item.status == "ready"


def test_layout_gzh_article_not_routed_to_tietu(db_env, monkeypatch):
    """默认图文形态不能被贴图分支截走：走 HTML 排版链路（_llm_layout）。"""
    Maker = db_env
    cid = uuid.uuid4().hex
    hit = {"tietu": False, "llm": False}

    async def _fake_tietu(db, content):
        hit["tietu"] = True
        return {"ok": True}

    async def _fake_llm_layout(markdown, theme_id, manifest, **kwargs):
        hit["llm"] = True
        return "<p>fake</p>", [], []

    async def _seed():
        async with Maker() as db:
            db.add(
                Content(
                    id=cid, platform="gzh", sub_type="article", kind="text",
                    title="图文", body="# 草稿\n\n" + "正文内容。" * 500, owner="u1",
                )
            )
            await db.commit()

    async def _body():
        async with Maker() as db:
            c = await db.get(Content, cid)
            # auto_tags=False：话题标签会另起一次 LLM 调用，本测试只关心路由
            return await layout_mod.layout_content(
                db, c, theme_id="moyu-green", platform="gzh", auto_tags=False
            )

    monkeypatch.setattr(layout_mod, "_layout_gzh_tietu", _fake_tietu)
    monkeypatch.setattr(layout_mod, "_llm_layout", _fake_llm_layout)
    _run(_seed())
    res = _run(_body())

    assert hit["tietu"] is False
    assert hit["llm"] is True
    # 走了 HTML 链路：返回的是主题信息而不是文本规整结果
    assert res["theme"] == "moyu-green"
