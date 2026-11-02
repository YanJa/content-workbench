"""小云雀（Pippit）智能生视频 Agent 适配器。

小云雀是字节跳动/剪映旗下的 AI 视频创作 Agent。它的**开放 API 托管在火山引擎
视觉智能**下（不是 xyq.jianying.com 那个网页端），走标准的两步异步：

    1. CVSync2AsyncSubmitTask  → 拿 task_id
    2. CVSync2AsyncGetResult   → 轮询，done 后拿 video_url

服务标识：req_key = pippit_iv2v_cvtob（智能生视频 Agent 1.0）

⚠️ 三个必须记住的约束：

1. **video_url 只有 1 小时有效期**。拿到就必须立刻下载转存，否则历史任务全变死链。
2. **必须开明水印**。`enable_watermark` 默认 true，开后在左上角标「AI生成」、
   右下角标「小云雀AI生成」。按《人工智能生成合成内容标识办法》，这是法定义务，
   本模块**不提供关闭开关**。
3. **任务 12 小时过期**（status=not_found / expired），超时得重新提交。

至于「AI 海报 / 换背景」那些能力：网页版和 App 有，**开放 API 没有**。开放的就
生视频这一条线，别指望它出图。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services import volcengine as volc

logger = logging.getLogger("workbench")

# 智能生视频 Agent 1.0 的服务标识
REQ_KEY_VIDEO = "pippit_iv2v_cvtob"

# 画幅：接口只认这四个
RATIOS = ("16:9", "9:16", "4:3", "3:4")
# 时长档位（注意用的是全角波浪号，接口文档原文如此，不能换成 ASCII 的 ~）
DURATIONS = ("～15s", "～30s", "40～60s")

_STATUS_TEXT = {
    "processing": "任务前置处理中",
    "in_queue": "排队中",
    "generating": "生成中",
    "done": "已完成",
    "not_found": "任务未找到（可能已过期，任务保留 12 小时）",
    "expired": "任务已过期，请重新提交",
}


def configured() -> bool:
    return volc.configured()


def status_info() -> dict[str, Any]:
    """给前端的能力与状态说明。未配置时前端据此隐藏入口。"""
    return {
        "name": "pippit",
        "label": "小云雀（智能生视频）",
        "configured": configured(),
        "hint": "" if configured() else "在 .env 配 VOLC_ACCESS_KEY / VOLC_SECRET_KEY 后可用",
        "note": "字节跳动旗下 · 15-60 秒成片 · 带 AI 生成标识",
        # 前端据此渲染下拉，避免把可选值硬编码在两端
        "ratios": list(RATIOS),
        "durations": list(DURATIONS),
    }


async def submit_video(
    *,
    prompt: str,
    ratio: str = "",
    duration: str = "",
    language: str = "Chinese",
    accent: str = "",
    img_urls: list[str] | None = None,
    video_urls: list[str] | None = None,
) -> str:
    """提交一个生视频任务，返回 task_id。

    `img_urls` / `video_urls` 必须是**公网可访问**的地址 —— 火山那边是拉取而不是
    上传。本地文件得先传到可访问的地方（素材库签名直链就是干这个的）。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise volc.VolcError("提示词不能为空")
    if len(prompt) > 2000:
        raise volc.VolcError("提示词超过 2000 字，火山会报错或忽略指令，请压缩")

    body: dict[str, Any] = {
        "req_key": REQ_KEY_VIDEO,
        "prompt": prompt,
        # 法定义务：开明水印（左上「AI生成」+ 右下「小云雀AI生成」）
        "enable_watermark": True,
    }
    if ratio:
        if ratio not in RATIOS:
            raise volc.VolcError(f"画幅只支持 {'/'.join(RATIOS)}，收到 {ratio!r}")
        body["ratio"] = ratio
    if duration:
        if duration not in DURATIONS:
            raise volc.VolcError(f"时长只支持 {'/'.join(DURATIONS)}，收到 {duration!r}")
        body["duration"] = duration
    if language:
        body["language"] = language
    if accent:
        body["accent"] = accent
    if img_urls:
        body["img_url_list"] = img_urls
    if video_urls:
        body["video_url_list"] = video_urls

    data = await volc.call_visual("CVSync2AsyncSubmitTask", body)
    task_id = data.get("task_id")
    if not task_id:
        raise volc.VolcError(f"提交成功但没拿到 task_id：{data}")
    logger.info("小云雀任务已提交: %s", task_id)
    return str(task_id)


