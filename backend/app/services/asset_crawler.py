"""按文章内容采集配图（受控爬虫）。

一句话说清边界：这是**帮用户把自己有权使用的图片收进素材库**的工具，
不是「全网洗图」。所以护栏不是补充说明，是主体逻辑：

1. **域名白名单**：只抓 `ASSET_CRAWL_ALLOW_DOMAINS` 里的站点。留空 = 不采，
   不像在线图源那样给个默认源就能跑 —— 采集对象必须由用户显式指定。
2. **尊重 robots.txt**：默认开。站点说不给你抓就不抓，用户可在配置里关掉
   （关掉的场景应是「这是我自己/已获授权的站」，不是「绕过别人」）。
3. **限速 + 上限**：单次最多 N 个页面、每页最多 M 张图、总字节封顶，
   每页之间留间隔。不做定时任务、不做批量囤图。
4. **留痕**：每张候选都带来源页 URL、页面标题、抓取时间、页面域名。
   入库时 `license` 固定 `unknown`、`source` 固定 `crawl`，前端打「授权待确认」。
5. **不做去水印**：权属水印删除是《著作权法》第 51 条红线（见 docs/素材库模块
   调研与实现方案.md 第 5 节）。这里只存原图，加工能力走独立的合规闸门。

流程分两步，和现有「在线搜图」一致：
    preview(关键词/文章 → 找页面 → 抽图片 URL → 下载到内存缓存)  ← 不落库
    import(勾选的候选 id → 落库)                                  ← 才真正入库

预览阶段就下载，是为了让前端能显示真实缩略图（多数站点防盗链，直接把外链
塞进 <img> 会裂图），同时导入时不必二次请求源站。缓存是进程内 TTL，重启即丢。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import time
import urllib.parse
import urllib.robotparser
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import logger

# ---------------------------------------------------------------- 常量

_UA = "content-workbench-asset-crawler/1.0 (+material library; contact: owner)"

# 常见无信息量的图片：logo / 占位 / 追踪像素 / 图标。按文件名与尺寸一起判。
_SKIP_NAME = re.compile(
    r"(logo|icon|avatar|qrcode|qr_code|weixin|wechat|badge|spinner|loading|"
    r"placeholder|blank|pixel|banner_ad|ad_|advert|emoji|sprite|favicon|"
    r"share|arrow|btn_|button|border|bg_)",
    re.I,
)
# 图片最小边长（像素）：小于这个基本是图标/装饰，不是配图
_MIN_EDGE = 320
# 候选缓存：id -> CrawlCandidate。上限 200 条，TTL 30 分钟。
_CACHE: OrderedDict[str, "CrawlCandidate"] = OrderedDict()
_CACHE_TTL = 30 * 60
_CACHE_MAX = 200

# robots.txt 缓存：origin -> (expire_at, parser or None)。解析失败按「允许」处理，
# 但会把失败记进日志 —— 拿不到 robots 不等于站点同意，只是不阻塞用户。
_ROBOTS: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
_ROBOTS_TTL = 3600


# ---------------------------------------------------------------- 数据结构


@dataclass
class CrawlCandidate:
    """一条预览候选。bytes 只在内存里，不落盘、不入库。"""

    id: str
    data: bytes
    mime: str
    width: int
    height: int
    source_url: str          # 图片本身的 URL
    page_url: str            # 图片所在页面（留痕用）
    page_title: str
    domain: str
    alt: str
    fetched_at: int
    owner: str
    filename: str = ""

    def to_dict(self, thumb_prefix: str = "") -> dict[str, Any]:
        return {
            "id": self.id,
            "thumb_url": f"{thumb_prefix}/{self.id}" if thumb_prefix else "",
            "source_url": self.source_url,
            "page_url": self.page_url,
            "page_title": self.page_title,
            "domain": self.domain,
            "alt": self.alt,
            "width": self.width,
            "height": self.height,
            "size_bytes": len(self.data),
            "mime": self.mime,
            "fetched_at": self.fetched_at,
            "license": "unknown",
        }


@dataclass
class CrawlResult:
    items: list[CrawlCandidate] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    pages_visited: int = 0

    def to_dict(self, thumb_prefix: str = "") -> dict[str, Any]:
        return {
            "ok": True,
            "items": [c.to_dict(thumb_prefix) for c in self.items],
            "errors": self.errors,
            "keywords": self.keywords,
            "pages_visited": self.pages_visited,
        }


# ---------------------------------------------------------------- 配置与护栏


def allow_domains() -> list[str]:
    """配置的白名单域名，去空白、去协议、转小写。"""
    raw = getattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "") or ""
    out: list[str] = []
    for part in raw.split(","):
        p = part.strip().lower()
        if not p:
            continue
        p = re.sub(r"^https?://", "", p).split("/")[0]
        if p:
            out.append(p)
    return out


def is_allowed_host(host: str) -> bool:
    """精确匹配或子域匹配。防 `evil-example.com` 绕过 `example.com`。"""
    h = (host or "").lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in allow_domains())


def _assert_public_url(url: str, *, must_be_whitelisted: bool) -> urllib.parse.ParseResult:
    """SSRF 闸门 + 白名单。复用 asset_sources 的思路，但多一层域名校验。

    图片 CDN 常常不在文章域名下（如 xxx.com 的图放在 cdn.xxx.com 或第三方图床），
    所以对**图片**只要求 https + 非内网；对**文章页**才强制白名单。
    """
    p = urllib.parse.urlparse(url)
    if p.scheme != "https":
        raise ValueError("只允许 https 地址")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("地址缺少主机名")
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("拒绝内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # 域名，继续
    else:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("拒绝内网地址")
    if must_be_whitelisted and not is_allowed_host(host):
        raise ValueError(f"域名 {host} 不在采集白名单内")
    return p


async def _robots_allows(url: str) -> bool:
    """robots.txt 检查。默认遵守；配置关闭时直接放行。"""
    if not getattr(settings, "ASSET_CRAWL_RESPECT_ROBOTS", True):
        return True
    p = urllib.parse.urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    now = time.time()
    hit = _ROBOTS.get(origin)
    if hit and hit[0] > now:
        parser = hit[1]
        return parser.can_fetch(_UA, url) if parser else True
    parser: urllib.robotparser.RobotFileParser | None = None
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as cli:
            resp = await cli.get(origin + "/robots.txt", headers={"User-Agent": _UA})
        if resp.status_code == 200 and len(resp.text) < 200_000:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(resp.text.splitlines())
    except Exception as exc:  # noqa: BLE001
        logger.warning("robots.txt 读取失败 %s: %s", origin, exc)
        parser = None
    _ROBOTS[origin] = (now + _ROBOTS_TTL, parser)
    return parser.can_fetch(_UA, url) if parser else True


# ---------------------------------------------------------------- 关键词提取

# 中文停用词（够用即可，不引第三方分词库）。英文按空格切。
_STOP = set(
    "的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 "
    "看 好 自己 这 那 与 及 或 为 以 从 把 被 让 给 但 而 且 更 最 只 又 再 已 "
    "可以 这个 那个 什么 怎么 如何 为什么 我们 你们 他们 它 其 之 于 中 下 里 后 "
    "时 年 月 日 些 个 们 并 等 如 若 则 因 由 对 向 把 比 还 就 都 也 很 太 "
    "首先 其次 最后 总之 综上 然而 因此 所以 而且 并且 以及 通过 进行 实现 具有 "
    "关于 对于 由于 目前 现在 今天 一个 一种 一些 这样 那样 之后 之前 同时 实际 "
    "the a an of to in on for and or is are was were be been with as at by from "
    "this that it its we you they our your their will can should how what why"
)
_ZH = re.compile(r"[\u4e00-\u9fff]+")
_EN = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{2,}")
# 纯虚词单字。含这些字的 2~4 字滑窗，绝大多数是跨词边界的碎片
# （「作工具的」「具的真实」），不是能拿去检索的独立词。
_ZH_FUNC = set(
    "的了和与及或在把被让给但而且更最只又再已其之于些个们并等若则因由向比"
    "还就也很太这那为以从着"
)
# 按虚词切短语：中文标题里「的 / 与 / 和」天然就是语义边界，切出来的
# 「人工智能写作工具」「真实体验」比任何 n-gram 都更像人写的检索词。
_ZH_SPLIT = re.compile(
    "[的了和与及或在把被让给但而且更最只又再已其之于些个们并等若则因由向比"
    "还就也很太这那为以从着]+"
)


def extract_keywords(title: str, body: str, limit: int = 6) -> list[str]:
    """从文章标题 + 正文提取检索关键词。

    不引 jieba：多一个依赖没必要，采集关键词只需要「能搜到相关页」的粒度，
    不需要语言学意义上的分词。三层：
      1. 标题/正文里的中文串按虚词切成短语，给最高权重（标题 ×3、正文 ×1）；
      2. 中文串再取 2~4 字滑窗作补充，但丢掉含虚词的跨词碎片；
      3. 英文词直接收。
    最后**双向去重**：短词被长词包住则跳过；反过来长词包住已留的短词时，
    用长词替换短词。只做单向会留下「人工智能」和「人工智」并存的脏结果。
    """
    freq: dict[str, int] = {}
    phrases: dict[str, int] = {}  # 短语单独记分，不与滑窗混算

    def _feed(text: str, weight: int) -> None:
        for seg in _EN.findall(text):
            w = seg.lower()
            if w not in _STOP and len(w) > 2:
                freq[w] = freq.get(w, 0) + weight * 2
        for chunk in _ZH.findall(text):
            for part in _ZH_SPLIT.split(chunk):
                if (
                    2 <= len(part) <= 10
                    and len(set(part)) > 1      # 丢「试试」这类叠词
                    and part not in _STOP
                ):
                    phrases[part] = max(phrases.get(part, 0), weight)
            for n in (2, 3, 4):
                for i in range(len(chunk) - n + 1):
                    w = chunk[i : i + n]
                    if w in _STOP or len(set(w)) == 1:
                        continue
                    if any(ch in _ZH_FUNC for ch in w):
                        continue
                    freq[w] = freq.get(w, 0) + weight

    _feed(title or "", 3)
    # 正文只取前 3000 字：后面多半是结语/免责，权重低还拖慢
    _feed(re.sub(r"[#*`>\[\]()（）]", " ", (body or "")[:3000]), 1)

    # 短语是主关键词：给一个远高于滑窗的基准分。分开算是因为短短语会同时
    # 被 2~4 gram 命中而「加分反超」长短语（「真实体验」曾压过
    # 「人工智能写作工具」），短语之间再按长度优先，长的更像完整检索词。
    for k, w in phrases.items():
        freq[k] = 10_000 + w * 20 + len(k)

    # 长词优先（同分时更长的排前面），再双向去重
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    out: list[str] = []
    for w, _score in ranked:
        if any(w in kept and w != kept for kept in out):
            continue  # 新词是已留长词的一部分
        # 新词更长、包住了已留的短词 → 用长词顶掉短词
        out = [kept for kept in out if kept not in w]
        out.append(w)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 页面发现


def build_search_urls(keywords: list[str]) -> list[str]:
    """由关键词构造「相关文章」搜索页。

    支持配置 `ASSET_CRAWL_SEARCH_TEMPLATE`（含 `{q}` 占位），例如站内搜索：
        https://example.com/search?q={q}
    未配置时用 Bing 网页搜索，结果里再按白名单过滤链接。
    """
    q = urllib.parse.quote(" ".join(keywords[:4]))
    tpl = (getattr(settings, "ASSET_CRAWL_SEARCH_TEMPLATE", "") or "").strip()
    if tpl:
        if "{q}" not in tpl:
            return []
        return [tpl.replace("{q}", q)]
    return [f"https://www.bing.com/search?q={q}&count=30&setlang=zh-hans"]


_RE_LINK = re.compile(r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I)
_RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_RE_IMG = re.compile(r"<img\b([^>]*)>", re.I | re.S)
_RE_ATTR = re.compile(
    r"\b(src|data-src|data-original|data-lazy-src|data-echo|data-url|srcset)\s*=\s*"
    r"[\"']([^\"']+)[\"']",
    re.I,
)
_RE_OG = re.compile(
    r"<meta[^>]+(?:property|name)\s*=\s*[\"']og:image[\"'][^>]+content\s*=\s*[\"']([^\"']+)[\"']",
    re.I,
)
_RE_SCRIPT_TYPE = re.compile(r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)


def page_title(doc: str) -> str:
    m = _RE_TITLE.search(doc)
    if not m:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()[:200]


def extract_article_links(doc: str, base: str, *, whitelist_only: bool = True) -> list[str]:
    """从搜索结果页里找文章链接。Bing 的跳转链接会被还原成真实 URL。"""
    out: list[str] = []
    for raw in _RE_LINK.findall(doc):
        href = html.unescape(raw)
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        # Bing 用 https://www.bing.com/ck/a?...&u=a1<base64> 包真实地址
        if "bing.com/ck/a" in href:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
            if q.startswith("a1"):
                import base64

                try:
                    href = base64.urlsafe_b64decode(q[2:] + "===").decode("utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    continue
        url = urllib.parse.urljoin(base, href)
        try:
            p = _assert_public_url(url, must_be_whitelisted=whitelist_only)
        except ValueError:
            continue
        # 搜索结果页自身、分页、站内首页不带文章路径，跳过
        if not p.path.strip("/") or p.path.strip("/") in ("search", "s"):
            continue
        if url not in out:
            out.append(url)
    return out


def extract_image_urls(doc: str, page_url: str) -> list[tuple[str, str]]:
    """从文章 HTML 抽取图片地址，返回 [(url, alt)]。

    覆盖三种常见形态：<img> 的正/懒加载属性、srcset 里最大的一张、og:image。
    过滤 data: URI、明显图标名、以及 JSON-LD 里的图片数组。
    """
    out: list[tuple[str, str]] = []

    def _add(raw: str, alt: str = "") -> None:
        raw = html.unescape((raw or "").strip())
        if not raw or raw.startswith(("data:", "blob:", "javascript:")):
            return
        # srcset 取最后一条（通常是最大尺寸）
        if "," in raw and " " in raw:
            parts = [s.strip().split(" ")[0] for s in raw.split(",") if s.strip()]
            raw = parts[-1] if parts else ""
        if not raw:
            return
        url = urllib.parse.urljoin(page_url, raw)
        tail = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].lower()
        if _SKIP_NAME.search(tail):
            return
        try:
            _assert_public_url(url, must_be_whitelisted=False)
        except ValueError:
            return
        if (url, alt) not in out:
            out.append((url, alt))

    for attrs in _RE_IMG.findall(doc):
        alt_m = re.search(r"\balt\s*=\s*[\"']([^\"']*)[\"']", attrs, re.I)
        alt = html.unescape(alt_m.group(1))[:200] if alt_m else ""
        for _k, val in _RE_ATTR.findall(attrs):
            _add(val, alt)
    for m in _RE_OG.findall(doc):
        _add(m)
    # JSON-LD 里常见 "image": "https://..." 或数组
    for block in _RE_SCRIPT_TYPE.findall(doc):
        for m in re.findall(r"[\"'](?:image|url)[\"']\s*:\s*[\"'](https?://[^\"']+)[\"']", block):
            _add(m)
    return out


# ---------------------------------------------------------------- 缓存


def cache_put(c: CrawlCandidate) -> None:
    _CACHE[c.id] = c
    _CACHE.move_to_end(c.id)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def cache_get(cid: str, owner: str) -> CrawlCandidate | None:
    c = _CACHE.get(cid)
    if not c or c.owner != owner:
        return None
    if c.fetched_at + _CACHE_TTL < time.time():
        _CACHE.pop(cid, None)
        return None
    _CACHE.move_to_end(cid)
    return c


# 注意：不提供「只按 id 取字节」的无鉴权入口。缩略图端点统一走
# `cache_get(cid, owner)`，owner 来自 HMAC 签名，跨用户猜不到别人的候选图。


# ---------------------------------------------------------------- 采集主流程


def _probe(data: bytes) -> tuple[int, int, str]:
    """复用 assets 服务的图片探测，避免两处尺寸逻辑漂移。"""
    from app.services.assets import _probe_image

    w, h = _probe_image(data)
    ext = "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        ext = "gif"
    return w, h, ext


async def _fetch_html(url: str, *, use_browser: bool) -> str:
    if use_browser:
        return await _fetch_html_browser(url)
    async with httpx.AsyncClient(
        timeout=getattr(settings, "ASSET_CRAWL_TIMEOUT", 15),
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    ) as cli:
        resp = await cli.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            raise ValueError("目标不是网页")
        return resp.text


async def _fetch_html_browser(url: str) -> str:
    """可选：Playwright 渲染。依赖按需装，不进主 requirements（见文件尾注）。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "启用浏览器渲染需先安装：pip install playwright && playwright install chromium"
        ) from exc
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page(user_agent=_UA, locale="zh-CN")
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # 触发懒加载：滚到底再回来，多数图片此时才写进 src
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
            return await page.content()
        finally:
            await browser.close()


