"""文本向量化（embedding）。

知识库检索要把「用户问的话」和「素材片段」都变成向量再比余弦相似度。
目前唯一的可用通道是 Cloudflare Workers AI 的 bge-m3：

  - DeepSeek 侧**没有** embedding 端点（只有 chat），指望不上；
  - CF 免费额度里的 bge-small / bge-base / bge-large-en-v1.5 全是
    **英文**模型，中文素材喂进去相似度失真到没法用；
  - bge-m3 支持 100+ 语言、1024 维，是 CF 目录里唯一能打中文的选择。

接口形态与文生图一致（POST /ai/run/{model}），写法照抄
`services/imagegen.py::_run_cloudflare`，包括可选的固定出口代理。

设计原则：**本模块只负责「把文本变成向量」**，不做缓存、不做降级打分。
失败一律抛 `EmbeddingError`，由 `services/knowledge.py` 决定是重试还是
退化成关键词检索 —— 向量化不通绝不能把对话写稿挡住。
"""
from __future__ import annotations

import math
from typing import Sequence

import httpx

from app.core.config import settings
from app.core.logging import logger


class EmbeddingError(RuntimeError):
    """向量化失败（未配置 / 网络错误 / 返回结构异常）。

    调用方应捕获它并降级，不要把异常透到写稿链路上。
    """


class EmbeddingNotConfigured(EmbeddingError):
    """没配凭证，压根不该发请求（省一次无谓的超时等待）。"""


def configured() -> bool:
    """是否配置了可用的向量化通道。"""
    return bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID)


def model_name() -> str:
    return settings.CLOUDFLARE_EMBED_MODEL


def _url() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.CLOUDFLARE_ACCOUNT_ID}/ai/run/{settings.CLOUDFLARE_EMBED_MODEL}"
    )


def _client_kwargs() -> dict:
    kw: dict = {"timeout": settings.CLOUDFLARE_EMBED_TIMEOUT}
    proxy = (settings.CLOUDFLARE_PROXY or "").strip()
    if proxy:
        kw["proxy"] = proxy
    return kw


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """把一批文本转成向量，返回与入参等长的向量列表。

    bge-m3 的入参是 `{"text": [...]}`（也有实现收单个字符串，这里统一走数组），
    返回形如 `{"result": {"data": [[0.01, -0.02, ...], ...], "shape": [n, 1024]}}`。
    """
    if not texts:
        return []
    if not configured():
        raise EmbeddingNotConfigured(
            "未配置 Cloudflare（CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID）"
        )

    payload = {"text": [t for t in texts]}
    headers = {
        "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(**_client_kwargs()) as client:
            resp = await client.post(_url(), headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"Cloudflare 向量化请求失败：{exc}") from exc

    if not data.get("success", True):
        errs = data.get("errors") or []
        msg = errs[0].get("message") if errs else str(data)[:200]
        raise EmbeddingError(f"Cloudflare 向量化返回错误：{msg}")

    result = data.get("result") or {}
    vectors = result.get("data")
    # 有的模型/网关返回 {"data": {"data": [...]}}，兜一层
    if isinstance(vectors, dict):
        vectors = vectors.get("data")
    if not isinstance(vectors, list) or not vectors:
        raise EmbeddingError(f"Cloudflare 返回结构异常：{str(data)[:300]}")

    # 单条输入时部分模型返回的是一维数组，补回外层
    if vectors and not isinstance(vectors[0], list):
        vectors = [vectors]

    if len(vectors) < len(texts):
        raise EmbeddingError(
            f"Cloudflare 只返回 {len(vectors)} 条向量，期望 {len(texts)} 条"
        )

    out: list[list[float]] = []
    for v in vectors[: len(texts)]:
        try:
            # 4 位小数足够（余弦相似度对这点精度完全不敏感），
            # 1024 维 × 每片存库能省一半以上的体积
            out.append([round(float(x), 4) for x in v])
        except (TypeError, ValueError) as exc:
            raise EmbeddingError(f"向量内容非数值：{exc}") from exc
    logger.debug("向量化完成: %d 条 / %s", len(out), settings.CLOUDFLARE_EMBED_MODEL)
    return out


async def embed_one(text: str) -> list[float]:
    """单条文本向量化（批量接口的薄封装）。"""
    vecs = await embed_texts([text])
    if not vecs:
        raise EmbeddingError("向量化返回空结果")
    return vecs[0]


def norm_of(vec: Sequence[float]) -> float:
    """向量模长。检索时预存它，余弦 = 点积 / (|a|·|b|) 就只剩一次除法。"""
    return math.sqrt(sum(x * x for x in vec)) or 1.0


def cosine(a: Sequence[float], na: float, b: Sequence[float], nb: float) -> float:
    """已知模长的两个向量的余弦相似度。

    向量长度不一致（换过 embedding 模型）时返回 0，而不是抛异常 ——
    检索是旁路，宁可漏召回也不能炸。
    """
    if len(a) != len(b):
        return 0.0
    denom = (na or 1.0) * (nb or 1.0)
    if denom <= 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / denom
