"""知乎直达（zhihu_zhida）工具：注册、直答调用、未配置凭证降级。

背景：用户安装了一个「知乎直达(zhida)」技能，但它是"脚本型"的——正文要求
运行 zhida.py 去调知乎直答，而对话模型只有 activate_skill 一个工具，没有
"执行脚本/调外部 HTTP"的能力，所以装了也用不了。v1.7.3 把"知乎直答"做成
对话模型能主动调用的真实工具 zhihu_zhida，并把 zhida 技能的 description 改成
引导模型激活它、再调该工具。本文件守住这条链路。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# 导入即触发工具注册（registry 是模块级单例，@tool 装饰器在 import 时执行）
from app.tools import builtin  # noqa: F401
from app.tools.registry import registry
from app.services import skill as skill_svc
from app.services import zhihu as zhihu_svc
from app.models.skill import Skill


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 注册
def test_zhihu_zhida_registered():
    """工具已注册到 registry（main.py 导入 builtin 即触发）。"""
    assert registry.get("zhihu_zhida") is not None


def test_zhihu_zhida_not_requires_confirm():
    """只读查询，不属危险操作，无需人工确认。"""
    assert registry.get("zhihu_zhida").requires_confirm is False


def test_zhihu_zhida_in_openai_schema():
    """会出现在发给模型的 tools 列表里。"""
    names = [s["function"]["name"] for s in registry.schemas()]
    assert "zhihu_zhida" in names


# ---------------------------------------------------------------- 底层直答
async def _a_unconfigured():
    with patch.object(zhihu_svc, "_configured", return_value=False):
        res = await zhihu_svc.zhida_query("什么是 rave 文化")
    assert res["ok"] is False
    assert "未配置" in res["error"]
    assert "hint" in res  # 给用户的配置指引


def test_zhida_query_unconfigured():
    """未配置 ZHIHU_ACCESS_SECRET 时返回明确错误，而不是假装成功。"""
    _run(_a_unconfigured())


async def _a_success():
    fake = MagicMock()
    fake.status_code = 200
    fake.content = b"{}"
    fake.json.return_value = {
        "model": "zhida-fast-1p5",
        "choices": [{"message": {"content": "知乎直答内容", "reasoning_content": ""}}],
    }
    inst = AsyncMock()
    inst.__aenter__.return_value = inst
    inst.post.return_value = fake
    with patch.object(zhihu_svc, "_configured", return_value=True), \
         patch("httpx.AsyncClient", return_value=inst):
        res = await zhihu_svc.zhida_query("rave 文化是什么")
    assert res["ok"] is True
    assert res["answer"] == "知乎直答内容"


def test_zhida_query_success():
    """凭证就绪时正常解析 OpenAI 兼容返回值。"""
    _run(_a_success())


# ---------------------------------------------------------------- 工具层
async def _a_tool_unconfigured():
    # 显式让凭证未就绪，隔离「工具层透传底层未配置错误」的行为，
    # 不依赖运行时 settings.ZHIHU_ACCESS_SECRET 是否为空。
    with patch.object(zhihu_svc, "_configured", return_value=False):
        res = await builtin.zhihu_zhida(query="rave 文化是什么")
    assert res["ok"] is False
    assert "未配置" in res["error"]


def test_zhihu_zhida_tool_unconfigured():
    """工具层把底层「未配置」错误原样透传给模型，模型据此提示用户配凭证。"""
    _run(_a_tool_unconfigured())


async def _a_tool_success():
    with patch.object(
        zhihu_svc, "zhida_query",
        new=AsyncMock(return_value={"ok": True, "answer": "知乎答案", "model": "zhida-fast-1p5"}),
    ):
        res = await builtin.zhihu_zhida(query="rave 文化是什么")
    assert res["ok"] is True
    assert res["answer"] == "知乎答案"
    assert "知乎" in res["note"]


def test_zhihu_zhida_tool_success():
    """工具层成功时透传 answer，并附「来源是知乎」的说明。"""
    _run(_a_tool_success())


# ---------------------------------------------------------------- catalog 联动
def test_catalog_mentions_zhida_tool():
    """zhida 技能改版后，catalog 里的新描述应引导模型「查知乎→调 zhihu_zhida」。"""
    s = Skill(
        name="zhida",
        description="当用户想就某问题查询知乎直答时激活本技能，再调用 zhihu_zhida 工具",
    )
    cat = skill_svc.build_catalog([s])
    assert "zhida" in cat
    assert "zhihu_zhida" in cat