async def _download_image(url: str, page_url: str) -> bytes:
    """下载图片：带来源页 Referer（很多站点防盗链要它），限体积。

    重定向**手动跟随**，不全开 `follow_redirects`：图床 / CDN 几乎都会 302
    一次，一刀不跟会丢掉绝大多数图片；但自动跟随又会让 `_assert_public_url`
    只检查第一跳，302 到内网就成了 SSRF。所以每一跳都重新过一遍公网闸门。

    content-type 只挡明显的网页，不强制 `image/*`：不少 CDN 返回
    `application/octet-stream` 但内容确实是图。真正的硬校验交给 PIL
    （`_probe_image`），它打不开就当失败，比信响应头可靠。
    """
    _assert_public_url(url, must_be_whitelisted=False)
    limit = max(1, settings.ASSET_MAX_MB) * 1024 * 1024
    headers = {"User-Agent": _UA, "Referer": page_url}
    async with httpx.AsyncClient(
        timeout=getattr(settings, "ASSET_CRAWL_TIMEOUT", 15),
        follow_redirects=False,
        headers=headers,
    ) as cli:
        cur = url
        for _hop in range(4):
            async with cli.stream("GET", cur) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location", "").strip()
                    if not loc:
                        raise RuntimeError("重定向缺少 Location")
                    cur = urllib.parse.urljoin(cur, loc)
                    _assert_public_url(cur, must_be_whitelisted=False)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype.startswith("text/html") or ctype.startswith("application/json"):
                    raise ValueError("返回的是网页，不是图片")
                buf = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > limit:
                        raise ValueError("图片超过体积上限")
                return bytes(buf)
    raise RuntimeError("重定向次数过多")


