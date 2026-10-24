"""知乎平台接入：三形态提示词、子形态归一、发布深链、合规分档。

背景（详见 docs/知乎内容生产调研.md）：知乎官方开放平台**没有发布接口**，
所以系统是「AI 生成 → 人带走 → 人发布」。本文件测的是这条链路里所有
「不能靠眼睛看出来」的规则：子形态怎么归一、深链怎么拼、篇幅按哪档判。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.db.session import Base, get_db
from app.main import app
from app.prompts.platforms import (
    ZHIHU_SUBTYPE_PROMPTS,
    build_system_prompt,
    normalize_sub_type,
)
from app.services.zhihu import extract_question_id, publish_url
from app.tools.builtin import check_compliance

PASSWORD = "Test@2026"


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- 1. 子形态归一

def test_sub_type_only_for_zhihu():
    """非知乎平台一律归空 —— 一个错传的 sub_type 不该污染数据。"""
    assert normalize_sub_type("gzh", "answer") == ""
    assert normalize_sub_type("xiaohongshu", "article") == ""
    assert normalize_sub_type("douyin", "idea") == ""
    assert normalize_sub_type(None, "answer") == ""


def test_zhihu_sub_types_pass_through():
    for st in ("answer", "article", "idea"):
        assert normalize_sub_type("zhihu", st) == st


def test_zhihu_unknown_falls_back_to_answer():
    """未知形态回落 answer：它是主力形态，篇幅区间也最宽容，
    回落过去不至于把一篇长文判成超标。"""
    assert normalize_sub_type("zhihu", "") == "answer"
    assert normalize_sub_type("zhihu", None) == "answer"
    assert normalize_sub_type("zhihu", "专栏") == "answer"


# ------------------------------------------------------- 2. 提示词拼装

def test_prompt_has_no_platform_block_when_unknown():
    p = build_system_prompt("weibo")
    assert "知乎" not in p


@pytest.mark.parametrize("st,kw", [
    ("answer", "结论先行"),
    ("article", "专栏长文"),
    ("idea", "50-300 字"),
])
def test_prompt_carries_sub_type_rules(st, kw):
    p = build_system_prompt("zhihu", topic="怎么选第一份工作", audience="应届生", sub_type=st)
    assert kw in p
    assert "主题：怎么选第一份工作" in p
    assert "目标读者：应届生" in p


def test_prompt_omits_empty_topic_and_audience():
    """空值整行省略 —— 留着「主题：」这种空行会被模型当成占位符照抄。"""
    p = build_system_prompt("zhihu", sub_type="idea")
    assert "主题：\n" not in p
    assert "目标读者：\n" not in p


def test_three_sub_types_are_distinct():
    """三种形态必须是三套要求，不能是同一份文案换个名字。"""
    a = build_system_prompt("zhihu", sub_type="answer")
    b = build_system_prompt("zhihu", sub_type="article")
    c = build_system_prompt("zhihu", sub_type="idea")
    assert len({a, b, c}) == 3
    assert len(ZHIHU_SUBTYPE_PROMPTS) == 3


def test_prompt_increment_requirement():
    """回答必须写「比头部回答多了什么」—— 这是知乎回答唯一的核心纪律。"""
    assert "增量" in build_system_prompt("zhihu", sub_type="answer")


# ------------------------------------------------------- 3. 发布深链

@pytest.mark.parametrize("value,expect", [
    ("https://www.zhihu.com/question/1234567890", "1234567890"),
    ("https://www.zhihu.com/question/1234567890?utm_source=hot&utm_medium=x", "1234567890"),
    ("https://www.zhihu.com/question/123/answer/456", "123"),
    ("1234567890", "1234567890"),  # 用户直接粘 id
    ("", ""),
    ("随便一段不是链接的话", ""),
    ("https://zhuanlan.zhihu.com/p/111", ""),  # 文章链接不该被当成问题 id
])
def test_extract_question_id(value, expect):
    assert extract_question_id(value) == expect


def test_publish_url_answer_with_question():
    """回答 + 已知问题 → 直达问题页，省得用户自己搜。"""
    assert publish_url("answer", "https://www.zhihu.com/question/123") == (
        "https://www.zhihu.com/question/123"
    )


def test_publish_url_answer_without_question():
    """没挂问题时去创作中心的回答管理页，别跳首页。"""
    assert publish_url("answer", "").startswith("https://www.zhihu.com/creator")


def test_publish_url_article_and_idea():
    assert publish_url("article") == "https://zhuanlan.zhihu.com/write"
    assert publish_url("idea").startswith("https://www.zhihu.com/creator")


def test_publish_url_unknown_sub_type_is_safe():
    """未知形态也要给出一个能用的入口，不能返回空串让按钮消失。"""
    assert publish_url("乱写").startswith("https://www.zhihu.com")


# ------------------------------------------------------- 4. 合规分档

def test_compliance_answer_band():
    """同一份 1000 字的稿子：回答合格，文章太短，想法太长。"""
    body = "字" * 1000
    assert _run(check_compliance("zhihu", body, "answer"))["passed"] is True
    assert _run(check_compliance("zhihu", body, "article"))["passed"] is False
    assert _run(check_compliance("zhihu", body, "idea"))["passed"] is False


def test_compliance_idea_band():
    # 想法要带 1-3 个话题标签，所以正文里必须有 #
    r = _run(check_compliance("zhihu", "字" * 120 + "\n#职场 #成长", "idea"))
    assert r["passed"] is True


def test_compliance_idea_requires_tags():
    """想法不带话题标签要提醒 —— 想法主要靠话题分发。"""
    r = _run(check_compliance("zhihu", "字" * 120, "idea"))
    assert r["passed"] is False
    assert any("话题标签" in i["msg"] for i in r["issues"])


def test_compliance_catches_interaction_begging():
    r = _run(check_compliance("zhihu", "字" * 700 + "\n希望对你有帮助，点赞收藏", "answer"))
    assert r["passed"] is False
    assert any("套话" in i["msg"] for i in r["issues"])


def test_compliance_catches_emoji_in_answer():
    r = _run(check_compliance("zhihu", "字" * 700 + "🔥🔥🔥", "answer"))
    assert r["passed"] is False
    assert any("emoji" in i["msg"] for i in r["issues"])


def test_compliance_tolerates_few_emoji_in_idea():
    """想法允许少量 emoji 做节奏 —— 一刀切禁掉会把想法写死。"""
    r = _run(check_compliance("zhihu", "字" * 80 + "🔥\n#职场", "idea"))
    assert r["passed"] is True


def test_compliance_catches_ba_gu_connectives():
    r = _run(check_compliance("zhihu", "字" * 300 + "首先如何，其次如何", "answer"))
    assert any("八股" in i["msg"] for i in r["issues"])


def test_other_platforms_unaffected():
    """老平台的行为不能因为加了知乎而变。"""
    r = _run(check_compliance("gzh", "字" * 1500))
    assert r["sub_type"] == ""


# ------------------------------------------------------- 5. 入队与队列返回

def _mk_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _override_db(engine, Maker):
    async def _get():
        # 与真实 get_db 保持一致（含 commit）：入队只 flush 不 commit，
        # override 少了 commit 就等于什么都没存下来。
        async with Maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db] = _get


def _register(client) -> dict[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"username": f"u{uuid.uuid4().hex[:10]}", "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_enqueue_zhihu_answer_carries_sub_type_and_deep_link():
    """入队时把形态和归属问题带上，队列列表要能直接给出「去哪儿发」。"""
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    asyncio.run(_create_all(engine))
    client = TestClient(app)
    headers = _register(client)

    r = client.post(
        "/api/content/items",
        json={
            "platform": "zhihu",
            "sub_type": "answer",
            "title": "第一份工作怎么选",
            "body": "# 先选行业再选公司\n正文…",
            "zhihu_question": "https://www.zhihu.com/question/1234567890?utm_source=hot",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text

    items = client.get("/api/queue/items", headers=headers).json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["platform"] == "zhihu"
    assert it["sub_type"] == "answer"
    # 深链直达具体问题的「写回答」页
    assert it["publish_url"] == "https://www.zhihu.com/question/1234567890"

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def test_enqueue_non_zhihu_ignores_sub_type_and_link():
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    asyncio.run(_create_all(engine))
    client = TestClient(app)
    headers = _register(client)

    r = client.post(
        "/api/content/items",
        json={"platform": "gzh", "sub_type": "answer", "title": "标题", "body": "正文"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    it = client.get("/api/queue/items", headers=headers).json()["items"][0]
    assert it["sub_type"] == ""
    assert it["publish_url"] == ""

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def test_enqueue_zhihu_without_sub_type_defaults_to_answer():
    """老客户端不传 sub_type 也要能用 —— 回落 answer，不是 400。"""
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    asyncio.run(_create_all(engine))
    client = TestClient(app)
    headers = _register(client)

    r = client.post(
        "/api/content/items",
        json={"platform": "zhihu", "title": "标题", "body": "正文"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    it = client.get("/api/queue/items", headers=headers).json()["items"][0]
    assert it["sub_type"] == "answer"
    # 没有归属问题 → 创作中心的回答入口
    assert it["publish_url"].startswith("https://www.zhihu.com/creator")

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


async def _create_all(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
