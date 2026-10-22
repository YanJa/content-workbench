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



def _replace_src_by_filename(
    html: str, fname: str, new_url: str | None = None
) -> tuple[str, int]:
    """把 html 里所有指向文件名为 fname 的 `<img src="...">` 整体替换为 new_url。

    返回 (新 html, 命中次数)。new_url 传 None 时只探测不改动（调用方用于
    先判断 html 里有没有这个文件的引用，避免白上传）。

    ⚠️ 正则必须容忍文件名后面的查询串：本地图片地址是 HMAC 签名直链
    （`?o={owner}&sig=...`，见 core/security.link_url），且 `&` 在 html 里
    会被转义成 `&amp;`。三个坑都真实踩过：
    1. 早期写成 `src="([^"]*fname)"`，要求文件名紧贴结束引号——签名直链
       上线后查询串挡在中间，**全部静默失配**，正文图一张都没转存，微信
       抓不到图，草稿正文全是空的（只剩封面，因为封面走另一条链路）。
    2. 匹配必须把整个属性值（含查询串）一起换掉，只在文件名处截断替换的
       话，微信 url 后面会挂着无效签名参数，图照样裂。
    3. 只匹配**站点相对路径**（`/` 开头）的 src：本地引用一定是 `/api/...`，
       而微信 CDN 是绝对地址（https://mmbiz.qpic.cn/...）且**可能含原文件名**。
       不设这道守卫的话，对已转存过的 html 再跑一次会把微信 url 也当本地
       引用重复上传——守卫同时让本函数幂等。
    """
    if not fname:
        return html, 0
    import re

    # 兼容单/双引号；属性值内不会出现裸引号，[^"'] 不会跨属性边界
    pat = re.compile(r"src=([\"'])/[^\"']*" + re.escape(fname) + r"[^\"']*\1")
    matches = list(pat.finditer(html))
    if new_url is None or not matches:
        return html, len(matches)
    out = html
    # 从后往前替换，避免偏移失效；同一张图在正文出现多次只算一次上传
    for m in reversed(matches):
        q = m.group(1)
        out = out[: m.start()] + f"src={q}{new_url}{q}" + out[m.end() :]
    return out, len(matches)


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

        两条路径互补：
        1. **扫正文**（主路径）：本地图片地址形如
           `/api/content/images/{cid}/{file}?o=...&sig=...`（HMAC 签名直链，
           映射规则见 routers/layout.serve_image：UPLOAD_DIR/gen/{cid}/{file}），
           直接从 html 里提取引用。正文才是「有哪些图」的权威来源——
           content.images 元数据会与正文漂移（实测：正文引了图元数据没记、
           元数据记了图正文没引，两种都有）。
        2. **扫元数据**（兜底）：content.images 里记了、磁盘上还在的图，
           若正文里仍有它的本地引用（URL 形态不规范没被路径 1 覆盖到的），
           按文件名再兜一次。

        替换统一走 _replace_src_by_filename（整体换掉 src 属性值，含查询串），
        踩坑记录见那个函数的文档。
        """
        if not html:
            return html
        import re
        from pathlib import Path

        out = html

        # 1) 扫正文：/api/content/images/{cid}/{fname}（容忍签名查询串与 &amp;）
        scan = re.compile(
            r"src=([\"'])(/api/content/images/([^/\"'?]+)/([^\"'?]+))[^\"']*\1"
        )
        seen: dict[str, str] = {}  # fname -> 磁盘绝对路径
        for m in scan.finditer(out):
            cid, fname = m.group(3), m.group(4)
            if fname in seen:
                continue
            full = Path(settings.UPLOAD_DIR) / "gen" / cid / fname
            if full.exists():
                seen[fname] = str(full)
        for fname, full in seen.items():
            try:
                new_url = await self.upload_image(full)
            except Exception as exc:  # noqa: BLE001
                logger.warning("正文图片转存失败（保留原地址）: %s", exc)
                continue
            out, n = _replace_src_by_filename(out, fname, new_url)
            logger.info("正文图片转存: %s → %d 处引用已替换", fname, n)

        # 2) 扫元数据兜底
        for img in images or []:
            path = img.get("path") or ""
            if not path:
                continue
            fname = Path(path).name
            full = Path(settings.UPLOAD_DIR) / path
            if not full.exists():
                continue
            # 先探一下 html 里还有没有这个文件的本地引用，没有就跳过，别白上传
            _, hits = _replace_src_by_filename(out, fname)
            if hits == 0:
                continue
            try:
                new_url = await self.upload_image(str(full))
            except Exception as exc:  # noqa: BLE001
                logger.warning("正文图片转存失败（保留原地址）: %s", exc)
                continue
            out, n = _replace_src_by_filename(out, fname, new_url)
            logger.info("正文图片转存（元数据兜底）: %s → %d 处引用已替换", fname, n)

        # 3) 仍有残留本地引用的（文件不在磁盘上了）——记一笔，推送前能排查
        leftover = len(scan.findall(out))
        if leftover:
            logger.warning(
                "正文仍有 %d 处本地图片未能转存（文件已不在磁盘，推送后会缺图，"
                "建议重新「加工排版」生成正文）", leftover,
            )
        return out

    async def add_draft(
        self,
        title: str,
        html: str,
        digest: str = "",
        thumb_media_id: str = "",
        author: str = "",
    ) -> dict[str, Any]:
        """创建草稿。

        html 必须是公众号兼容的内联样式片段（<section>…</section>），
        即 gzh-design 排版引擎的产物。
        thumb_media_id：封面图媒体 id（来自 upload_thumb）。
        **微信要求每篇草稿必须有封面**：实测空串 / 省略该字段都返回
        40007「invalid media_id」（2026-10-21 探测确认），没有“无封面草稿”
        这种状态。所以这里显式拦截并给出可操作的提示，而不是把微信的
        40007 原样抛给用户。
        """
        if not self.configured:
            return {"ok": False, "error": "未配置 WECHAT_APPID / WECHAT_SECRET"}
        if not thumb_media_id:
            return {
                "ok": False,
                "error": "缺少封面图：微信要求每篇草稿必须有封面",
                "hint": "先在队列里点「加工排版」生成配图（首图会自动用作封面）后再推送",
            }

        try:
            token = await self._access_token()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        payload = {
            "articles": [
                {
                    "title": title,
                    "author": author or "",
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

    async def delete_draft(self, media_id: str) -> dict[str, Any]:
        """删除草稿箱里的草稿（draft/delete）。

        media_id 即 add_draft 返回的草稿 id。删除后：
          - 本地 queue item 的 wechat_media_id 必须同步清空（由上层负责），
            否则记录仍显示「已推送」，而微信侧草稿已不存在，用户会再次推送
            或误以为群发过了。
          - 微信侧删除不可恢复（草稿箱没有回收站），必须前端二次确认后才调。
        """
        if not self.configured:
            return {"ok": False, "error": "未配置 WECHAT_APPID / WECHAT_SECRET"}
        if not media_id:
            return {"ok": False, "error": "缺少 media_id"}

        try:
            token = await self._access_token()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        payload = {"media_id": media_id, "index": 0}
        url = f"{WECHAT_API}/cgi-bin/draft/delete?access_token={token}"

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
                        logger.warning("删除草稿经代理瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
                        await asyncio.sleep(0.8 * (i + 1))
                    else:
                        raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除草稿失败")
            return {"ok": False, "error": str(exc)}

        if not data or data.get("errcode", 0) != 0:
            errcode = data.get("errcode")
            errmsg = data.get("errmsg", "未知错误")
            hint = ""
            if errcode == 40007:
                hint = "media_id 无效或草稿已不存在（可能已被删除）"
            return {
                "ok": False,
                "error": errmsg,
                "errcode": errcode,
                "hint": hint or "常见原因：IP 不在白名单 / 凭证错误 / 接口权限未开通",
            }

        logger.info("删除草稿成功: media_id=%s", media_id)
        return {
            "ok": True,
            "media_id": media_id,
            "message": "已从公众号草稿箱删除，本地记录恢复为可推送状态",
        }


# ---------------------------------------------------------------- 流量主 / 广告分析
# 广告位枚举 → 中文名。公众号与小程序的广告位都在这里，未知枚举回落原始名。
SLOT_NAME_MAP: dict[str, str] = {
    "SLOT_ID_BIZ_BOTTOM": "底部广告",
    "SLOT_ID_BIZ_MID_CONTEXT": "文中广告",
    "SLOT_ID_BIZ_VIDEO_END": "视频后贴",
    "SLOT_ID_BIZ_SPONSOR": "互选广告",
    "SLOT_ID_BIZ_CPS": "返佣商品",
    "SLOT_ID_WEAPP_COVER": "封面广告",
    "SLOT_ID_WEAPP_TEMPLATE": "原生模板",
    "SLOT_ID_WEAPP_REWARD_VIDEO": "激励视频",
    "SLOT_ID_WEAPP_INTERSTITIAL": "插屏广告",
    "SLOT_ID_WEAPP_BANNER": "横幅广告",
    "SLOT_ID_WEAPP_VIDEO_FEEDS": "视频广告",
    "SLOT_ID_WEAPP_VIDEO_BEGIN": "视频前贴",
    "SLOT_ID_WEAPP_BOX": "格子广告",
}

# 取中文名：未知枚举回落原始字符串，绝不返回空。
def slot_name(ad_slot: str | None) -> str:
    if not ad_slot:
        return "其他"
    return SLOT_NAME_MAP.get(ad_slot, ad_slot)


async def _get_json(self, url: str, params: dict) -> dict:
    """GET 并解析 JSON，代理瞬断时重试（与 _PROXY_RETRY 一致）。"""
    total = _PROXY_RETRY if settings.WECHAT_PROXY else 1
    last_exc: Exception | None = None
    for i in range(total):
        try:
            async with httpx.AsyncClient(**_client_kwargs(20)) as client:
                resp = await client.get(url, params=params)
                return resp.json()
        except httpx.TransportError as exc:  # 代理瞬断，重试即可
            last_exc = exc
            if i < total - 1:
                logger.warning("微信 GET 经代理瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
                await asyncio.sleep(0.8 * (i + 1))
            else:
                raise
    if last_exc:
        raise last_exc
    return {}


async def get_adpos_general(
    self, start_date: str, end_date: str, page: int = 1, page_size: int = 90
) -> dict:
    """获取分广告位天级数据（publisher_adpos_general）。

    ⚠️ 参数名是 `start_date` / `end_date`（不是 begin_date）——传错微信不报错、
    只静默返回空数据（total_num=0），这是最容易踩的坑。
    最大时间跨度 90 天，每页最多 90 条。
    """
    token = await self._access_token()
    params = {
        "action": "publisher_adpos_general",
        "access_token": token,
        "start_date": start_date,
        "end_date": end_date,
        "page": page,
        "page_size": page_size,
    }
    data = await _get_json(self, f"{WECHAT_API}/publisher/stat", params)
    if data.get("base_resp", {}).get("ret", -1) != 0:
        raise RuntimeError(f"流量主广告数据失败：{data.get('base_resp')}")
    return data


async def get_settlement(
    self, start_date: str, end_date: str, page: int = 1, page_size: int = 90
) -> dict:
    """获取结算收入数据（publisher_settlement）。

    结算数据是累计口径，日期区间只用于圈定「重合的结算区间」，不影响累计总额。
    返回 revenue_all（累计收入·分）、settled_revenue_all（已结算·分）、
    penalty_all（扣除·分）、settlement_list（各结算区间明细）。
    """
    token = await self._access_token()
    params = {
        "action": "publisher_settlement",
        "access_token": token,
        "start_date": start_date,
        "end_date": end_date,
        "page": page,
        "page_size": page_size,
    }
    data = await _get_json(self, f"{WECHAT_API}/publisher/stat", params)
    if data.get("base_resp", {}).get("ret", -1) != 0:
        raise RuntimeError(f"流量主结算数据失败：{data.get('base_resp')}")
    return data


async def fetch_adpos_all(self, start_date: str, end_date: str, page_size: int = 90) -> list[dict]:
    """分页拉全某区间的分广告位数据（自动翻页）。"""
    items: list[dict] = []
    page = 1
    while page <= 50:  # 防御性上限：90 天 * 十几个广告位远到不了
        data = await self.get_adpos_general(start_date, end_date, page=page, page_size=page_size)
        items.extend(data.get("list") or [])
        total = data.get("total_num", 0)
        if not data.get("list") or len(items) >= total:
            break
        page += 1
    return items


# 把实例方法挂到 WeChatClient（保持类定义风格统一，且能复用 _access_token / 代理）。
WeChatClient.get_adpos_general = get_adpos_general  # type: ignore[attr-defined]
WeChatClient.get_settlement = get_settlement  # type: ignore[attr-defined]
WeChatClient.fetch_adpos_all = fetch_adpos_all  # type: ignore[attr-defined]
WeChatClient._get_json = _get_json  # type: ignore[attr-defined]


# 需要人工确认才能执行的工具（供上层判断）
DANGEROUS_TOOLS = {"push_wechat_draft", "delete_wechat_draft", "render_video"}
