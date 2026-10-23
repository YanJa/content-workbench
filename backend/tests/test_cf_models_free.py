"""Cloudflare 模型清单「只显示免费模型」的回归测试。

背景（2026-10-23 用户要求「不是所有模型都能用，只能用 free 的，只帮我
显示」）：免费账号选到需要 Workers Paid 的模型，对话直接 403（内部错误
5035）。官方定价页列了 7 个「需要付费计费方式」的模型，与模型搜索接口
返回的 property `require_workers_paid=true` 逐条吻合 —— 下拉据此过滤。

顺带锁死两处「过滤器空转」的修复（老代码按错误的键名查找，等于没装）：
  - task 在顶层对象 m["task"]["name"]，不在 properties 里；
  - deprecated 的 property 键名是 property_id，不是 name。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.routers import settings as settings_router


def _run(coro):
    return asyncio.run(coro)


def _m(name, task="Text Generation", props=None, desc="x"):
    """按模型搜索接口的真实结构造一条：task 是顶层对象，properties 用
    property_id 作键（2026-10-23 实测响应结构）。"""
    return {
        "name": name,
        "description": desc,
        "task": {"id": "t1", "name": task},
        "properties": [{"property_id": k, "value": v} for k, v in (props or {}).items()],
    }


# 官方定价页点名的 7 个「需要付费计费方式」模型（免费账号 403）
_PAID_MODELS = [
    "@cf/moonshotai/kimi-k2.6",
    "@cf/moonshotai/kimi-k2.7-code",
    "@cf/zai-org/glm-5.2",
    "@cf/zai-org/glm-5.3",
    "@cf/zai-org/glm-5.3-flash",
    "@cf/deepseek-ai/deepseek-v4-flash-0731",
    "@cf/deepseek-ai/deepseek-v4-pro-0813",
]


# ---------------------------------------------------------------- 纯函数


def test_paid_models_filtered_out():
    for name in _PAID_MODELS:
        m = _m(name, props={"require_workers_paid": True})
        assert settings_router._cf_model_allowed(m) is False, name


def test_free_text_gen_models_kept():
    for name in [
        "@cf/meta/llama-3.1-8b-instruct-fp8",
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "@cf/openai/gpt-oss-120b",
        "@cf/qwen/qwen3-30b-a3b-fp8",
        "@cf/zai-org/glm-4.7-flash",
    ]:
        m = _m(name, props={"price": [{"price": 0.35}]})
        assert settings_router._cf_model_allowed(m) is True, name


def test_non_text_generation_tasks_filtered():
    # task 在顶层对象里（老代码去 properties 找 "task"，过滤器空转）
    assert settings_router._cf_model_allowed(_m("@cf/x/img", task="Text-to-Image")) is False
    assert settings_router._cf_model_allowed(_m("@cf/x/asr", task="Automatic Speech Recognition")) is False
    # 没有 task 字段的保留（宁多勿漏，老口径）
    assert settings_router._cf_model_allowed({"name": "@cf/x/unknown"}) is True


def test_deprecated_filtered():
    # deprecated 的键名是 property_id（老代码找 "name"，过滤器空转）
    assert (
        settings_router._cf_model_allowed(
            _m("@cf/meta/old", props={"deprecated": True})
        )
        is False
    )
    # deprecated=false 不算废弃
    assert (
        settings_router._cf_model_allowed(
            _m("@cf/meta/ok", props={"deprecated": False})
        )
        is True
    )


def test_non_cf_prefix_filtered():
    assert settings_router._cf_model_allowed(_m("my-org/model")) is False


def test_props_helper_handles_garbage():
    # properties 里混进非 dict / 缺键的条目不能炸
    m = {
        "name": "@cf/meta/llama-3.1-8b-instruct-fp8",
        "task": {"name": "Text Generation"},
        "properties": ["junk", {"value": 1}, {"property_id": "price", "value": []}],
    }
    assert settings_router._cf_props(m) == {"price": []}


# ---------------------------------------------------------------- 拉取


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data, *a, **k):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _FakeResp(self._data)


def _payload():
    return {
        "success": True,
        "result": [
            _m("@cf/meta/llama-3.1-8b-instruct-fp8", desc="Llama 3.1 8B\n第二行"),
            _m("@cf/openai/gpt-oss-120b"),
            _m(_PAID_MODELS[0], props={"require_workers_paid": True}),
            _m(_PAID_MODELS[3], props={"require_workers_paid": True}),
            _m("@cf/moonshotai/kimi-k2.6-paid-copy", props={"require_workers_paid": True}),
            _m("@cf/baai/bge-large-en-v1.5", task="Text Embedding"),
            _m("@cf/microsoft/resnet-50", task="Image Classification"),
            _m("@cf/meta/deprecated-one", props={"deprecated": True}),
            {"name": "not-cf/model", "task": {"name": "Text Generation"}},
        ],
    }


def test_fetch_cf_models_only_free(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "tok")
        monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acc")
        monkeypatch.setattr(settings_router, "_CF_MODELS_CACHE", {})
        monkeypatch.setattr(
            "httpx.AsyncClient", lambda *a, **k: _FakeClient(_payload())
        )

        models = await settings_router._fetch_cf_models()
        names = [m["name"] for m in models]
        assert names == ["@cf/meta/llama-3.1-8b-instruct-fp8", "@cf/openai/gpt-oss-120b"]
        # llama 系列排最前（默认模型优先可见）+ label 取描述首行
        assert models[0]["label"] == "Llama 3.1 8B"

        # 缓存生效：第二次不再打接口（换一个会炸的 payload 也不影响）
        monkeypatch.setattr(
            "httpx.AsyncClient", lambda *a, **k: _FakeClient({"success": False})
        )
        again = await settings_router._fetch_cf_models()
        assert [m["name"] for m in again] == names
        await _dispose_cache()

    _run(go())


def test_fetch_cf_models_all_paid_raises(monkeypatch):
    """账号下只剩付费模型 → 明确报错，而不是返回空清单让下拉空转。"""

    async def go():
        monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "tok")
        monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acc")
        monkeypatch.setattr(settings_router, "_CF_MODELS_CACHE", {})
        payload = {"success": True, "result": [_m(_PAID_MODELS[0], props={"require_workers_paid": True})]}
        monkeypatch.setattr(
            "httpx.AsyncClient", lambda *a, **k: _FakeClient(payload)
        )
        with pytest.raises(RuntimeError, match="免费计划可用"):
            await settings_router._fetch_cf_models()
        await _dispose_cache()

    _run(go())


async def _dispose_cache():
    settings_router._CF_MODELS_CACHE.clear()
