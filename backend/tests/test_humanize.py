"""人工痕迹体检与「加痕迹」改写。

这个文件最重要的两条用例是 `test_scan_separates_ai_from_human` 与
`test_verify_facts_catches_lost_numbers`：

- 前者是**判别力回归**。规则表以后一定会被人改（加词、调权重），没有这条用例，
  某次「顺手加两个词」就可能把人类正常写作也判成机器味——那就成了冤枉人的工具。
- 后者守的是改写功能的底线：改稿子可以，改数字不行。悄悄把「提升 37%」改没，
  比不改更糟，因为用户会拿着错数据发出去。
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
from app.services.humanize import (
    LEVELS,
    PLATFORM_DEFAULT_LEVEL,
    build_humanize_messages,
    clean_llm_output,
    scan,
    verify_facts,
)

PASSWORD = "Test@2026"

AI_TEXT = """随着人工智能技术的不断发展，内容创作行业正在经历深刻的变革。
首先，AI 写作工具显著提升了创作效率；其次，它有效降低了内容生产的门槛；
最后，它为创作者赋能，打造了全新的内容生态。值得注意的是，这不仅是技术的进步，
更是生产力的飞跃。

在数字化转型的背景下，创作者需要把握这一趋势，深耕垂直赛道，
实现内容价值的全方位提升。不可否认，AI 与人类的深度融合将持续推动行业再上新台阶。

综上所述，AI 写作工具为内容创作带来了显著的便利。希望以上内容对你有帮助，
如果你还有疑问，欢迎在评论区留言交流。"""

HUMAN_TEXT = """我第一次用 AI 写稿是去年 11 月，交上去被编辑打回来了。
她说"读起来像机器写的"，我盯着那句话看了很久，因为我自己读着觉得挺顺的。

后来我才发现，问题就出在"太顺"了。每段都是三句话，每句都是二十来个字，
没有一个短句，没有一个具体的人名。编辑说：你写的东西里没有一个人。

我改了三次。最后交的那一版，我加了一句"当时我在地铁上改的稿，坐过了两站"。
她回了个"过了"。就加了这一句。

