"""draft/add 封面必填的回归测试。

2026-10-21 实测（api.weixin.qq.com/cgi-bin/draft/add）：thumb_media_id 是
必填项——空串、省略字段都返回 40007「invalid media_id」，不存在“无封面草稿”。
无配图的文章推送曾因此失败，且错误信息完全指不到真正原因（没封面）。
现在 add_draft 显式拦截并给出「加工排版」指引。
"""
from __future__ import annotations

import asyncio

from app.services.wechat import WeChatClient


async def _run():
    client = WeChatClient(appid="x", secret="y")

    # 空封面：不发请求，直接返回可操作错误（hint 指向加工排版）
    res = await client.add_draft("标题", "<p>x</p>")
    assert res["ok"] is False, res
    assert "封面" in res["error"], res
    assert "加工排版" in (res.get("hint") or ""), res

    # 未配置凭证：同样明确报错（不依赖网络）
    bad = WeChatClient(appid="", secret="")
    res = await bad.add_draft("标题", "<p>x</p>", thumb_media_id="THUMB")
    assert res["ok"] is False and "未配置" in res["error"], res


def test_add_draft_requires_thumb():
    asyncio.run(_run())
