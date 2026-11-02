"""小云雀适配器测试。

分两层：
1. **离线**：monkeypatch 替掉 HTTP 调用，验证请求体组装、状态机解析、参数校验。
   不联网，任何环境都能跑。
2. **在线冒烟**（默认跳过）：用假凭证打真实端点。它的价值很特别——如果签名算错，
   服务端返回 InvalidSignature / SignatureDoesNotMatch；如果签名**对**而只是凭证
   不存在，返回的是 InvalidAccessKey。所以只要看到 InvalidAccessKey，就证明签名
   链路（CanonicalRequest、派生链、Authorization 头、Region/Service）全对。
   设 VOLC_ONLINE_TEST=1 才跑。

异步写法跟随项目惯例：同步测试函数 + asyncio.run()，不引 pytest-asyncio。
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.services import pippit, volcengine


# ---------------------------------------------------------------- 常量
def test_req_key_is_documented_value():
    """req_key 是服务标识，错了会直接报服务不存在。"""
    assert pippit.REQ_KEY_VIDEO == "pippit_iv2v_cvtob"


def test_ratios_and_durations_match_api_spec():
    """可选值必须与接口文档一致 —— 特别是时长用的全角波浪号。"""
    assert pippit.RATIOS == ("16:9", "9:16", "4:3", "3:4")
    assert pippit.DURATIONS == ("～15s", "～30s", "40～60s")
    # 半角的 ~ 会被接口拒掉，这条断言防的是「顺手改成 ASCII」这种好心办坏事
    assert all("~" not in d for d in pippit.DURATIONS)


# ---------------------------------------------------------------- 提交
def test_submit_always_enables_watermark(monkeypatch):
    """明水印是法定义务，请求体里必须是 True，且没有开关能关掉它。"""
    captured: dict = {}

    async def fake_call(action, body, **kw):
        captured["action"] = action
        captured["body"] = body
        return {"task_id": "123456"}

    monkeypatch.setattr(volcengine, "call_visual", fake_call)
    task_id = asyncio.run(
        pippit.submit_video(prompt="一只猫在窗台上晒太阳", ratio="9:16", duration="～15s")
    )

    assert task_id == "123456"
    assert captured["action"] == "CVSync2AsyncSubmitTask"
    body = captured["body"]
    assert body["req_key"] == pippit.REQ_KEY_VIDEO
    assert body["enable_watermark"] is True
    assert body["ratio"] == "9:16"
    assert body["duration"] == "～15s"


def test_submit_rejects_bad_ratio(monkeypatch):
    monkeypatch.setattr(volcengine, "call_visual", _never_called)
    with pytest.raises(volcengine.VolcError, match="画幅只支持"):
        asyncio.run(pippit.submit_video(prompt="x", ratio="1:1"))


def test_submit_rejects_ascii_tilde_duration(monkeypatch):
    """半角 ~15s 在本地就被拦下，而不是发给服务端再报个看不懂的错。"""
    monkeypatch.setattr(volcengine, "call_visual", _never_called)
    with pytest.raises(volcengine.VolcError, match="时长只支持"):
        asyncio.run(pippit.submit_video(prompt="x", duration="~15s"))


def test_submit_rejects_empty_prompt(monkeypatch):
    monkeypatch.setattr(volcengine, "call_visual", _never_called)
    with pytest.raises(volcengine.VolcError, match="提示词不能为空"):
        asyncio.run(pippit.submit_video(prompt="   "))


def test_submit_rejects_overlong_prompt(monkeypatch):
    """超过 2000 字火山会「概率异常或指令不生效」，本地先挡掉。"""
    monkeypatch.setattr(volcengine, "call_visual", _never_called)
    with pytest.raises(volcengine.VolcError, match="2000"):
        asyncio.run(pippit.submit_video(prompt="字" * 2001))


# ---------------------------------------------------------------- 查询
def test_query_parses_done_with_duration(monkeypatch):
    async def fake_call(action, body, **kw):
        assert action == "CVSync2AsyncGetResult"
        return {"status": "done", "video_url": "https://x/a.mp4", "resp_data": '{"Duration":"15.2"}'}

    monkeypatch.setattr(volcengine, "call_visual", fake_call)
    info = asyncio.run(pippit.query_video("123"))
    assert info["done"] is True
    assert info["video_url"] == "https://x/a.mp4"
    assert info["duration"] == pytest.approx(15.2)


def test_query_generating_is_not_done(monkeypatch):
    async def fake_call(action, body, **kw):
        return {"status": "generating"}

    monkeypatch.setattr(volcengine, "call_visual", fake_call)
    info = asyncio.run(pippit.query_video("123"))
    assert info["done"] is False
    assert info["status_text"] == "生成中"


def test_query_done_without_url_is_not_done(monkeypatch):
    """status=done 但没给 URL，不能当成功——转存空地址是更隐蔽的故障。"""

    async def fake_call(action, body, **kw):
        return {"status": "done"}

    monkeypatch.setattr(volcengine, "call_visual", fake_call)
    info = asyncio.run(pippit.query_video("123"))
    assert info["done"] is False


def test_query_expired_reports_reason(monkeypatch):
    async def fake_call(action, body, **kw):
        return {"status": "expired"}

    monkeypatch.setattr(volcengine, "call_visual", fake_call)
    info = asyncio.run(pippit.query_video("123"))
    assert info["done"] is False
    assert "过期" in info["error"]


def test_wait_video_returns_url(monkeypatch):
    calls = {"n": 0}

    async def fake_call(action, body, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"status": "generating"}
        return {"status": "done", "video_url": "https://x/ok.mp4"}

    monkeypatch.setattr(volcengine, "call_visual", fake_call)
    url = asyncio.run(pippit.wait_video("123", poll_interval=0, timeout=10))
    assert url == "https://x/ok.mp4"
    assert calls["n"] == 3


def test_wait_video_timeout_says_task_still_running(monkeypatch):
    """超时的文案要提示「可继续用 task_id 查」，否则用户会重新提交、白扣额度。"""

    async def fake_call(action, body, **kw):
        return {"status": "generating"}

    monkeypatch.setattr(volcengine, "call_visual", fake_call)
    with pytest.raises(volcengine.VolcError, match="无需重新提交"):
        asyncio.run(pippit.wait_video("123", poll_interval=0, timeout=0.01))


# ---------------------------------------------------------------- 错误码
def test_err_hint_translation():
    """已知错误码翻成中文；未知码保留原始 message，别瞎猜。"""
    assert "敏感词" in volcengine._friendly(50413, "Post Text Risk Not Pass")
    assert volcengine._friendly(99999, "Some Raw Message") == "Some Raw Message"


async def _never_called(*a, **kw):  # noqa: ANN001
    raise AssertionError("不该发出请求：参数校验应在本地拦掉")


# ---------------------------------------------------------------- 在线冒烟
@pytest.mark.skipif(
    os.getenv("VOLC_ONLINE_TEST") != "1", reason="设 VOLC_ONLINE_TEST=1 才跑联网冒烟"
)
def test_online_signature_accepted_by_server():
    """用假凭证打真实端点：拿到 InvalidAccessKey 就说明签名链路对。

    签名算错时服务端报 SignatureDoesNotMatch，签名正确但凭证不存在时报
    InvalidAccessKey。两者走同一个报错通道，能区分开就等于验证了签名。
    """
    import httpx

    body = json.dumps(
        {"req_key": pippit.REQ_KEY_VIDEO, "prompt": "test", "enable_watermark": True},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    query = {"Action": "CVSync2AsyncSubmitTask", "Version": volcengine.VISUAL_VERSION}
    headers = volcengine.sign(
        access_key="AKLTfakefakefakefakefakefakefake",
        secret_key="ZmFrZWZha2VmYWtlZmFrZWZha2U=",
        method="POST",
        host=volcengine.VISUAL_HOST,
        path="/",
        query=query,
        body=body,
        region="cn-north-1",
        service="cv",
    )

    async def go():
        async with httpx.AsyncClient(timeout=30) as cli:
            return await cli.post(
                f"https://{volcengine.VISUAL_HOST}/",
                params=query,
                content=body,
                headers=headers,
            )

    resp = asyncio.run(go())
    err = (resp.json().get("ResponseMetadata") or {}).get("Error") or {}
    assert err.get("Code") == "InvalidAccessKey", f"签名可能有问题：{err}"
