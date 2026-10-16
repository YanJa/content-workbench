"""占位符替换的单元测试。

重点验证「只替换白名单键、吃掉后缀说明、不误伤其它槽位」。
"""
from __future__ import annotations

import pytest

from app.services.author import apply_author_placeholders, apply_placeholders


def test_plain_author_and_intro():
    """最朴素的两个占位符。"""
    text = "<span leaf=\"\">我是 {{作者名}}，{{一句话简介}}。</span>"
    out = apply_author_placeholders(text, "小明", "专注 AI 观察")
    assert out == "<span leaf=\"\">我是 小明，专注 AI 观察。</span>"
    assert "{{" not in out


def test_suffixed_variants():
    """带说明后缀的变体（组件库真实写法）也要被完整吃掉。"""
    text = "我是 {{作者名，未知则删整行}}，{{一句话简介，如：热衷于分享 AI 观察与干货}}。"
    out = apply_author_placeholders(text, "小明", "专注 AI 观察")
    assert out == "我是 小明，专注 AI 观察。"
    # 说明文字不能残留
    assert "未知则删整行" not in out
    assert "如：" not in out


def test_empty_values_keep_placeholder():
    """值为空时保持占位符原样（让用户看到「还没填」）。"""
    text = "我是 {{作者名}}，{{一句话简介}}。"
    assert apply_author_placeholders(text, "", "") == text
    # 只填一个：另一个保持不动
    out = apply_author_placeholders(text, "小明", "")
    assert out == "我是 小明，{{一句话简介}}。"


def test_does_not_touch_other_slots():
    """绝不误伤其它槽位 —— 这是最关键的一条。

    注意：`{{号名}}` 已进入白名单（账号维度），因此本用例改用**未登记**的
    槽位来验证「白名单之外一律不动」。
    """
    text = (
        "{{简介段落}} {{作者身份}} {{LOGO占位}} {{顶部标签}} "
        "{{一句话简介}} {{作者名}}"
    )
    out = apply_author_placeholders(text, "小明", "专注 AI")
    # 其它槽位原样保留
    assert "{{简介段落}}" in out
    assert "{{作者身份}}" in out
    assert "{{LOGO占位}}" in out
    assert "{{顶部标签}}" in out
    # 目标两键被替换
    assert "专注 AI" in out
    assert "小明" in out


def test_account_placeholders():
    """账号维度的 {{号名}} / {{slogan}} 也能被替换。"""
    text = "<span leaf=\"\">{{号名}}</span> · <span leaf=\"\">{{slogan}}</span>"
    out = apply_placeholders(text, {"号名": "码上聊聊", "slogan": "有问题，码上聊聊"})
    assert out == "<span leaf=\"\">码上聊聊</span> · <span leaf=\"\">有问题，码上聊聊</span>"
    assert "{{" not in out


def test_author_helper_does_not_touch_account_keys():
    """旧的 apply_author_placeholders 不该动账号键（职责隔离）。"""
    text = "{{号名}} {{slogan}} {{作者名}}"
    out = apply_author_placeholders(text, "小明", "AI")
    assert "{{号名}}" in out
    assert "{{slogan}}" in out
    assert "小明" in out


def test_whitelist_blocks_unknown_keys():
    """即使 mapping 里塞了未登记的键，也不能替换。"""
    text = "{{简介}} {{作者身份}}"
    out = apply_placeholders(text, {"简介": "X", "作者身份": "Y"})
    assert out == text  # 一字不动


def test_account_empty_value_keeps_placeholder():
    """账号值为空 → 占位符保留（与作者维度同语义）。"""
    text = "{{号名}}"
    assert apply_placeholders(text, {"号名": ""}) == text


def test_multiple_occurrences():
    """同一段里多次出现应全部替换。"""
    text = "{{作者名}} 说：{{作者名}}（{{一句话简介}}）"
    out = apply_author_placeholders(text, "小明", "AI 观察")
    assert out == "小明 说：小明（AI 观察）"


def test_whitespace_tolerant():
    """占位符内部有空格也要能匹配（模型有时会加空格）。"""
    text = "{{ 作者名 }} / {{  一句话简介  }}"
    out = apply_author_placeholders(text, "小明", "AI 观察")
    assert out == "小明 / AI 观察"


def test_empty_text():
    assert apply_author_placeholders("", "小明", "AI") == ""


@pytest.mark.parametrize("blank", ["", None])
def test_falsy_text(blank):
    assert apply_author_placeholders(blank, "小明", "AI") in ("", None)
