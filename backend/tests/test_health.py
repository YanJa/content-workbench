"""health 端点测试。

_health_payload 是探活 payload 的单一来源（裸 /health 与 /api/health 共用）。
2026-10-20 起它多背了「模型明细」字段 —— 服务健康卡片要展示用户实际在用的
模型（Flash/Pro 双模型 + CF 通道默认值），光一个 llm_configured 布尔不够。

注意：字段值来自 settings（.env 可能被覆盖），测试只断言**字段存在且类型
正确**，不断言具体模型名。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import _health_payload, app

client = TestClient(app)

MODEL_FIELDS = (
    "model_flash",
    "model_pro",
    "fallback_model",
    "cf_chat_model",
    "cf_configured",
)


def test_health_payload_contains_model_fields():
    payload = _health_payload()
    for key in MODEL_FIELDS:
        assert key in payload, f"health payload 缺字段 {key}"
    # 模型名都是字符串（可能为空串，如 fallback_model 默认不兜底）
    for key in MODEL_FIELDS[:4]:
        assert isinstance(payload[key], str), f"{key} 应为字符串"
    assert isinstance(payload["cf_configured"], bool)


def test_health_endpoints_expose_model_fields():
    # 裸路径（探活）与带前缀路径（前端）必须一致
    for path in ("/health", "/api/health"):
        resp = client.get(path)
        assert resp.status_code == 200
        body = resp.json()
        for key in MODEL_FIELDS:
            assert key in body, f"{path} 缺字段 {key}"


def test_health_payload_exposes_version():
    payload = _health_payload()
    assert payload["version"] == settings.APP_VERSION
    assert isinstance(payload["version"], str) and payload["version"]
    for path in ("/health", "/api/health"):
        assert client.get(path).json()["version"] == settings.APP_VERSION