async def query_video(task_id: str) -> dict[str, Any]:
    """查一次任务状态。

    返回 `{status, done, video_url, duration, error}`：
    - status 是接口原始状态（processing/in_queue/generating/done/...）
    - done=True 只在**真的出片**时为真；done 状态也可能是业务失败，要配合 code 判断
    """
    data = await volc.call_visual(
        "CVSync2AsyncGetResult",
        {"req_key": REQ_KEY_VIDEO, "task_id": task_id},
    )
    status = str(data.get("status") or "")
    video_url = str(data.get("video_url") or "")
    duration = 0.0
    raw_extra = data.get("resp_data")
    if raw_extra:
        try:
            duration = float(json.loads(raw_extra).get("Duration") or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            duration = 0.0

    return {
        "status": status,
        "status_text": _STATUS_TEXT.get(status, status or "未知状态"),
        # 有 video_url 才算真出片：status=done 但 code!=10000 会在 call_visual 抛错，
        # 能走到这里的 done 一般都有 URL；仍做一次判空，免得转存空地址。
        "done": status == "done" and bool(video_url),
        "video_url": video_url,
        "duration": duration,
        "error": "" if video_url or status not in ("not_found", "expired") else _STATUS_TEXT[status],
    }


async def wait_video(
    task_id: str,
    *,
    poll_interval: float | None = None,
    timeout: float | None = None,
) -> str:
    """轮询到出片，返回 video_url。

    出片耗时不定（Agent 要自己写脚本、分镜、合成），30-60 秒到几分钟都有可能，
    所以默认给足 10 分钟。超时不代表失败 —— 任务还在火山那边跑，调用方可以
    稍后用 task_id 再查一次，不必重新提交（重新提交是要再扣额度的）。
    """
    interval = poll_interval if poll_interval is not None else settings.PIPPIT_POLL_INTERVAL
    limit = timeout if timeout is not None else settings.PIPPIT_TIMEOUT
    # 用「轮次上限」而不是累加 elapsed 来控制：interval 传 0（测试里常见的做法）时
    # 累加法永远不前进，只能靠死循环或者外部超时兜底。这里先算出该查多少次。
    if interval <= 0:
        max_rounds = 1000  # 间隔为 0 时的硬上限，防止失控空转打爆接口
    else:
        max_rounds = max(1, int(limit / interval) + 1)

    for _ in range(max_rounds):
        if interval > 0:
            await asyncio.sleep(interval)
        info = await query_video(task_id)
        if info["done"]:
            return info["video_url"]
        if info["status"] in ("not_found", "expired"):
            raise volc.VolcError(info["status_text"])
    raise volc.VolcError(
        f"等待超时（{limit:.0f} 秒）。任务仍在服务端运行，可用 task_id 继续查询，无需重新提交"
    )


async def download_video(url: str, *, max_mb: int | None = None) -> bytes:
    """把成片拉下来。

    链接 1 小时过期，所以调用方拿到 URL 要立刻调这里。限制体积防止被超大文件
    （或异常重定向）拖死内存。
    """
    limit = (max_mb or settings.PIPPIT_MAX_MB) * 1024 * 1024
    buf = bytearray()
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as cli:
        async with cli.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise volc.VolcError(f"下载成片失败：HTTP {resp.status_code}")
            async for chunk in resp.aiter_bytes(65536):
                buf.extend(chunk)
                if len(buf) > limit:
                    raise volc.VolcError(f"成片超过 {max_mb or settings.PIPPIT_MAX_MB}MB，已中止")
    return bytes(buf)
