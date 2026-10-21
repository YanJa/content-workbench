"""Cloudflare 生图提示词翻译的单元测试。

背景（2026-10-21 实测）：flux-1-schnell 对中文语义基本不识——中文提示词
「一杯咖啡旁的笔记本电脑…俯视图」生成的是古风仕女图，同一语义的英文提示词
则精确命中。故含 CJK 的描述先经轻量 LLM 译成英文再喂模型。
"""
from __future__ import annotations

import asyncio

from app.core.config import settings
from app.services import imagegen


class _FakeRes:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """记录调用并返回预设内容的假 LLMClient。"""

    def __init__(self, content: str = "", raise_exc: bool = False):
        self._content = content
        self._raise = raise_exc
        self.calls: list[list[dict]] = []

    async def chat(self, messages, task="copywriting", **kw):
        self.calls.append(messages)
        if self._raise:
            raise RuntimeError("LLM 挂了")
        return _FakeRes(self._content)


def _patch_llm(monkeypatch_target: dict, fake: _FakeLLM):
    import app.services.llm as llm_mod

    monkeypatch_target["orig"] = llm_mod.LLMClient
    llm_mod.LLMClient = lambda: fake


def _restore_llm(monkeypatch_target: dict):
    import app.services.llm as llm_mod

    if "orig" in monkeypatch_target:
        llm_mod.LLMClient = monkeypatch_target["orig"]


async def _run():
    orig_translate = settings.CLOUDFLARE_IMAGE_TRANSLATE
    settings.CLOUDFLARE_IMAGE_TRANSLATE = True
    patch: dict = {}
    try:
        # 1) 中文提示词 → 走 LLM 翻译，返回英文结果
        fake = _FakeLLM("a laptop next to a cup of coffee, warm light, office desk, flat lay")
        _patch_llm(patch, fake)
        out = await imagegen._translate_prompt('一杯咖啡旁的笔记本电脑，温暖光线，俯拍')
        assert out == "a laptop next to a cup of coffee, warm light, office desk, flat lay", out
        assert len(fake.calls) == 1, "中文描述应触发一次翻译调用"
        # 系统提示在位（防回归：有人把翻译指令删了）
        sys_msg = fake.calls[0][0]["content"]
        assert "英文" in sys_msg and "FLUX" in sys_msg, sys_msg[:80]
        _restore_llm(patch)

        # 2) 纯英文提示词 → 不调 LLM，原样返回
        fake2 = _FakeLLM("should not be called")
        _patch_llm(patch, fake2)
        out2 = await imagegen._translate_prompt("a laptop on a desk, warm light")
        assert out2 == "a laptop on a desk, warm light", out2
        assert fake2.calls == [], "英文提示词不该触发翻译"
        _restore_llm(patch)

        # 3) LLM 调用失败 → 回落原提示词（翻译是增强，不能挡死生图）
        fake3 = _FakeLLM(raise_exc=True)
        _patch_llm(patch, fake3)
        out3 = await imagegen._translate_prompt('一杯咖啡旁的笔记本电脑')
        assert out3 == '一杯咖啡旁的笔记本电脑', out3
        _restore_llm(patch)

        # 4) 翻译结果异常（翻回来还是中文）→ 回落原提示词
        fake4 = _FakeLLM('一台笔记本电脑和咖啡')  # 假翻译器坏了，吐的还是中文
        _patch_llm(patch, fake4)
        out4 = await imagegen._translate_prompt('一杯咖啡旁的笔记本电脑')
        assert out4 == '一杯咖啡旁的笔记本电脑', out4
        _restore_llm(patch)

        # 5) 开关关掉 → 不翻译
        settings.CLOUDFLARE_IMAGE_TRANSLATE = False
        fake5 = _FakeLLM("whatever")
        _patch_llm(patch, fake5)
        out5 = await imagegen._translate_prompt('一杯咖啡旁的笔记本电脑')
        assert out5 == '一杯咖啡旁的笔记本电脑', out5
        assert fake5.calls == [], "关掉开关后不该调 LLM"
        _restore_llm(patch)
    finally:
        settings.CLOUDFLARE_IMAGE_TRANSLATE = orig_translate


def test_prompt_translation_for_cloudflare():
    asyncio.run(_run())
