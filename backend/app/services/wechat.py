"""微信公众号 API 客户端（草稿箱推送）。

合规说明（重要）：
    微信《公众平台运营规范》明确禁止「通过脚本、程序托管等自动化方式
    替代真人完成内容创作、发布」。因此本客户端**只做草稿箱写入**，
    不做自动群发。

    草稿箱接口（draft/add、draft/update，支持 news 图文与 newspic 图片消息）
    是官方接口，合规可用；发布接口（freepublish）自动发布本身违规。

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
    """构造 httpx.AsyncClient 的公共参数（恒直连）。

    微信接口出网 IP 会被公众号后台「IP 白名单」校验，直接把服务器出网 IP
    加进白名单即可（已在生产验证）。早期为绕过「动态出网 IP 加不完白名单」
    曾支持 `WECHAT_PROXY` 固定出口代理（SSH 隧道），现已移除——服务器 IP
    已固定并加白，微信请求一律直连，无代理、无隧道依赖。
    """
    return {"timeout": timeout}


# 网络瞬断（长连接/大文件上传偶发「Server disconnected」）时重试即可成功，
# 不影响幂等性（拉 token 幂等；上传素材会产生孤儿素材但无害）。
# 值 = 总尝试次数（含首次）。
_RETRY = 2


async def probe_egress_ip(timeout: int = 10) -> dict:
    """探测微信请求实际会用的出口 IP（用于系统状态页 / 排查白名单）。

    恒直连：用公共回显服务查本机出网 IP，即微信看到的来源 IP。
    """
    kw = _client_kwargs(timeout)
    last_err = ""
    for i in range(3):  # 公共回显服务偶发抖动，多试几次
        try:
            async with httpx.AsyncClient(**kw) as client:
                resp = await client.get("https://ifconfig.me/ip")
                ip = resp.text.strip()
            if ip:
                return {"ok": True, "ip": ip}
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            await asyncio.sleep(0.5)
    return {"ok": False, "error": last_err}


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

    # 兼容单/双引号；属性值内不会出现裸引号，[^"'] 不会跨属性边界。
    # 既匹配本地签名直链（/api/content/images/...），也匹配 TOS 公网直链
    # （https://<bucket>.tos-.../gen/...），后者在 STORAGE_BACKEND=s3 时出现。
    pat = re.compile(
        r"src=([\"'])(?:/api/content/images/|https?://[^\"']*?/gen/)[^\"']*"
        + re.escape(fname)
        + r"[^\"']*\1"
    )
    matches = list(pat.finditer(html))
    if new_url is None or not matches:
        return html, len(matches)
    out = html
    # 从后往前替换，避免偏移失效；同一张图在正文出现多次只算一次上传
    for m in reversed(matches):
        q = m.group(1)
        out = out[: m.start()] + f"src={q}{new_url}{q}" + out[m.end() :]
    return out, len(matches)


# ---------------------------------------------------------------- 贴图（newspic）文案 / 载荷
# 微信 draft/add、draft/update 的 article_type 分两类（2026-11 官方文档）：
#   news    —— 图文消息：content 是 HTML 片段，thumb_media_id 必填（封面）
#   newspic —— 图片消息（贴图）：content 只能是**纯文本**（不支持 HTML/外链图），
#              image_info.image_list[].image_media_id 必填（永久素材 media_id），
#              最多 20 张，首图即封面，不传 thumb_media_id
_NEWPIC_MAX_IMAGES = 20
_NEWPIC_TITLE_MAX = 32


def _image_mime(name: str) -> str:
    """按扩展名给图片 MIME：微信 add_material / uploadimg 会看声明的图片类型。

    早先恒传 image/png，jpg/webp 文件字节虽对但声明类型不符，微信偶发
    40005「invalid file type」。按真实扩展名给，少踩一个坑。
    """
    import mimetypes

    return mimetypes.guess_type(name)[0] or "image/png"


def clean_newspic_text(text: str) -> str:
    """贴图正文：剥掉残留 Markdown 标记，保留换行与 emoji。

    排版 prompt 已要求「不输出 Markdown 语法符号」，但用户会手改、旧数据也可能
    带进 `# ` / `**` / 列表符号。newspic 的 content 是纯文本，微信不渲染这些
    符号，留着就是一屏幕星号，所以在这里清一遍。
    """
    import re

    if not text:
        return ""
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    # 行首列表 / 引用符号去掉，内容保留
    out = re.sub(r"^[ \t]*[-*+>]+[ \t]+", "", out, flags=re.M)
    # 强调与行内代码：**xx** / __xx__ / `xx` → xx
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"__(.+?)__", r"\1", out)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    # 行首标题井号（历史数据里的「# 首图标题」写法）
    out = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", out, flags=re.M)
    # [文字](url) → 文字：newspic 正文不支持外链，留 url 没意义
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)
    # 3 行以上连续空行收成 1 个空行，别把正文撑成一屏空白
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def split_newspic_text(text: str, fallback_title: str = "") -> tuple[str, str]:
    """把贴图文案拆成 (首图标题, 正文纯文本)。

    排版产物 layouts.gzh.text 的**第一行就是首图标题**（_layout_gzh_tietu 的
    prompt 明确要求不输出 Markdown 符号），其余是正文。兼容旧的 `# 标题` 写法
    （提示词里的输出格式说明曾带 #，历史数据可能还有）。
    """
    cleaned = clean_newspic_text(text or "")
    lines = cleaned.split("\n")
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), -1)
    if idx < 0:
        return (fallback_title or "").strip()[:_NEWPIC_TITLE_MAX], ""
    title = lines[idx].strip()[:_NEWPIC_TITLE_MAX]
    body_lines = [ln.rstrip() for ln in lines[idx + 1:]]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    return title, "\n".join(body_lines).strip()


def build_article(
    *,
    article_type: str,
    title: str,
    content: str,
    digest: str = "",
    author: str = "",
    need_open_comment: bool = True,
    thumb_media_id: str = "",
    image_media_ids: list[str] | None = None,
) -> dict[str, Any]:
    """构造 draft/add、draft/update 共用的单篇 article 对象。

    两类文章共用 title/author/digest/content 等字段，差异只在：
      news    → thumb_media_id（封面）
      newspic → article_type + image_info.image_list（贴图）
    抽出来是为了 add / update 不会一个支持贴图、另一个忘了同步。
    """
    article: dict[str, Any] = {
        "title": title,
        "content": content,
        "need_open_comment": 1 if need_open_comment else 0,
        "only_fans_can_comment": 0,
    }
    if article_type == "newspic":
        ids = [str(m).strip() for m in (image_media_ids or []) if str(m).strip()]
        article["article_type"] = "newspic"
        article["author"] = author or ""
        article["image_info"] = {
            "image_list": [{"image_media_id": mid} for mid in ids[:_NEWPIC_MAX_IMAGES]]
        }
        # 官方 add/update 的 newspic 示例只有上面这些字段：digest（摘要）、
        # content_source_url（阅读原文）、thumb_media_id（封面）都是 news 专属。
        # 图片消息首图即封面，多传 thumb_media_id 可能被判参数冲突，索性不带。
    else:
        article.update(
            {
                "author": author or "",
                "digest": digest,
                "content_source_url": "",
                "thumb_media_id": thumb_media_id,
            }
        )
    return article


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
        total = _RETRY
        data = None
        for i in range(total):
            try:
                async with httpx.AsyncClient(**_client_kwargs(60)) as client:
                    files = {"media": (name, data_bytes, _image_mime(name))}
                    resp = await client.post(url, files=files)
                    data = resp.json()
                break
            except httpx.TransportError as exc:
                if i < total - 1:
                    logger.warning("图片上传网络瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
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
        total = _RETRY
        data = None
        for i in range(total):
            try:
                async with httpx.AsyncClient(**_client_kwargs(60)) as client:
                    files = {"media": (name, data_bytes, _image_mime(name))}
                    resp = await client.post(url, files=files)
                    data = resp.json()
                break
            except httpx.TransportError as exc:
                if i < total - 1:
                    logger.warning("封面上传网络瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
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

        # 1) 扫正文：/api/content/images/{cid}/{fname}（本地直链）或
        #    https://<bucket>.tos-.../gen/{cid}/{fname}（TOS 公网直链，s3 后端）。
        #    两种都提取出 (cid, fname)，字节仍从本地盘读（云端后端也保留了本地落盘）。
        scan = re.compile(
            r"src=[\"'](?:/api/content/images/|https?://[^\"']*?/gen/)"
            r"([^/\"'?]+)/([^\"'?]+)[^\"']*[\"']"
        )
        seen: dict[str, str] = {}  # fname -> 磁盘绝对路径
        for m in scan.finditer(out):
            cid, fname = m.group(1), m.group(2)
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
        need_open_comment: bool = True,
        article_type: str = "news",
        image_media_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建草稿（draft/add），支持图文 news 与贴图 newspic 两类。

        html / content：
          - news    → 公众号兼容的内联样式 HTML 片段（gzh-design 产物）
          - newspic → **纯文本**（贴图正文不支持 HTML / 外链图）
        thumb_media_id：news 的封面永久素材 id（来自 upload_thumb）。
          **图文消息必须有封面**：实测空串 / 省略都返回 40007「invalid media_id」
          （2026-10-21 探测确认），所以显式拦截并给可操作提示，而不是把
          微信的 40007 原样抛给用户。
        image_media_ids：newspic 的图片永久素材 id 列表（≤20，首图即封面），
          由上层把 content.images 逐张走 material/add_material 上传得到。

        need_open_comment：是否打开留言。微信默认 0（关闭），但内容号几乎
        都需要留言来承接互动，所以本平台**默认打开**（传 True → 1）。
        """
        if not self.configured:
            return {"ok": False, "error": "未配置 WECHAT_APPID / WECHAT_SECRET"}
        if article_type == "newspic":
            if not [m for m in (image_media_ids or []) if str(m).strip()]:
                return {
                    "ok": False,
                    "error": "贴图至少需要 1 张图片（图片消息没有封面字段，首图即封面）",
                    "hint": "先在队列里点「加工排版」并在「配图」里添加图片后再推送",
                }
        elif not thumb_media_id:
            return {
                "ok": False,
                "error": "缺少封面图：微信要求每篇草稿必须有封面",
                "hint": "先在队列里点「加工排版」生成配图（首图会自动用作封面）后再推送",
            }

        try:
            token = await self._access_token()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        # add 的 articles 是**数组**（官方文档标 objarray），别和 update 的 object 搞混
        payload = {
            "articles": [
                build_article(
                    article_type=article_type,
                    title=title,
                    content=html,
                    digest=digest,
                    author=author,
                    need_open_comment=need_open_comment,
                    thumb_media_id=thumb_media_id,
                    image_media_ids=image_media_ids,
                )
            ]
        }
        url = f"{WECHAT_API}/cgi-bin/draft/add?access_token={token}"

        # 微信要求 body 用 UTF-8 编码，且不转义中文
        import json as _json

        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        total = _RETRY
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
                        logger.warning("推送草稿网络瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
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

    async def update_draft(
        self,
        media_id: str,
        title: str,
        html: str,
        digest: str = "",
        thumb_media_id: str = "",
        author: str = "",
        need_open_comment: bool = True,
        article_type: str = "news",
        image_media_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """原地更新草稿箱里已有的一篇（cgi-bin/draft/update）。

        用于「同一任务多次推送」：复用上一次推送得到的 media_id 替换内容，
        而不是再 add 一条，避免草稿箱堆积重复草稿。

        **articles 是 object 不是数组**：官方文档 draft_update 的 Body.articles
        标的是 Object Payload（示例 `"articles": { ... }`），和 draft/add 的
        objarray 不同。旧实现沿用了数组形态，这里一并按官方纠正；news /
        newspic 两种 article_type 都受支持。

        index=0 表示更新该草稿内的第 1 篇图文（本平台一篇草稿就是一篇图文）。
        若 media_id 已失效（被群发 / 后台删除 / 过期清理）→ 微信返回 40007，
        上层应 fallback 到 add_draft 新建一篇。
        """
        if not self.configured:
            return {"ok": False, "error": "未配置 WECHAT_APPID / WECHAT_SECRET"}
        if not media_id:
            return {"ok": False, "error": "缺少 media_id，无法原地更新"}
        if article_type == "newspic":
            if not [m for m in (image_media_ids or []) if str(m).strip()]:
                return {
                    "ok": False,
                    "error": "贴图至少需要 1 张图片（图片消息没有封面字段，首图即封面）",
                    "hint": "先在队列里点「加工排版」并在「配图」里添加图片后再推送",
                }
        elif not thumb_media_id:
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
            "media_id": media_id,
            "index": 0,
            "articles": build_article(
                article_type=article_type,
                title=title,
                content=html,
                digest=digest,
                author=author,
                need_open_comment=need_open_comment,
                thumb_media_id=thumb_media_id,
                image_media_ids=image_media_ids,
            ),
        }
        url = f"{WECHAT_API}/cgi-bin/draft/update?access_token={token}"

        # 微信要求 body 用 UTF-8 编码，且不转义中文
        import json as _json

        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        total = _RETRY
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
                        logger.warning("更新草稿网络瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
                        await asyncio.sleep(0.8 * (i + 1))
                    else:
                        raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("更新草稿箱失败")
            return {"ok": False, "error": str(exc)}

        if not data or data.get("errcode", 0) != 0:
            errcode = data.get("errcode")
            errmsg = data.get("errmsg", "未知错误")
            hint = ""
            if errcode == 40007:
                hint = "media_id 无效或草稿已不存在（可能已被群发 / 删除）"
            return {
                "ok": False,
                "error": errmsg,
                "errcode": errcode,
                "hint": hint or "常见原因：IP 不在白名单 / 凭证错误 / 接口权限未开通",
            }

        logger.info("更新草稿成功: media_id=%s", media_id)
        return {
            "ok": True,
            "media_id": media_id,
            "message": "已更新草稿箱里的这篇草稿（原地替换，不新增记录）",
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
        total = _RETRY
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
                        logger.warning("删除草稿网络瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
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
    """GET 并解析 JSON，网络瞬断时重试。"""
    total = _RETRY
    last_exc: Exception | None = None
    for i in range(total):
        try:
            async with httpx.AsyncClient(**_client_kwargs(20)) as client:
                resp = await client.get(url, params=params)
                return resp.json()
        except httpx.TransportError as exc:  # 网络瞬断，重试即可
            last_exc = exc
            if i < total - 1:
                logger.warning("微信 GET 网络瞬断，重试 %d/%d：%s", i + 1, total - 1, exc)
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