所以我现在写东西有个笨办法：写完自己念一遍，念着别扭的地方就对了，
那地方就是人味。念着特别顺的地方，反而要小心。"""


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- 1. 扫描判别力

def test_scan_separates_ai_from_human():
    """核心回归：AI 腔必须得低分、人写的必须得高分，且差距要足够大。"""
    ai = scan(AI_TEXT, "gzh")
    human = scan(HUMAN_TEXT, "gzh")
    assert ai["score"] <= 30, ai["score"]
    assert human["score"] >= 75, human["score"]
    assert human["score"] - ai["score"] >= 40


def test_ai_text_hits_the_obvious_tells():
    """AI 稿该命中的几条必须命中：连接词、宏大词、助手残留、无细节。"""
    r = scan(AI_TEXT, "gzh")
    keys = {h["key"] for h in r["hits"]}
    assert "connector" in keys
    assert "buzzword" in keys
    assert "chat_residue" in keys
    assert "gzh_no_detail" in keys
    # 句长节奏与短句占比是中文最强的两个统计信号，AI 稿必然全中
    assert "len_cv" in keys
    assert "short_sent" in keys


def test_human_text_is_not_punished_for_writing_naturally():
    """人写稿可以有小毛病，但不能被判成「机器味」。"""
    r = scan(HUMAN_TEXT, "gzh")
    assert r["level"] in ("natural", "slight")
    # 统计层不能冤枉它：人类句长 CV 约 0.52、短句占比约 25%，这篇实测应当达标
    assert r["stats"]["sent_len_cv"] >= 0.45
    assert r["stats"]["short_sent_ratio"] >= 0.15


def test_scan_reports_where_the_problem_is():
    """每条命中都要带原文片段 —— 没有位置信息的报告等于没说。"""
    r = scan(AI_TEXT, "gzh")
    for h in r["hits"]:
        assert h["how"], f"{h['key']} 没给改法"
    residue = next(h for h in r["hits"] if h["key"] == "chat_residue")
    assert any("希望" in s for s in residue["samples"])


# ------------------------------------------------------- 2. 样本与平台

def test_short_sample_does_not_fake_statistics():
    """短文不硬算统计分：检测器在短文上误判率本来就高，装精确就是骗人。"""
    r = scan("首先，这个方案很好。其次，它很便宜。", "gzh")
    assert r["short_sample"] is True
    assert "未参与计分" in r["note"]
    assert "sent_len_cv" not in r["stats"]


def test_normal_sample_says_it_is_not_a_verdict():
    r = scan(AI_TEXT, "gzh")
    assert r["short_sample"] is False
    assert "不是「是否 AI 生成」的判定" in r["note"]


def test_empty_text_is_rejected_cleanly():
    r = scan("", "gzh")
    assert r["ok"] is False
    assert r["score"] == 0


def test_xiaohongshu_wants_first_person_and_emoji():
    """平台差异：同样的稿子在小红书会因为「没我、没 emoji」被扣分。"""
    gzh = scan(HUMAN_TEXT, "gzh")
    xhs = scan(HUMAN_TEXT, "xiaohongshu")
    assert xhs["score"] < gzh["score"]
    assert any(h["key"] in ("xhs_no_self", "xhs_no_emoji") for h in xhs["hits"])


def test_zhihu_punishes_interaction_bait_harder():
    """同一段求互动套话，在知乎扣得比公众号狠。

    文本里必须有数字 —— 否则公众号的「通篇没有具体数字」规则会一起命中，
    两个平台的差就不是「对套话的态度」了，测不出想测的东西。
    """
    bait = ("这个方案到底好不好？它帮我省了 30% 的时间。"
            "欢迎点赞关注收藏，你怎么看呢？\n" + "内容，一共 3 个步骤。" * 30)
    zhihu = scan(bait, "zhihu")
    gzh = scan(bait, "gzh")
    assert zhihu["score"] < gzh["score"]
    assert any(h["key"] == "interaction_bait" and h["severity"] == "high"
               for h in zhihu["hits"])
    assert any(h["key"] == "interaction_bait" and h["severity"] == "low"
               for h in gzh["hits"])


def test_html_is_stripped_before_scanning():
    """传进来的可能是排版后的 HTML，标签不能污染统计。"""
    html = "<h2>标题</h2><p>这是一段正常的正文内容，用来测试标签剥离。</p>"
    assert "<h2>" not in scan(html, "gzh")["hits"][0]["samples"][0]


# ------------------------------------------------------- 3. 事实校验

def test_verify_facts_catches_lost_numbers():
    before = "转化率提升了 37%，成本从 1200 元降到 480 元，用了 3 个月。"
    after = "转化率提升了很多，成本下降明显，用了三个月。"
    w = verify_facts(before, after)
    assert w and any("数字" in x for x in w)


def test_verify_facts_keeps_quiet_when_nothing_lost():
    before = "转化率提升了 37%，用了 3 个月。"
    after = "转化率提升了 37% —— 前后折腾了 3 个月。"
    assert verify_facts(before, after) == []


def test_verify_facts_catches_lost_links():
    before = "详见 https://example.com/a/b 与 https://example.org/c"
    after = "详见文末链接。"
    assert any("链接" in x for x in verify_facts(before, after))


# ------------------------------------------------------- 4. 改写装配

def test_platform_default_levels_are_sane():
    """知乎要克制、小红书要放得开 —— 反过来就错了。"""
    assert PLATFORM_DEFAULT_LEVEL["zhihu"] == "light"
    assert PLATFORM_DEFAULT_LEVEL["xiaohongshu"] == "heavy"
    assert PLATFORM_DEFAULT_LEVEL["gzh"] == "medium"
    assert set(LEVELS) == {"light", "medium", "heavy"}


def test_unknown_level_falls_back_to_platform_default():
    msgs = build_humanize_messages("正文", "zhihu", "瞎写的档位")
    assert "轻度" in msgs[0]["content"]


def test_messages_carry_scan_findings():
    """体检发现的问题要喂给模型 —— 具体指令比通用指令有效得多。"""
    hits = scan(AI_TEXT, "gzh")["hits"]
    user = build_humanize_messages(AI_TEXT, "gzh", "medium", hits)[1]["content"]
    assert "体检发现的问题" in user
    assert "机械连接词" in user


def test_clean_llm_output_strips_fences_and_preamble():
    assert clean_llm_output("```markdown\n正文内容\n```") == "正文内容"
    assert clean_llm_output("改写如下：\n正文内容") == "正文内容"
    assert clean_llm_output("正文内容") == "正文内容"


def test_build_messages_forbids_new_facts():
    """提示词里必须有「不许编造事实」这条铁律，否则模型会顺手补数据。"""
    system = build_humanize_messages("正文", "gzh", "medium")[0]["content"]
    assert "不许新增事实" in system
    assert "一个数字都不能改" in system


# ------------------------------------------------------- 5. 接口

def _mk_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _override_db(engine, Maker):
    async def _get():
        # 与真实 get_db 一致（含 commit），见开发指南坑 67
        async with Maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db] = _get


def _create_all(engine):
    async def _go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    return _go()


def _register(client) -> dict[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"username": f"u{uuid.uuid4().hex[:10]}", "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def api():
    """一套独立的内存库 + 已登录客户端。

    为什么每个接口用例都要换库：不 override get_db 的话请求会打到**开发库**，
    那里已经有 admin 用户了，注册就不再是「首用户放行」而要走邀请码 → 403。
    看起来是鉴权问题，其实是「测试没隔离」。
    """
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    _run(_create_all(engine))
    client = TestClient(app)
    headers = _register(client)
    yield client, headers
    app.dependency_overrides.clear()
    _run(engine.dispose())


def test_scan_endpoint_is_free_and_sync(api):
    """扫描是零 LLM 的：不提交任务、不返回 task_id，直接给结果。"""
    client, headers = api
    r = client.post("/api/humanize/scan", json={"text": AI_TEXT, "platform": "gzh"},
                    headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["score"] <= 30
    assert "task_id" not in body


def test_scan_endpoint_rejects_empty_text(api):
    client, headers = api
    r = client.post("/api/humanize/scan", json={"text": "   ", "platform": "gzh"},
                    headers=headers)
    assert r.status_code == 400


def test_rewrite_and_apply_roundtrip(api):
    """提交改写 → 拿结果 → 写回源文。写回后排版产物必须失效。"""
    client, headers = api

    r = client.post(
        "/api/content/items",
        json={"platform": "gzh", "title": "测试稿", "body": AI_TEXT},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    cid = r.json()["content_id"]

    # 扫一次源文（走 content_id 分支）
    r = client.post("/api/humanize/scan", json={"content_id": cid}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["score"] <= 30

    # 提交改写：真实调 LLM 太贵，这里只验证「秒回 task_id + 任务登记成功」
    r = client.post("/api/humanize/rewrite",
                    json={"content_id": cid, "platform": "gzh", "level": "medium"},
                    headers=headers)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "queued"
    task_id = job["task_id"]

    jr = client.get(f"/api/humanize/job/{task_id}", headers=headers)
    assert jr.status_code == 200, jr.text
    assert jr.json()["content_id"] == cid

    # 写回：用一份「更像人写的」正文，验证排版产物被清掉
    r = client.post("/api/humanize/apply",
                    json={"content_id": cid, "text": HUMAN_TEXT}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["score"] >= 75

    detail = client.get(f"/api/content/items/{cid}", headers=headers).json()
    assert "地铁" in (detail.get("body") or "")
    # 排版产物失效，需要重排（保留旧 HTML 会造成面板与源文不一致）
    assert not (detail.get("body_html") or "")
    assert detail.get("layout_status") == "draft"


def test_rewrite_rejects_missing_content(api):
    client, headers = api
    r = client.post("/api/humanize/rewrite",
                    json={"content_id": "not-exist", "platform": "gzh"}, headers=headers)
    assert r.status_code == 404


def test_job_lookup_404_for_unknown_task(api):
    client, headers = api
    assert client.get("/api/humanize/job/nope", headers=headers).status_code == 404


def test_levels_endpoint_lists_all_tiers(api):
    client, headers = api
    r = client.get("/api/humanize/levels", headers=headers)
    assert r.status_code == 200, r.text
    assert {x["id"] for x in r.json()["levels"]} == {"light", "medium", "heavy"}
    assert r.json()["platform_default"]["zhihu"] == "light"


def test_endpoints_require_auth():
    """新端点一个都不能是裸的（全站守卫测试会扫，这里再钉一次）。"""
    client = TestClient(app)
    assert client.post("/api/humanize/scan", json={"text": AI_TEXT}).status_code in (401, 403)
    assert client.get("/api/humanize/job/x").status_code in (401, 403)
    assert client.get("/api/humanize/levels").status_code in (401, 403)
