"""Cloudflare Workers AI 对话通道单元测试（不依赖真实凭证）。

覆盖计划里的验证点：
  1. CF client 构造（凭证有无两态）
  2. CF 分支特例：忽略 tools、不传 stream_options、max_tokens 收紧、模型取 override
  3. CF 失败不静默 fallback
  4. 流式异常 → {"type": "error"} 事件（SSE 已 200 的唯一告知途径）
  5. CF 流式 usage 缺失 → estimated 估价路径
  6. 历史工具痕迹剥离（llama 收到 role:tool 会 400）
  7. system prompt 工具纪律段剥离
  8. 模型清单：成功过滤 / 失败不进缓存 / 凭证缺 400 / TTL 缓存命中
  9. chat-provider PUT 的模型校验与清空
 10. _set_setting 的 upsert 语义

写法说明：项目不依赖 pytest-asyncio，async 用例按 test_auth.py 的传统
用 asyncio.run() 驱动（见各 wrapper）。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.prompts.platforms import BASE_SYSTEM, strip_tool_discipline
from app.routers import settings as settings_router
from app.schemas.models import ChatProviderRequest
from app.services.agent import _strip_tool_traces
from app.services.llm import LLMClient, Usage


# ---------------------------------------------------------------- 假客户端
class _FakeDelta:
    def __init__(self, content: str = "", reasoning: str | None = None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None):
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None, usage=None):
        self.choices = [_FakeChoice(delta, finish_reason)]
        self.usage = usage


class _FakeCompletions:
    """记录 kwargs，按脚本吐出假 chunk 流。

    stream=True → 返回 _FakeStream（流式路径）；
    stream 缺省 → 返回 _FakeRespMsg（非流式路径，chat() 的 _call 用）。
    """

    def __init__(self, chunks: list[_FakeChunk] | None = None, exc: Exception | None = None):
        self.chunks = chunks or []
        self.exc = exc
        self.last_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any):
        self.last_kwargs = kwargs
        if self.exc:
            raise self.exc
        if kwargs.get("stream"):
            return _FakeStream(self.chunks)
        return _FakeRespMsg(self.chunks)


class _FakeRespMsg:
    """非流式响应：把假 chunk 拼成一条 message（chunks 的 content 相连）。"""

    def __init__(self, chunks: list[_FakeChunk]):
        from types import SimpleNamespace

        content = "".join(c.choices[0].delta.content or "" for c in chunks)
        usage = next((c.usage for c in reversed(chunks) if c.usage), None)
        msg = SimpleNamespace(content=content, tool_calls=None)
        self.choices = [SimpleNamespace(message=msg, finish_reason="stop")]
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks: list[_FakeChunk]):
        self._chunks = chunks

    def __aiter__(self):
        return _FakeStreamIter(self._chunks)


class _FakeStreamIter:
    def __init__(self, chunks: list[_FakeChunk]):
        self._it = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeCFClient:
    """假 Cloudflare OpenAI 兼容客户端。"""

    def __init__(self, chunks: list[_FakeChunk] | None = None, exc: Exception | None = None):
        self.completions = _FakeCompletions(chunks, exc)
        self.chat = _FakeChat(self.completions)


def _cf_client(monkeypatch, token: str = "cf-token", account: str = "acct-123") -> None:
    """把 CF 凭证写进 settings（monkeypatch 会自动复原）。"""
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", token)
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", account)


async def _collect(gen) -> list[dict]:
    return [evt async for evt in gen]


# ---------------------------------------------------------------- 1. client 构造
def test_cf_client_built_only_with_credentials(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "")
    assert LLMClient()._cloudflare is None

    _cf_client(monkeypatch)
    c = LLMClient()._cloudflare
    assert c is not None
    assert "/accounts/acct-123/ai/v1" in str(c.base_url)


# ---------------------------------------------------------------- 2-5. provider 分支
def test_chat_cf_ignores_tools_and_tightens_budget(monkeypatch):
    async def _body():
        _cf_client(monkeypatch)
        llm = LLMClient()
        fake = _FakeCFClient(chunks=[_FakeChunk(_FakeDelta(content="你好"))])
        llm._cloudflare = fake  # type: ignore[assignment]

        result = await llm.chat(
            [{"role": "user", "content": "hi"}],
            provider="cloudflare",
            tools=[{"type": "function", "function": {"name": "x"}}],
            model_override="@cf/meta/llama-3.1-8b-instruct",
        )
        kw = fake.completions.last_kwargs
        assert "tools" not in kw and "tool_choice" not in kw, "CF 分支必须剥掉 tools"
        assert kw["model"] == "@cf/meta/llama-3.1-8b-instruct"
        assert kw["max_tokens"] == 4096, "CF 模型输出上限低，不能用 DeepSeek 的 16384"
        assert result.content == "你好"

    asyncio.run(_body())


def test_chat_cf_no_silent_fallback(monkeypatch):
    async def _body():
        _cf_client(monkeypatch)
        llm = LLMClient()
        fake = _FakeCFClient(exc=RuntimeError("401 unauthorized"))
        llm._cloudflare = fake  # type: ignore[assignment]
        # 失败必须直接抛错（上层转 SSE error），不能静默切 fallback
        with pytest.raises(RuntimeError, match="401"):
            await llm.chat([{"role": "user", "content": "hi"}], provider="cloudflare")

    asyncio.run(_body())


def test_stream_cf_skips_stream_options(monkeypatch):
    async def _body():
        _cf_client(monkeypatch)
        llm = LLMClient()
        fake = _FakeCFClient(
            chunks=[
                _FakeChunk(_FakeDelta(content="你")),
                _FakeChunk(_FakeDelta(content="好"), finish_reason="stop"),
            ]
        )
        llm._cloudflare = fake  # type: ignore[assignment]

        events = await _collect(
            llm.stream_call(
                [{"role": "user", "content": "hi"}],
                provider="cloudflare",
                tools=[{"type": "function", "function": {"name": "x"}}],
            )
        )
        kw = fake.completions.last_kwargs
        assert "stream_options" not in kw, "CF 的 /ai/v1 不认识 include_usage，带了会 400"
        assert kw["stream"] is True
        assert "".join(e["content"] for e in events if e["type"] == "delta") == "你好"
        assert events[-1]["type"] == "finish"

    asyncio.run(_body())


def test_stream_error_becomes_sse_event(monkeypatch):
    async def _body():
        _cf_client(monkeypatch)
        llm = LLMClient()
        fake = _FakeCFClient(exc=RuntimeError("400 bad model name"))
        llm._cloudflare = fake  # type: ignore[assignment]

        events = await _collect(
            llm.stream_call([{"role": "user", "content": "hi"}], provider="cloudflare")
        )
        types = [e["type"] for e in events]
        assert "error" in types, "异常必须转成 error 事件，不能冒泡成断流"
        err = next(e for e in events if e["type"] == "error")
        assert "bad model name" in err["message"]
        assert types[-1] == "finish" and events[-1]["finish_reason"] == "error"

    asyncio.run(_body())


def test_stream_cf_missing_usage_is_estimated(monkeypatch):
    """CF 流式不带 usage → total_tokens=0 → estimated 估价路径（不炸）。"""

    async def _body():
        _cf_client(monkeypatch)
        llm = LLMClient()
        fake = _FakeCFClient(
            chunks=[
                _FakeChunk(_FakeDelta(content="正文")),
                _FakeChunk(_FakeDelta(content=""), finish_reason="stop"),
            ]
        )
        llm._cloudflare = fake  # type: ignore[assignment]

        events = await _collect(
            llm.stream_call([{"role": "user", "content": "hi"}], provider="cloudflare")
        )
        usage_evt = next(e for e in events if e["type"] == "usage")
        u: Usage = usage_evt["usage"]
        assert u.total_tokens == 0 and u.estimated is True

    asyncio.run(_body())


def test_stream_deepseek_path_unchanged(monkeypatch):
    """回归：DeepSeek 路径仍带 tools + stream_options（重构共用 _run_stream 后不能变）。"""

    async def _body():
        llm = LLMClient()
        fake = _FakeCFClient(chunks=[_FakeChunk(_FakeDelta(content="hi"), finish_reason="stop")])
        llm._primary = fake  # type: ignore[assignment]

        events = await _collect(
            llm.stream_call(
                [{"role": "user", "content": "hi"}],
                task="lightweight",
                tools=[{"type": "function", "function": {"name": "x"}}],
            )
        )
        kw = fake.completions.last_kwargs
        assert kw.get("tools"), "DeepSeek 路径 tools 必须带上"
        assert kw.get("stream_options") == {"include_usage": True}, "stream_options 不能丢"
        assert [e["type"] for e in events if e["type"] == "delta"]

    asyncio.run(_body())


# ---------------------------------------------------------------- 6-7. 剥离
def test_strip_tool_traces():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "写篇文章"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {
            "role": "assistant",
            "content": "这是正文",
            "tool_calls": [{"id": "c2", "function": {"name": "y", "arguments": "{}"}}],
        },
    ]
    out = _strip_tool_traces(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant"]
    assert all("tool_calls" not in m for m in out), "CF 消息里不能残留 tool_calls"
    assert all(m["role"] != "tool" for m in out)
    assert out[2]["content"] == "这是正文", "有正文的工具轮次要保留正文"


def test_strip_tool_discipline():
    s = strip_tool_discipline(BASE_SYSTEM)
    assert "工具使用纪律" not in s and "check_compliance" not in s
    assert "交付纪律" in s, "不能误伤纪律段之前的 prompt 内容"
    assert strip_tool_discipline(s) == s, "幂等"
    assert strip_tool_discipline("普通 prompt") == "普通 prompt", "无标记原样返回"


# ---------------------------------------------------------------- 8. 模型清单
class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


class _FakeHttpxClient:
    """假 httpx.AsyncClient：记录调用次数，按脚本返回。"""

    calls = 0
    next_payload: dict = {"success": True, "result": []}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        type(self).calls += 1
        return _FakeResp(type(self).next_payload)


@pytest.fixture
def fake_httpx(monkeypatch):
    _FakeHttpxClient.calls = 0
    _FakeHttpxClient.next_payload = {"success": True, "result": []}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHttpxClient)
    settings_router._CF_MODELS_CACHE.clear()
    yield _FakeHttpxClient
    settings_router._CF_MODELS_CACHE.clear()


def _cf_model(
    name: str,
    task: str = "Text Generation",
    deprecated: bool = False,
    paid: bool = False,
) -> dict:
    """按模型搜索接口的**真实结构**造条目（2026-10-23 实测校正）。

    老版本这里把 task / deprecated 塞进 properties 且用 "name" 作键 ——
    真实响应的 property 键名是 property_id、task 在顶层对象里。照着错结构
    写的过滤器永远不命中（空转），实现侧已修正，夹具跟着改。
    """
    props = []
    if deprecated:
        props.append({"property_id": "deprecated", "value": True})
    if paid:
        props.append({"property_id": "require_workers_paid", "value": True})
    return {
        "name": name,
        "description": f"desc of {name}",
        "task": {"id": "t1", "name": task},
        "properties": props,
    }


def test_fetch_cf_models_filters_and_caches(monkeypatch, fake_httpx):
    async def _body():
        _cf_client(monkeypatch)
        _FakeHttpxClient.next_payload = {
            "success": True,
            "result": [
                _cf_model("@cf/meta/llama-3.1-8b-instruct"),
                _cf_model("@cf/mistral/mistral-7b-instruct-v0.1"),
                _cf_model("@cf/old/model", deprecated=True),  # 剔除
                _cf_model("@cf/black-forest-labs/flux-1-schnell", task="Text-to-Image"),  # 非对话，剔除
                _cf_model("@cf/moonshotai/kimi-k2.6", paid=True),  # 需 Workers Paid，剔除（2026-10-23）
                {"name": "not-cf", "description": ""},  # 非 @cf/ 前缀，剔除
            ],
        }
        models = await settings_router._fetch_cf_models()
        names = [m["name"] for m in models]
        assert "@cf/meta/llama-3.1-8b-instruct" in names
        assert "@cf/mistral/mistral-7b-instruct-v0.1" in names
        assert not any("old" in n or "flux" in n or n == "not-cf" for n in names)
        assert "kimi" not in " ".join(names), "付费模型不进免费清单"
        assert names[0].startswith("@cf/meta"), "llama 系排前面（默认模型优先可见）"
        # 缓存命中：第二次不再打 CF
        assert _FakeHttpxClient.calls == 1
        await settings_router._fetch_cf_models()
        assert _FakeHttpxClient.calls == 1, "TTL 内应命中缓存"

    asyncio.run(_body())


def test_fetch_cf_models_failure_not_cached(monkeypatch, fake_httpx):
    async def _body():
        _cf_client(monkeypatch)
        _FakeHttpxClient.next_payload = {"success": False, "errors": [{"code": 10000}]}
        with pytest.raises(RuntimeError):
            await settings_router._fetch_cf_models()
        assert not settings_router._CF_MODELS_CACHE, "失败结果不能进缓存"
        # 修好后重试立即可用（不用等 TTL）
        _FakeHttpxClient.next_payload = {
            "success": True,
            "result": [_cf_model("@cf/meta/llama-3.1-8b-instruct")],
        }
        models = await settings_router._fetch_cf_models()
        assert len(models) == 1 and _FakeHttpxClient.calls == 2

    asyncio.run(_body())


def test_fetch_cf_models_requires_credentials(monkeypatch, fake_httpx):
    async def _body():
        monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "")
        monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "")
        with pytest.raises(HTTPException) as exc_info:
            await settings_router._fetch_cf_models()
        assert exc_info.value.status_code == 400
        assert _FakeHttpxClient.calls == 0, "没凭证不该打 CF"

    asyncio.run(_body())


# ---------------------------------------------------------------- 9. PUT 校验
def test_put_chat_provider_validates_model(monkeypatch, fake_httpx):
    """选了清单外的模型 → 400；清单拉取失败 → 放宽放行（不堵死保存）。"""

    async def _body():
        _cf_client(monkeypatch)
        _FakeHttpxClient.next_payload = {
            "success": True,
            "result": [_cf_model("@cf/meta/llama-3.1-8b-instruct")],
        }
        db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await settings_router.put_chat_provider(
                ChatProviderRequest(provider="cloudflare", model="@cf/not/exists"), db, "o1"
            )
        assert exc_info.value.status_code == 400

        # 清单拉取失败时放宽：只要非空就收。
        # 先清缓存 —— 上面第一个场景刚把成功清单缓存进 TTL，不清的话
        # 这里会读到旧清单（这本身就是真实特性：TTL 内用缓存）。
        settings_router._CF_MODELS_CACHE.clear()
        _FakeHttpxClient.next_payload = {"success": False, "errors": []}
        res = await settings_router.put_chat_provider(
            ChatProviderRequest(provider="cloudflare", model="@cf/whatever"), db, "o1"
        )
        assert res["model"] == "@cf/whatever"

        # 切回 deepseek：model 强制清空（防串味）
        res = await settings_router.put_chat_provider(
            ChatProviderRequest(provider="deepseek", model="@cf/meta/llama-3.1-8b-instruct"),
            db,
            "o1",
        )
        assert res["provider"] == "deepseek" and res["model"] == ""

    asyncio.run(_body())


def test_put_chat_provider_requires_credentials(monkeypatch):
    async def _body():
        monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "")
        monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "")
        with pytest.raises(HTTPException) as exc_info:
            await settings_router.put_chat_provider(
                ChatProviderRequest(provider="cloudflare"), AsyncMock(), "o1"
            )
        assert exc_info.value.status_code == 400

    asyncio.run(_body())


# ---------------------------------------------------------------- 10. upsert 语义
def test_set_setting_is_upsert():
    """_set_setting 走 ON CONFLICT upsert：重复写同 key 不炸（并发双写场景）。"""

    async def _body():
        db = AsyncMock()
        await settings_router._set_setting(db, "o1", "k", "v1")
        await settings_router._set_setting(db, "o1", "k", "v2")
        # 两次调用 = insert(ON CONFLICT DO NOTHING) + update 各两轮，共 4 次 execute
        assert db.execute.await_count == 4
        sql_texts = [str(c.args[0]) for c in db.execute.await_args_list]
        assert any("ON CONFLICT" in t.upper() for t in sql_texts), (
            "必须走 upsert 而非 select-then-write"
        )

    asyncio.run(_body())
