"""朱雀 AI 生成检测客户端（腾讯 EdgeOne Makers 内置模型 @makers/zhuque-text）。

文档：https://cloud.tencent.com/document/product/1552/137539
端点：POST https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify

三分类口径（与官方报告单一致）：
  label 0 = 人工特征（Human）
  label 1 = AI 特征
  label 2 = 疑似 AI
每段带 conf（0~1），官方报告里叫「AIGC 值」。

⚠️ 两条纪律：
  1. Key 只从 settings.ZHUQUE_API_KEY 读（.env 注入），**永远不要**写进代码、
     日志或 git；免费额度 50 万 token/月，实际扣减看 makers_models_usage。
  2. 这是**外部黑箱概率**，中文文本误判率不低（实测连人味很重的段落也能判成
     0.9998 AI）。所以它在本工作台里永远和本地规则体检（humanize.scan）并列
     展示，且报告单必须带官方同款免责声明——「仅为辅助判断，不作为审核或
     处罚的决定性依据」。
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.core.config import settings

API = "https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify"

# label → 展示名。与官方报告单的图例对齐。
LABEL_NAMES = {0: "人工特征", 1: "AI特征", 2: "疑似AI"}

# 官方报告「AI 分布图」的着色阈值（按每段 AIGC 值分段）。
#   0    ~ 0.5  人工特征（绿）
#   0.5  ~ 0.9  疑似 AI（黄）
#   0.9  ~ 1.0  AI 特征（红）
BAND_RULES = (
    (0.5, "human", "人工特征"),
    (0.9, "suspect", "疑似AI"),
    (float("inf"), "ai", "AI特征"),
)


def configured() -> bool:
    return bool(settings.ZHUQUE_API_KEY)


def _proxy_kwargs() -> dict:
    # 复用知乎那套出口代理配置（沙箱访问外部接口被限流时才需要）
    return {"proxy": settings.ZHIHU_PROXY} if settings.ZHIHU_PROXY else {}


def band_of(conf: float) -> tuple[str, str]:
    """按 AIGC 值返回 (band_key, band_name)。"""
    for ceiling, key, name in BAND_RULES:
        if conf < ceiling:
            return key, name
    return "ai", "AI特征"


async def classify(text: str, is_merge: bool = False, timeout: int = 30) -> dict:
    """调一次朱雀分类，返回归一化结果：

    成功 → {ok, configured, human_ratio, ai_ratio, suspect_ratio,
            softmax_confidence, ratio_confidence, segments[], tokens_used, ms}
    失败 → {ok: False, error: "...", configured: bool}

    segments: [{text, label, label_name, conf, order, position}]
    is_merge=False：每段独立置信度（报告单的「片段解析」要的就是分段数据）。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "configured": configured(), "error": "没有可检测的文本"}
    if not configured():
        return {
            "ok": False,
            "configured": False,
            "error": "未配置 ZHUQUE_API_KEY（EdgeOne Makers API Key，见系统设置）",
        }
    if len(text) > settings.ZHUQUE_TEXT_MAX:
        return {
            "ok": False,
            "configured": True,
            "error": f"文本超过 {settings.ZHUQUE_TEXT_MAX} 字上限",
        }

    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, **_proxy_kwargs()) as client:
            resp = await client.post(
                API,
                headers={
                    "Authorization": f"Bearer {settings.ZHUQUE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"text": text, "is_merge": is_merge},
            )
            data = resp.json() if resp.content else {}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "configured": True, "error": f"朱雀接口请求失败：{exc}"}

    if resp.status_code != 200 or data.get("status") != "success":
        msg = (data.get("msg") or "").strip() or f"HTTP {resp.status_code}"
        return {"ok": False, "configured": True, "error": f"朱雀接口返回错误：{msg}"}

    ratios = data.get("labels_ratio") or {}
    segments_raw = data.get("segment_labels") or []
    segments = []
    for i, seg in enumerate(segments_raw, start=1):
        label = int(seg.get("label", 0))
        segments.append(
            {
                "text": seg.get("text", ""),
                "label": label,
                "label_name": LABEL_NAMES.get(label, str(label)),
                "conf": round(float(seg.get("conf", 0.0)), 4),
                "order": int(seg.get("order", i)),
                "position": seg.get("position") or [0, 0],
            }
        )
    segments.sort(key=lambda s: s["order"])

    return {
        "ok": True,
        "configured": True,
        "human_ratio": round(float(ratios.get("0", 0.0)), 4),
        "ai_ratio": round(float(ratios.get("1", 0.0)), 4),
        "suspect_ratio": round(float(ratios.get("2", 0.0)), 4),
        "softmax_confidence": round(float(data.get("softmax_confidence", 0.0)), 4),
        "ratio_confidence": round(float(data.get("ratio_confidence", 0.0)), 4),
        "segments": segments,
        "tokens_used": int((data.get("makers_models_usage") or {}).get("total_tokens", 0)),
        "ms": int((time.time() - started) * 1000),
    }


def conclusion(human_ratio: float, ai_ratio: float, suspect_ratio: float) -> tuple[str, str]:
    """按三分类占比给一句官方报告单同款风格的结论。

    返回 (tone, text)：tone ∈ good|warn|bad（决定横幅配色）。
    阈值与官方报告单一致：官方样张里人工特征仅 10.76% 时给「人工创作特征较弱」，
    所以人工占比 <20% 即判 bad，不要求 AI 特征过半（官方把疑似 AI 也算风险侧）。
    """
    if human_ratio < 0.2:
        return "bad", "人工创作特征较弱"
    risk = ai_ratio + suspect_ratio
    if risk >= 0.5:
        return "warn", "疑似 AI 生成内容较多"
    if ai_ratio > 0.2 or suspect_ratio > 0.2:
        return "warn", "存在一定 AI 生成特征"
    return "good", "人工创作特征明显"
