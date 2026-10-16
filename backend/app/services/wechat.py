"""微信公众号 API 客户端（草稿箱推送）。

合规说明（重要）：
    微信《公众平台运营规范》明确禁止「通过脚本、程序托管等自动化方式
    替代真人完成内容创作、发布」。因此本客户端**只做草稿箱写入**，
    不做自动群发。

    草稿箱接口（draft/add）对个人订阅号同样开放，是官方接口，合规可用；
    发布接口（freepublish）个人号已被回收，且自动发布本身违规。

    所以终态是：自动进草稿 → 人工核对 → 人工群发。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.cache import get_cache
from app.core.config import settings
from app.core.logging import logger

WECHAT_API = "https://api.weixin.qq.com"

# 常见微信错误码 → 中文可读提示（验通/推送失败时直接展示给用户）
_WECHAT_ERR_HINT = {
    40001: "secret 无效，请核对 AppSecret",
    40013: "AppID 无效，请核对",
    40125: "AppSecret 无效，请核对",
    40164: "调用来源 IP 不在白名单：请把服务器出网 IP 加入公众号后台「IP 白名单」",
    45009: "接口调用超过限额",
    48001: "该接口未授权（个人订阅号部分接口不开放）",
}


def _client_kwargs(timeout: int) -> dict:
    """构造 httpx.AsyncClient 的公共参数。

    微信接口的出网 IP 会被平台校验（白名单），而沙箱/容器出网往往是**动态 IP 池**，
    无法稳定加白。这里支持把「仅微信请求」强制走固定出口（如经 SSH 隧道转发的
    SOCKS5 代理 `socks5://127.0.0.1:10800`，出口即那台固定公网 IP 的机器）。

    - `WECHAT_PROXY` 为空 → 直连（原行为，不做任何改变）；
    - 只影响本模块（微信）的请求，LLM 等其他出网流量不受影响。
    """
    kw: dict = {"timeout": timeout}
    proxy = (settings.WECHAT_PROXY or "").strip()
    if proxy:
        kw["proxy"] = proxy
    return kw


# 走代理（SSH 隧道）时，长连接/大文件上传偶发「Server disconnected」瞬断。
# 这类错误重试即可成功，不影响幂等性（拉 token 幂等；上传素材会产生孤儿素材但无害）。
# 值 = 总尝试次数（含首次）。
_PROXY_RETRY = 3


async def probe_egress_ip(timeout: int = 10) -> dict:
    """探测微信请求实际会用的出口 IP（用于系统状态页 / 排查白名单）。

    返回 {ok, ip?, via_proxy, error?}。直连时用公共回显服务查本机出口；
    走代理时同样走代理查，才能反映微信真正看到的来源 IP。
    """
    proxy = (settings.WECHAT_PROXY or "").strip()
    kw = _client_kwargs(timeout)
    last_err = ""
    for i in range(3):  # 公共回显服务偶发抖动，多试几次
        try:
            async with httpx.AsyncClient(**kw) as client:
                resp = await client.get("https://ifconfig.me/ip")
                ip = resp.text.strip()
            if ip:
                return {"ok": True, "ip": ip, "via_proxy": bool(proxy)}
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            await asyncio.sleep(0.5)
    return {"ok": False, "via_proxy": bool(proxy), "error": last_err}


def _friendly_error(data: dict) -> str:
    code = data.get("errcode")
    hint = _WECHAT_ERR_HINT.get(code)
    msg = data.get("errmsg", "")
    if hint:
        return f"{hint}（errcode={code}）"
    return f"微信返回错误 errcode={code} errmsg={msg}"



class WeChatClient:
    def __init__(self, appid: str | None = None, secret: str | None = None) -> None:
        # 不传则回退 .env（兼容老行为：只有一套全局凭证时的用法）
        self.appid = appid if appid is not None else settings.WECHAT_APPID
        self.secret = secret if secret is not None else settings.WECHAT_SECRET

    @property
    def configured(self) -> bool:
        return bool(self.appid and self.secret)

    async def check(self) -> dict[str, Any]:
        """连通性自检：拉一次 access_token 验证 appid/secret 是否有效。

        不抛异常，统一返回 {ok, error?}，方便系统状态页直接展示。
        """
        if not self.configured:
            return {"ok": False, "error": "appid 或 secret 为空"}
        try:
            # 绕过缓存直接请求，确保验证的是当前凭证而非缓存
            url = f"{WECHAT_API}/cgi-bin/token"
            params = {
                "grant_type": "client_credential",
                "appid": self.appid,
                "secret": self.secret,
            }
            async with httpx.AsyncClient(**_client_kwargs(15)) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
            if "access_token" not in data:
                # 微信错误码（如 40013 无效 appid / 40125 无效 secret / 40164 IP 不在白名单）
                return {
                    "ok": False,
                    "errcode": data.get("errcode"),
                    "error": _friendly_error(data),
                }
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"请求失败：{exc}"}

    async def _access_token(self) -> str:
        """获取 access_token（带缓存，微信侧有效期 7200 秒）。"""
        cache = get_cache()
        key = f"wechat:token:{self.appid}"
        cached = await cache.get(key)
        if cached:
            return cached["token"]

        url = f"{WECHAT_API}/cgi-bin/token"
        params = {"grant_type": "client_credential", "appid": self.appid, "secret": self.secret}
        async with httpx.AsyncClient(**_client_kwargs(15)) as client:
            resp = await client.get(url, params=params)
            data = resp.json()

        if "access_token" not in data:
            raise RuntimeError(f"获取 access_token 失败: {data}")

        # 提前 5 分钟过期，避免边界问题
        await cache.set(key, {"token": data["access_token"]}, ttl=data.get("expires_in", 7200) - 300)
        return data["access_token"]

    async def upload_image(self, file_path: str) -> str:
        """上传图片到微信素材库，返回可用的 url。

        ⚠️ 这步不能省：正文里的外链图片发布后会失效，
        必须先转存到微信自己的素材库。

        图片体积小，直接读进内存以便在代理瞬断时安全重试。
        """
        token = await self._access_token()
        url = f"{WECHAT_API}/cgi-bin/media/uploadimg?access_token={token}"
        data_bytes = Path(file_path).read_bytes()
        name = Path(file_path).name
        total = _PROXY_RETRY if settings.WECHAT_PROXY else 1
        data = None
        for i in range(total):
            try:
                async with httpx.AsyncClient(**_client_kwargs(60)) as client:
                    files = {"media": (name, data_bytes, "image/png")}
                    resp = await client.post(url, files=files)
                    data = resp.json()
                break
            except httpx.TransportError as exc:
                if i < total - 1:
                    logger.warning("图片上传经代理瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
                    await asyncio.sleep(0.8 * (i + 1))
                else:
                    raise
        if not data or "url" not in data:
            raise RuntimeError(f"图片上传失败: {data}")
        return data["url"]

    async def upload_thumb(self, file_path: str) -> str:
        """上传图片为**永久素材**，返回 thumb_media_id（草稿封面用）。

        正文配图走 upload_image（拿 url），封面走这里（拿 media_id），
        微信是两套接口，别混用。
        """
        token = await self._access_token()
        url = f"{WECHAT_API}/cgi-bin/material/add_material?access_token={token}&type=image"
        data_bytes = Path(file_path).read_bytes()
        name = Path(file_path).name
        total = _PROXY_RETRY if settings.WECHAT_PROXY else 1
        data = None
        for i in range(total):
            try:
                async with httpx.AsyncClient(**_client_kwargs(60)) as client:
                    files = {"media": (name, data_bytes, "image/png")}
                    resp = await client.post(url, files=files)
                    data = resp.json()
                break
            except httpx.TransportError as exc:
                if i < total - 1:
                    logger.warning("封面上传经代理瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
                    await asyncio.sleep(0.8 * (i + 1))
                else:
                    raise
        if not data or data.get("errcode", 0) != 0 or "media_id" not in data:
            raise RuntimeError(f"封面上传失败: {data}")
        return data["media_id"]

    async def transfer_inline_images(self, html: str, images: list[dict[str, Any]]) -> str:
        """把正文里的本地图片转存到微信素材库，替换成微信 url。

        为什么必须做：公众号发布后，正文里的**外链图片会失效**（含本地/内网地址），
        必须先上传到微信素材库换成它自己的 url。

        `images` 来自 content.images（[{url, path, alt}]）。本地 url 形如
        `/api/content/images/{cid}/{file}`，我们按其文件名在 html 里找到并替换。

        转存失败的图片保持原样（不阻断整体推送），由调用方决定是否提示。
        """
        if not images:
            return html
        from pathlib import Path

        out = html
        for img in images:
            local_url = img.get("url") or ""
            path = img.get("path") or ""
            if not local_url or not path:
                continue
            if local_url not in out:
                continue
            full = Path(settings.UPLOAD_DIR) / path
            if not full.exists():
                continue
            try:
                new_url = await self.upload_image(str(full))
                out = out.replace(local_url, new_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("正文图片转存失败（保留原地址）: %s", exc)
        return out

    async def add_draft(
        self,
        title: str,
        html: str,
        digest: str = "",
        thumb_media_id: str = "",
    ) -> dict[str, Any]:
        """创建草稿。

        html 必须是公众号兼容的内联样式片段（<section>…</section>），
        即 gzh-design 排版引擎的产物。
        thumb_media_id：封面图媒体 id（来自 upload_thumb）。留空则草稿无封面，
        用户可在后台补。
        """
        if not self.configured:
            return {"ok": False, "error": "未配置 WECHAT_APPID / WECHAT_SECRET"}

        try:
            token = await self._access_token()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        payload = {
            "articles": [
                {
                    "title": title,
                    "author": "",
                    "digest": digest,
                    "content": html,
                    "content_source_url": "",
                    "thumb_media_id": thumb_media_id,  # 封面（永久素材 id），可为空
                    "need_open_comment": 0,
                    "only_fans_can_comment": 0,
                }
            ]
        }
        url = f"{WECHAT_API}/cgi-bin/draft/add?access_token={token}"

        # 微信要求 body 用 UTF-8 编码，且不转义中文
        import json as _json

        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        total = _PROXY_RETRY if settings.WECHAT_PROXY else 1
        data = None
        try:
            for i in range(total):
                try:
                    async with httpx.AsyncClient(**_client_kwargs(30)) as client:
                        resp = await client.post(
                            url,
                            content=body,
                            headers={"Content-Type": "application/json"},
                        )
                        data = resp.json()
                    break
                except httpx.TransportError as exc:
                    if i < total - 1:
                        logger.warning("推送草稿经代理瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
                        await asyncio.sleep(0.8 * (i + 1))
                    else:
                        raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("推送草稿箱失败")
            return {"ok": False, "error": str(exc)}

        if not data or data.get("errcode", 0) != 0:
            return {
                "ok": False,
                "error": data.get("errmsg", "未知错误"),
                "errcode": data.get("errcode"),
                "hint": "常见原因：IP 不在白名单 / 凭证错误 / 接口权限未开通",
            }

        return {
            "ok": True,
            "media_id": data.get("media_id"),
            "message": "已推送到公众号草稿箱，请到后台核对后手动群发",
            "pushed_at": int(time.time()),
        }


# 需要人工确认才能执行的工具（供上层判断）
DANGEROUS_TOOLS = {"push_wechat_draft", "render_video"}
