"""文本向量化（embedding）。

知识库检索要把「用户问的话」和「素材片段」都变成向量再比余弦相似度。

通道优先级（2026-10-24 模型池上线后）：

    1. **模型池的 embedding 槽** —— 可以是 CF 的 bge-m3，也可以是火山 /
       硅基 / 百炼 / 千帆 的任何嵌入模型，管理员在「模型池」页配；
    2. **.env 里的 Cloudflare 直连** —— 兜底，保证池没配时不至于整个知识库
       瘫痪（接口形态 POST /ai/run/{model}，含可选的固定出口代理）。

为什么历史实现只认 CF：DeepSeek 侧没有 embedding 端点；CF 免费额度里的
bge-small / bge-base / bge-large-en-v1.5 全是英文模型，中文素材喂进去相似度
失真到没法用；bge-m3 支持 100+ 语言、1024 维，是 CF 目录里唯一能打中文的。

**关键约束：换 embedding 模型 = 库里已有向量全部作废。** 不同模型（甚至同
模型不同维度）产出的向量不在同一个空间里，混着比余弦相似度等于比噪声。所以
本模块每次都把「这条向量是哪个模型生成的」返回给调用方，`services/knowledge.py`
据此隔离检索、并在换模型时把旧向量标成 stale。

设计原则：**本模块只负责「把文本变成向量」**，不做缓存、不做降级打分。
失败一律抛 `EmbeddingError`，由 `services/knowledge.py` 决定是重试还是
退化成关键词检索 —— 向量化不通绝不能把对话写稿挡住。
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import httpx

from app.core.config import settings
from app.core.logging import logger


class EmbeddingError(RuntimeError):
    """向量化失败（未配置 / 网络错误 / 返回结构异常）。

    调用方应捕获它并降级，不要把异常透到写稿链路上。
    """


class EmbeddingNotConfigured(EmbeddingError):
    """没配凭证，压根不该发请求（省一次无谓的超时等待）。"""


def cf_configured() -> bool:
    """仅 .env 里的 Cloudflare 兜底通道是否可用。"""
    return bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID)


def configured() -> bool:
    """是否配置了可用的向量化通道（同步版，只认 .env 兜底通道）。

    注意：这个回答不了「模型池里配了没」——那是异步的（要读库/读快照）。
    需要准确判断请用 `await ready(owner, db)`。
    """
    return cf_configured()


def fallback_model() -> str:
    """.env 兜底通道的模型名。"""
    return settings.CLOUDFLARE_EMBED_MODEL


async def current_model(owner: str | None = None, db: Any = None) -> str:
    """**当前实际会用的** embedding 模型名（池优先，兜底其次）。

    这个值会写进每条 chunk 的 `embed_model`，检索时按它过滤——所以它必须
    是「真会用它去算向量的那个模型」，不能是「配置里填的那个」。
    """
    try:
        from app.services.model_pool import resolve_for

        spec = await resolve_for("embedding", owner, db)
        if spec is not None and spec.model:
            return spec.model
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析 embedding 模型失败，用兜底模型名: %s", exc)
    return fallback_model()


async def ready(owner: str | None = None, db: Any = None) -> bool:
    """是否存在可用的向量化通道（池 或 兜底）。"""
    if cf_configured():
        return True
    try:
        from app.services.model_pool import resolve_for

        return await resolve_for("embedding", owner, db) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型池向量通道探测失败: %s", exc)
        return False


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


async def embed_with_model(
    texts: Sequence[str], owner: str | None = None, db: Any = None
) -> tuple[list[list[float]], str]:
    """向量化并返回 `(向量列表, 实际使用的模型名)`。

    调用方**必须**把返回的模型名存进 chunk 的 `embed_model` —— 检索要按它
    过滤，否则换模型后新旧向量混在一起比余弦，结果是噪声。
    """
    texts = [t for t in texts]
    if not texts:
        return [], await current_model(owner, db)

    # 1) 模型池优先
    spec = None
    try:
        from app.services.model_pool import resolve_for

        spec = await resolve_for("embedding", owner, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析 embedding 模型失败，回落 .env 通道: %s", exc)
    if spec is not None:
        try:
            from app.services import model_exec

            vecs = await model_exec.run_embed(spec, texts)
            return vecs, spec.model
        except Exception as exc:  # noqa: BLE001
            # 池里的模型挂了：只有 .env 兜底通道在时才回落，否则把错误抛上去
            if not cf_configured():
                raise EmbeddingError(f"向量化失败（{spec.model}）：{exc}") from exc
            logger.warning("模型池向量化失败，回落 .env 通道: %s", exc)

    # 2) .env 兜底（原实现，行为不变）
    vecs = await _embed_via_cloudflare(texts)
    return vecs, fallback_model()


async def embed_texts(
    texts: Sequence[str], owner: str | None = None, db: Any = None
) -> list[list[float]]:
    """把一批文本转成向量，返回与入参等长的向量列表（`embed_with_model` 的薄封装）。"""
    vecs, _model = await embed_with_model(texts, owner=owner, db=db)
    return vecs


async def _embed_via_cloudflare(texts: Sequence[str]) -> list[list[float]]:
    """Cloudflare Workers AI 向量化（.env 兜底通道）。

    bge-m3 的入参是 `{"text": [...]}`（也有实现收单个字符串，这里统一走数组），
    返回形如 `{"result": {"data": [[0.01, -0.02, ...], ...], "shape": [n, 1024]}}`。
    """
    if not texts:
        return []
    if not cf_configured():
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


async def embed_one(
    text: str, owner: str | None = None, db: Any = None
) -> list[float]:
    """单条文本向量化（批量接口的薄封装）。"""
    vecs = await embed_texts([text], owner=owner, db=db)
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
