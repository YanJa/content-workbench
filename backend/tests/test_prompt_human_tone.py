"""默认「人味写作」基调注入回归。

v1.8.0 起，system prompt 默认携带「写作语气（像真人写）」段，把 humanize-pack
的句式原则前置为生成基调——这是「源头去 AI 味」而非事后改写。这段必须在
「工具使用纪律」标记之前，否则无工具的供应商模型会被 strip_tool_discipline
砍掉，人味指示也就失效了。
"""
from __future__ import annotations

from app.prompts.platforms import (
    BASE_SYSTEM,
    build_system_prompt,
    strip_tool_discipline,
)


def test_base_system_carries_human_tone():
    assert "长短句交错" in BASE_SYSTEM
    assert "赋能 / 抓手 / 闭环" in BASE_SYSTEM  # 宏大词例句之一


def test_human_tone_before_tool_discipline_mark():
    tone = BASE_SYSTEM.find("长短句交错")
    mark = BASE_SYSTEM.find("工具使用纪律")
    assert tone != -1 and mark != -1
    assert tone < mark  # 人味段在纪律段之前，不会被 strip 剥掉


def test_every_platform_includes_human_tone():
    for platform in ("gzh", "xiaohongshu", "douyin", "zhihu"):
        sp = build_system_prompt(platform, "主题", "读者")
        assert "长短句交错" in sp, f"{platform} 缺人味段"


def test_default_no_platform_includes_human_tone():
    sp = build_system_prompt(None)
    assert "长短句交错" in sp


def test_human_tone_survives_tool_discipline_strip():
    sp = build_system_prompt("gzh", "主题", "读者")
    stripped = strip_tool_discipline(sp)
    assert "长短句交错" in stripped        # 人味段保留
    assert "工具使用纪律" not in stripped  # 纪律段已剥离
