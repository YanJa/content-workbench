"""模型池执行器：把「一个 ModelSpec + 输入」变成实际结果。

`services/llm.py` 已经把 `openai_chat`（文本对话，含流式）走通了。这份模块
补上剩下的四种协议——生图与向量化，它们不是 chat，走不了 OpenAI SDK：

| protocol          | 形态                            | 典型模型 |
|---|---|---|
| `openai_images`     | POST `{base}/images/generations` | 火山 / 硅基 / 百炼的文生图 |
| `openai_embeddings` | POST `{base}/embeddings`        | 同上，向量化 |
| `cf_ai_run_image`   | POST `{base}/ai/run/{model}`    | `@cf/black-forest-labs/flux-1-schnell` |
| `cf_ai_run_embed`   | POST `{base}/ai/run/{model}`    | `@cf/baai/bge-m3` |

统一约定：

- **失败抛 `ModelExecError`**，由调用方决定是切备用还是降级（不在这里吞）；
- **成功/失败都记账**：`bump_quota` 累加次数与产出，`mark_success` /
  `mark_failure` 维护熔断——否则池里的额度与熔断对生图/向量就完全是摆设；
- **解析容错照抄既有实现踩过的坑**：CF 的文生图返回单数字段 `image`（不是
  `images` 数组）、向量可能嵌套 `data.data`、单条输入返回一维数组、上游可能
  HTTP 200 但 `success: false`。这些坑在 `imagegen.py` / `embedding.py` 里
  各自踩过一遍，这里集中收口，避免每接一家厂商就重踩一次。
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Sequence

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.services.model_pool import (
    ModelSpec,
    bump_quota,
    mark_failure,
    mark_success,
)

#: 生图比对话慢得多（flux 一张 5~15s），向量化则要快
_TIMEOUT_IMAGE = 180.0
_TIMEOUT_EMBED = 60.0


class ModelExecError(RuntimeError):
    """模型池执行失败（网络错误 / 上游报错 / 返回结构异常）。

    调用方必须捕获：生图与向量化都是旁路能力，炸了不能连累主链路。
    """


def _client_kwargs(kind: str, spec: ModelSpec) -> dict:
    kw: dict = {"timeout": _TIMEOUT_IMAGE if kind == "image" else _TIMEOUT_EMBED}
    # 固定出口代理目前只针对 CF（直连 api.cloudflare.com 不稳定时开）
    proxy = (settings.CLOUDFLARE_PROXY or "").strip()
    if proxy and spec.provider_key == "cloudflare":
        kw["proxy"] = proxy
    return kw


def _endpoint(spec: ModelSpec, kind: str) -> str:
    """按协议拼出完整 URL。

    CF 的服务商 base_url 记的是 OpenAI 兼容入口 `.../ai/v1`，而 REST 端点
    是同级的 `.../ai/run/{model}` —— 这里剥掉 `/ai/v1` 再拼，管理员只要
    填一次账号地址，两种协议都能用。
    """
    base = (spec.base_url or "").rstrip("/")
    if not base:
        raise ModelExecError(f"服务商 {spec.provider_key} 未配置 base_url")
    if spec.protocol.startswith("cf_ai_run"):
        if base.endswith("/ai/v1"):
            base = base[: -len("/ai/v1")]
        return f"{base}/ai/run/{spec.model}"
    if kind == "image":
        return f"{base}/images/generations"
    return f"{base}/embeddings"


async def _post(spec: ModelSpec, payload: dict, kind: str) -> dict:
    url = _endpoint(spec, kind)
    headers = {
        "Authorization": f"Bearer {spec.api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(**_client_kwargs(kind, spec)) as cli:
            resp = await cli.post(url, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001 - 网络层异常一律转成 ModelExecError
        raise ModelExecError(f"请求失败：{exc}") from exc

    if resp.status_code >= 400:
        raise ModelExecError(f"上游返回 {resp.status_code}：{resp.text[:300]}")
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ModelExecError(f"上游返回非 JSON：{resp.text[:200]}") from exc
    if not isinstance(data, dict):
        raise ModelExecError(f"上游返回结构异常：{str(data)[:200]}")
    # CF 家族：HTTP 200 也可能是失败，错误在 body 里
    if data.get("success") is False:
        errs = data.get("errors") or []
        msg = errs[0].get("message") if errs else str(data)[:200]
        raise ModelExecError(f"上游返回错误：{msg}")
    return data


# ---------------------------------------------------------------- 生图

def _b64_to_bytes(b64: str) -> bytes:
    if "," in b64[:64]:  # 可能带 data:image/png;base64, 前缀
        b64 = b64.split(",", 1)[1]
    try:
        return base64.b64decode(b64)
    except (binascii.Error, ValueError) as exc:
        raise ModelExecError(f"图片解码失败：{exc}") from exc


def _extract_images(data: dict) -> list[bytes]:
    """从各家响应里抠出图片字节。返回空列表视为异常，由调用方报错。"""
    out: list[bytes] = []

    # OpenAI 兼容：{"data": [{"b64_json": ...} | {"url": ...}]}
    items = data.get("data")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("b64_json"):
                out.append(_b64_to_bytes(str(it["b64_json"])))
        return out

    # CF REST：{"result": {"images": [...]} } 或 {"result": {"image": "..."}}
    # 实测 flux-1-schnell 返回的是单数字段 image，只认 images 会把好图当异常扔掉。
    result = data.get("result") or {}
    if isinstance(result, dict):
        images = result.get("images")
        if not images:
            single = result.get("image")
            if isinstance(single, str) and single:
                images = [single]
        if isinstance(images, list):
            for b64 in images:
                if isinstance(b64, str) and b64:
                    out.append(_b64_to_bytes(b64))
            return out
    # 少数网关直接把列表放在顶层
    top = data.get("images")
    if isinstance(top, list):
        for b64 in top:
            if isinstance(b64, str) and b64:
                out.append(_b64_to_bytes(b64))
    return out


async def run_image(
    spec: ModelSpec, prompt: str, n: int = 1, size: str = ""
) -> list[bytes]:
    """按 spec 的协议生成 n 张图，返回图片字节列表。

    张数策略随协议走：OpenAI 兼容端点认 `n` 参数，一次要齐；CF 的
    `/ai/run` 一次只出一张，就循环调（每张单独记账，额度才算得准）。
    """
    n = max(1, int(n or 1))
    is_openai = spec.protocol == "openai_images"
    out: list[bytes] = []
    # 循环上限：OpenAI 端最多补 3 轮（有时返回的比 n 少）；CF 端一轮一张
    guard = 0
    while len(out) < n and guard < (3 if is_openai else n):
        guard += 1
        if is_openai:
            payload: dict[str, Any] = {
                "model": spec.model,
                "prompt": prompt,
                "n": n - len(out),
            }
            if size:
                payload["size"] = size
        else:
            payload = {"prompt": prompt}
        try:
            data = await _post(spec, payload, "image")
            got = _extract_images(data)
        except ModelExecError as exc:
            await mark_failure(spec, str(exc))
            raise
        if not got:
            await mark_failure(spec, "上游未返回任何图片")
            raise ModelExecError(f"上游未返回任何图片：{str(data)[:300]}")
        out.extend(got)
        await bump_quota(spec, images=len(got))
    await mark_success(spec)
    logger.info("模型池生图: %s / %s 产出 %d 张", spec.provider_key, spec.model, len(out))
    return out[:n]


# ---------------------------------------------------------------- 向量化

def _extract_vectors(data: dict) -> tuple[list[list[float]], int]:
    """从各家响应里抠出二维向量数组与（拿得到的）输入 token 数。"""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)

    vectors: Any = None
    # OpenAI 兼容：{"data": [{"embedding": [...], "index": n}, ...]}
    items = data.get("data")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        rows = sorted(
            [it for it in items if isinstance(it.get("embedding"), list)],
            key=lambda it: int(it.get("index") or 0),
        )
        vectors = [it["embedding"] for it in rows]
    else:
        # CF REST：{"result": {"data": [[...], ...], "shape": [n, dim]}}
        result = data.get("result") or {}
        vectors = result.get("data") if isinstance(result, dict) else None
        if isinstance(vectors, dict):  # 有的网关多包一层
            vectors = vectors.get("data")

    if not isinstance(vectors, list) or not vectors:
        raise ModelExecError(f"上游返回结构异常：{str(data)[:300]}")
    # 单条输入时部分模型返回一维数组，补回外层
    if not isinstance(vectors[0], list):
        vectors = [vectors]

    out: list[list[float]] = []
    for v in vectors:
        try:
            # 4 位小数足够（余弦相似度对这点精度完全不敏感），
            # 1024 维 × 每片存库能省一半以上的体积
            out.append([round(float(x), 4) for x in v])
        except (TypeError, ValueError) as exc:
            raise ModelExecError(f"向量内容非数值：{exc}") from exc
    return out, tokens


async def run_embed(spec: ModelSpec, texts: Sequence[str]) -> list[list[float]]:
    """按 spec 的协议把一批文本转成向量。

    返回与入参等长的向量列表；上游少给就报错——半截结果存进库里比报错更难查。
    """
    texts = [t for t in texts]
    if not texts:
        return []
    if spec.protocol == "openai_embeddings":
        payload: dict[str, Any] = {"model": spec.model, "input": texts}
    else:
        payload = {"text": texts}
    try:
        data = await _post(spec, payload, "embed")
        vecs, tokens = _extract_vectors(data)
    except ModelExecError as exc:
        await mark_failure(spec, str(exc))
        raise
    if len(vecs) < len(texts):
        err = f"只返回 {len(vecs)} 条向量，期望 {len(texts)} 条"
        await mark_failure(spec, err)
        raise ModelExecError(err)
    await bump_quota(spec, tokens=tokens)
    await mark_success(spec)
    return vecs[: len(texts)]
