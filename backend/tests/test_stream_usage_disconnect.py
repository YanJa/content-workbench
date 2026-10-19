"""流式断连时用量兜底记账的回归测试。

背景（2026-10-19 实测）：SSE 客户端断开（切页面/关标签页）时取消以
CancelledError（BaseException）冒泡，`except Exception` 抓不到，_run_stream
的收尾埋点被整段跳过 —— 模型 token 已烧、账上零记录。UI 端到端测试
一切换页面就复现。修复：埋点移入 finally 兜底。

这些测试锁死：
  1. 断开前已拿到 usage chunk → 断开后仍记真实值（token 不丢账）；
  2. 断开前 usage 还没到（流尾才发）→ 记一笔 estimated 兜底（调用事实不丢）；
  3. 正常完整消费 → 只记一次（finally 兜底不能造成双记）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.llm import LLMClient


class _FakeDelta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, choice=None, usage=None):
        self.choices = [choice] if choice else []
        self.usage = usage


def _delta_chunk(text: str) -> _FakeChunk:
    return _FakeChunk(_FakeChoice(_FakeDelta(content=text)))


def _usage_chunk(prompt: int, completion: int, cached: int = 0) -> _FakeChunk:
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    # 收尾 chunk 通常还带 1 条 finish_reason=stop 的 choice
    return _FakeChunk(_FakeChoice(_FakeDelta(content=""), finish_reason="stop"), usage=usage)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return _aiter(self._chunks)


class _aiter:
    def __init__(self, chunks):
        self._it = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream(self._chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.completions = _FakeCompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)


def _sink_recorder():
    calls = []

    async def sink(payload):
        calls.append(payload)

    return sink, calls


def _llm_with(monkeypatch, chunks) -> tuple[LLMClient, _FakeClient]:
    cf = _FakeClient(chunks)
    llm = LLMClient.__new__(LLMClient)  # 绕过 __init__（不建真客户端）
    llm._primary = cf
    llm._fallback = None
    llm._cloudflare = cf
    return llm, cf


def test_stream_usage_survives_disconnect(monkeypatch):
    """断开前已收到 usage chunk：aclose 后埋点必须兜底发出真实值（token 不丢账）。"""
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acct-123")
    chunks = [
        _delta_chunk("你"),
        _usage_chunk(1000, 500, 200),  # usage chunk 先到（DeepSeek 也这样：带 usage 的收尾 chunk 常提前）
        _delta_chunk("好"),  # 还没读到这里就断开
    ]
    llm, _ = _llm_with(monkeypatch, chunks)
    sink, calls = _sink_recorder()

    async def _body():
        gen = llm.stream_call(
            [{"role": "user", "content": "hi"}],
            task="copywriting",
            usage_sink=sink,
            provider="cloudflare",
        )
        got_deltas = 0
        async for evt in gen:
            if evt["type"] == "delta":
                got_deltas += 1
                if got_deltas >= 2:
                    break  # 读到第 2 个 delta 才断开（usage chunk 已读过）
        # 显式关闭生成器 —— StreamingResponse 取消任务时框架就是这么干的
        await gen.aclose()

    asyncio.run(_body())

    assert len(calls) == 1, f"断开后应兜底记 1 笔，实际 {len(calls)}"
    p = calls[0]
    assert p["prompt_tokens"] == 1000
    assert p["completion_tokens"] == 500
    assert p["cached_tokens"] == 200
    assert p["total_tokens"] == 1500
    assert p["failed"] is False  # 模型没失败，是客户端断开


def test_stream_disconnect_without_usage_records_estimated(monkeypatch):
    """断开前 usage 还没到（流尾才发）：记一笔 estimated 兜底，调用事实不丢。"""
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acct-123")
    chunks = [_delta_chunk("你"), _delta_chunk("好"), _usage_chunk(800, 300)]
    llm, _ = _llm_with(monkeypatch, chunks)
    sink, calls = _sink_recorder()

    async def _body():
        gen = llm.stream_call(
            [{"role": "user", "content": "hi"}],
            task="copywriting",
            usage_sink=sink,
            provider="cloudflare",
        )
        got_deltas = 0
        async for evt in gen:
            if evt["type"] == "delta":
                got_deltas += 1
                if got_deltas >= 2:
                    break  # 读满 2 个 delta 就断开，usage chunk 在流尾还没到
        await gen.aclose()

    asyncio.run(_body())
    assert len(calls) == 1, f"应记 1 笔 estimated 兜底，实际 {len(calls)}"
    assert calls[0]["total_tokens"] == 0
    assert calls[0]["failed"] is False


def test_stream_full_consume_records_once(monkeypatch):
    """正常完整消费：只记一次（finally 兜底不能造成双记）。"""
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acct-123")
    chunks = [_delta_chunk("你好"), _usage_chunk(800, 300)]
    llm, _ = _llm_with(monkeypatch, chunks)
    sink, calls = _sink_recorder()

    async def _body():
        async for _evt in llm.stream_call(
            [{"role": "user", "content": "hi"}],
            task="copywriting",
            usage_sink=sink,
            provider="cloudflare",
        ):
            pass

    asyncio.run(_body())
    assert len(calls) == 1, f"正常路径应只记 1 笔，实际 {len(calls)}"
    assert calls[0]["total_tokens"] == 1100