async def crawl_preview(
    owner: str,
    *,
    keywords: list[str] | None = None,
    title: str = "",
    body: str = "",
    page_urls: list[str] | None = None,
    max_pages: int | None = None,
    max_images: int | None = None,
) -> CrawlResult:
    """采集预览：**不落库**。返回候选（已在内存缓存里，导入时直接取字节）。

    `page_urls` 由用户显式给出时优先（最可控）；否则按 title/body/keywords
    生成关键词 → 搜相关文章页 → 过滤白名单域名。
    """
    result = CrawlResult()
    domains = allow_domains()
    explicit = [u for u in (page_urls or []) if u and u.strip()]
    if not domains and not explicit:
        result.errors.append(
            {
                "source": "crawl",
                "reason": "自动按文章找配图需要采集域名白名单（ASSET_CRAWL_ALLOW_DOMAINS）"
                "——否则等于拿搜索引擎当全网采集入口。请先在后端 .env 填写你有权采集的"
                "站点域名（逗号分隔），或直接粘贴文章 URL 采集。",
            }
        )
        return result

    kws = [k.strip() for k in (keywords or []) if k and k.strip()]
    if not kws and (title or body):
        kws = extract_keywords(title, body)
    result.keywords = kws

    # 请求方给的上限只能往小压：负数会让 pages[:负数] 静默变成空列表，
    # 超大值会绕过护栏。两头都夹住，服务端配置是硬天花板。
    cap_pages = min(max(1, int(max_pages or getattr(settings, "ASSET_CRAWL_MAX_PAGES", 5))), 10)
    cap_images = min(max(1, int(max_images or getattr(settings, "ASSET_CRAWL_MAX_IMAGES", 20))), 40)
    use_browser = bool(getattr(settings, "ASSET_CRAWL_USE_BROWSER", False))
    delay = float(getattr(settings, "ASSET_CRAWL_DELAY_SEC", 1.0))

    # ---- 1. 确定要访问的页面
    pages: list[str] = []
    if explicit:
        for u in explicit[:cap_pages]:
            try:
                _assert_public_url(u, must_be_whitelisted=bool(domains))
                pages.append(u)
            except ValueError as exc:
                result.errors.append({"source": "crawl", "reason": f"跳过页面：{exc}"})
    elif kws:
        for su in build_search_urls(kws):
            try:
                doc = await _fetch_html(su, use_browser=False)
                links = extract_article_links(doc, su, whitelist_only=True)
                for link in links:
                    if link not in pages:
                        pages.append(link)
                    if len(pages) >= cap_pages:
                        break
            except Exception as exc:  # noqa: BLE001
                result.errors.append({"source": "search", "reason": f"搜索页读取失败：{exc}"})
            if len(pages) >= cap_pages:
                break
        if not pages:
            result.errors.append(
                {
                    "source": "search",
                    "reason": "没找到白名单站点内的相关文章页。可直接粘贴文章 URL 采集，"
                    "或检查关键词/白名单配置。",
                }
            )
    else:
        result.errors.append({"source": "crawl", "reason": "没有关键词，也没有指定文章 URL"})

    if not pages:
        return result

    # ---- 2. 逐页抽图、下载
    seen_hash: set[str] = set()
    for idx, pu in enumerate(pages[:cap_pages]):
        if len(result.items) >= cap_images:
            break
        if idx and delay > 0:
            await asyncio.sleep(delay)
        try:
            if not await _robots_allows(pu):
                result.errors.append(
                    {"source": "robots", "reason": f"robots.txt 不允许抓取：{pu}"}
                )
                continue
            doc = await _fetch_html(pu, use_browser=use_browser)
        except Exception as exc:  # noqa: BLE001
            result.errors.append({"source": "page", "reason": f"页面打开失败：{exc}"})
            continue
        result.pages_visited += 1
        ptitle = page_title(doc)
        domain = urllib.parse.urlparse(pu).hostname or ""
        per_page = 0
        for img_url, alt in extract_image_urls(doc, pu):
            if len(result.items) >= cap_images or per_page >= 12:
                break
            try:
                data = await _download_image(img_url, pu)
                w, h, _ext = _probe(data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("跳过图片 %s: %s", img_url, exc)
                continue
            if w < _MIN_EDGE or h < _MIN_EDGE:
                continue  # 图标/装饰
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hash:
                continue
            seen_hash.add(digest)
            cand = CrawlCandidate(
                id=uuid.uuid4().hex,
                data=data,
                mime=f"image/{_ext}" if _ext != "jpg" else "image/jpeg",
                width=w,
                height=h,
                source_url=img_url,
                page_url=pu,
                page_title=ptitle,
                domain=domain,
                alt=alt,
                fetched_at=int(time.time()),
                owner=owner,
                filename=urllib.parse.urlparse(img_url).path.rsplit("/", 1)[-1],
            )
            cache_put(cand)
            result.items.append(cand)
            per_page += 1
    return result
