"""火山引擎 Signature V4 签名与请求封装。

为什么自己写而不引官方 SDK：
    官方 SDK（volcengine-python-sdk）依赖链重（十几层传递依赖），而我们只需要
    一个纯粹的 HTTP 签名。更关键的是——本项目已经在用它家的**视觉智能**服务
    （小云雀生视频、Seedream 生图都挂在 `visual.volcengineapi.com` 的 `cv`
    服务下），同一套签名逻辑后面还会被反复使用，写清楚比塞个黑箱更划算。

签名流程（官方文档 docs/6369/67269）：
    1. CanonicalRequest = Method \\n URI \\n Query \\n Headers(以\\n结尾) \\n SignedHeaders \\n SHA256(Body)
    2. StringToSign = "HMAC-SHA256" \\n X-Date \\n CredentialScope \\n SHA256(CanonicalRequest)
    3. kSigning = HMAC 链：kSecret -> kDate -> kRegion -> kService -> "request"
    4. Authorization = "HMAC-SHA256 Credential={AK}/{scope}, SignedHeaders={...}, Signature={sig}"

⚠️ 两个最容易写错、且错了只会得到 400 "InvalidSignature" 的点：
    - CanonicalHeaders **自带结尾换行**，再拼 SignedHeaders 时不能额外补分隔符，
      否则会多出一个空行（现象是这个空行其实是对的，多补才是错的——文档里那句
      「\n\n 是正常现象」说的就是它）。
    - 密钥派生链里，第一步用 SecretKey 的**原始字符串**做 key，后续几步都拿上一步
      的**十六进制字符串**当 key。混用结果完全对不上。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings

logger = logging.getLogger("workbench")

# 视觉智能服务的固定值（小云雀、Seedream、即梦同款都走这套）
VISUAL_HOST = "visual.volcengineapi.com"
VISUAL_REGION = "cn-north-1"
VISUAL_SERVICE = "cv"
VISUAL_VERSION = "2022-08-31"


class VolcError(RuntimeError):
    """火山引擎调用失败。message 里带 request_id —— 没有它，官方工单都提不了。"""

    def __init__(self, message: str, *, code: int | None = None, request_id: str = "") -> None:
        self.code = code
        self.request_id = request_id
        tail = f"（code={code}, request_id={request_id}）" if code else ""
        super().__init__(f"{message}{tail}")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str) -> str:
    """RFC3986 编码。

    quote() 默认不转 `/`，而 RFC3986 要求斜杠也要编码——这点在 query 参数里
    尤其要命（`~15s` 这种值里的 `~` 反倒不该编码）。safe="" 让它全部编码，
    再手工放回 unreserved 字符集里被 quote 误伤的 `~`（Python 3.7+ 已不编码
    `~`，这里只是保持显式）。
    """
    return quote(value, safe="-_.~")


def canonical_query(params: dict[str, str]) -> str:
    """按参数名 ASCII 升序，逐个 `k=v` 用 `&` 连接。"""
    if not params:
        return ""
    parts = []
    for key in sorted(params):
        parts.append(f"{_uri_encode(key)}={_uri_encode(str(params[key]))}")
    return "&".join(parts)


def sign(
    *,
    access_key: str,
    secret_key: str,
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    body: bytes,
    region: str,
    service: str,
    content_type: str = "application/json",
    x_date: str | None = None,
) -> dict[str, str]:
    """算出一组可直接塞进 httpx 的请求头（含 Authorization）。

    返回：Host / X-Date / X-Content-Sha256 / Content-Type / Authorization。

    `x_date` 只在**测试**时传：签名强依赖时间戳，不注入就没法用固定向量做回归。
    生产调用一律留空，由函数自己取当前 UTC 时间。
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    x_date = x_date or now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = _sha256_hex(body)

    # 只签 host 与 x-date。
    #
    # 官方 Java SDK 示例把 content-type 和 x-content-sha256 也纳入了签名，两条路
    # 都能通过（已用官方示例向量验证：只签 host;x-date 才是文档里那组期望签名的
    # 算法）。选最小集是刻意的——反之，一旦某个代理/网关给 Content-Type 补个
    # `; charset=utf-8`，签名立刻失效，而报错只会说 InvalidSignature，极难排查。
    # x-content-sha256 仍然发出去（服务端可据此校验 body），只是不进签名。
    signed = {"host": host, "x-date": x_date}
    signed_headers = ";".join(sorted(signed))
    canonical_headers = "".join(f"{k}:{signed[k].strip()}\n" for k in sorted(signed))

    canonical_request = "\n".join(
        [
            method.upper(),
            path or "/",
            canonical_query(query),
            canonical_headers,  # 自带结尾 \n，这里不再补
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join(
        ["HMAC-SHA256", x_date, scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )

    # 派生链：**全程用原始字节**。
    #
    # 官方文档说「后三步将上一步结果作为 Hex 类型密钥传入」，这句话极具误导性，
    # 照字面理解（把 hex 字符串当 key）算出来的签名一定错。实测正确的做法是
    # 每一步都直接拿上一步 HMAC 输出的 raw bytes 当 key。已用官方示例向量
    # 逐层核对，kRegion/kService/kSigning 三级全部吻合。
    k_date = _hmac(secret_key.encode("utf-8"), short_date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Content-Type": content_type,
        "Authorization": (
            f"HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


def configured() -> bool:
    """是否配了火山凭证。没配时前端不显示小云雀入口，而不是点下去才报错。"""
    return bool((settings.VOLC_ACCESS_KEY or "").strip()) and bool(
        (settings.VOLC_SECRET_KEY or "").strip()
    )


async def call_visual(
    action: str,
    body: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """调视觉智能的一个 Action，返回 `data` 字段。

    错误处理只做一件事：把 `code != 10000` 翻成人话并带上 request_id。
    10000 是火山的成功码，其余全部是失败——包括 HTTP 200 但业务失败的情况，
    所以不能只看 status_code。
    """
    if not configured():
        raise VolcError("未配置火山引擎 Access Key / Secret Key")

    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    query = {"Action": action, "Version": VISUAL_VERSION}
    headers = await asyncio.to_thread(
        sign,
        access_key=settings.VOLC_ACCESS_KEY.strip(),
        secret_key=settings.VOLC_SECRET_KEY.strip(),
        method="POST",
        host=VISUAL_HOST,
        path="/",
        query=query,
        body=payload,
        region=settings.VOLC_REGION or VISUAL_REGION,
        service=VISUAL_SERVICE,
    )

    url = f"https://{VISUAL_HOST}/"
    try:
        async with httpx.AsyncClient(timeout=timeout or settings.VOLC_TIMEOUT) as cli:
            resp = await cli.post(url, params=query, content=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise VolcError(f"请求火山引擎失败：{exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        # 签名错时火山会返回一个没有 JSON body 的 4xx，这里要给出可读线索
        raise VolcError(
            f"火山引擎返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}"
        ) from None

    code = data.get("code")
    if code != 10000:
        msg = data.get("message") or "调用失败"
        raise VolcError(
            _friendly(int(code or 0), str(msg)),
            code=int(code or 0),
            request_id=str(data.get("request_id") or ""),
        )
    return data.get("data") or {}


# 官方错误码表里常见几项翻成人话。没列到的保留原始 message —— 瞎猜比不猜更糟，
# 至少原始英文能拿去搜。
_ERR_HINT = {
    50411: "输入图片审核未通过",
    50412: "输入文本审核未通过（可能是提示词含敏感词）",
    50413: "输入文本含敏感词或版权词，审核未通过",
    50511: "输出图片审核未通过，可重试",
    50512: "输出文本审核未通过",
    50513: "输入视频审核未通过",
    50514: "输入音频审核未通过",
    50429: "请求过于频繁（QPS 超限），稍后重试",
    50430: "并发超限，稍后重试",
    50500: "火山引擎内部错误，可重试",
    50501: "算法内部错误，可重试",
}


def _friendly(code: int, msg: str) -> str:
    """错误码 → 中文提示。保留原始 message 附在后面，便于排查。"""
    hint = _ERR_HINT.get(code)
    if hint:
        return f"{hint}（原始信息：{msg}）" if msg and msg != hint else hint
    return msg
