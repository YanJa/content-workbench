"""受控采集（asset_crawler）+ 采集导入接口的单元测试。

覆盖的是**护栏**，不是「能不能抓到图」——抓图效果取决于目标站点 HTML，
mock 出来没有意义；而护栏一旦破了就是 SSRF / 越权 / 无限囤图，必须钉死：

  - 白名单解析与子域绕过（`evil-example.com` 不能蹭 `example.com`）；
  - 公网/HTTPS/内网地址闸门；
  - Bing 跳转链接还原后仍要过白名单；
  - 预览不落库、缓存按 owner 隔离、TTL 过期；
  - 自动找图必须有白名单，显式 URL 模式不需要；
  - 请求方给的上限不能为负、不能超服务端硬顶；
  - 图片下载跟随重定向，但每一跳都重新过 SSRF 闸门；
  - 导入必须勾 rights_confirmed、created 计数真实、与在线导入共用额度。

不真发网络请求，也不碰开发库。
"""
from __future__ import annotations

import asyncio
import base64
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.routers.assets import CrawlImportReq, crawl_config, crawl_import
from app.services import asset_crawler as ac
from app.services import assets as svc


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例都从干净的白名单/缓存/robots 缓存开始，避免互相串味。"""
    monkeypatch.setattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "")
    monkeypatch.setattr(settings, "ASSET_CRAWL_SEARCH_TEMPLATE", "")
    monkeypatch.setattr(settings, "ASSET_CRAWL_RESPECT_ROBOTS", True)
    monkeypatch.setattr(settings, "ASSET_CRAWL_USE_BROWSER", False)
    monkeypatch.setattr(settings, "ASSET_CRAWL_DELAY_SEC", 0)
    monkeypatch.setattr(settings, "ASSET_CRAWL_MAX_PAGES", 5)
    monkeypatch.setattr(settings, "ASSET_CRAWL_MAX_IMAGES", 20)
    ac._CACHE.clear()
    ac._ROBOTS.clear()


# ---------------------------------------------------------------- 关键词提取
def test_extract_keywords_prefers_title_and_drops_stopwords():
    """标题词权重更高；停用词和纯语气词不该进检索词。"""
    kws = ac.extract_keywords(
        "人工智能写作工具的真实体验",
        "这个工具其实非常好用，我也觉得可以试试。人工智能写作工具的真实体验。",
        limit=4,
    )
    assert kws
    assert len(kws) <= 4
    assert any("人工智能" in k or "写作" in k for k in kws)
    assert all(k not in ac._STOP for k in kws)
    # 长词优先：短词不该和大词并存（「人工智」让位给「人工智能」）
    assert not any(a in b and a != b for a in kws for b in kws)


def test_extract_keywords_handles_english_and_empty():
    kws = ac.extract_keywords("ChatGPT writing workflow", "", limit=5)
    assert "chatgpt" in kws or "writing" in kws
    assert ac.extract_keywords("", "", limit=3) == []


# ---------------------------------------------------------------- 白名单 / SSRF
def test_allow_domains_parsing(monkeypatch):
    monkeypatch.setattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "https://Example.com/, sub.FOO.cn")
    assert ac.allow_domains() == ["example.com", "sub.foo.cn"]
    assert ac.is_allowed_host("example.com")
    assert ac.is_allowed_host("img.example.com")       # 子域放行
    assert ac.is_allowed_host("sub.foo.cn")
    assert not ac.is_allowed_host("evil-example.com")  # 后缀蹭域名必须拒绝
    assert not ac.is_allowed_host("example.com.evil.com")


def test_assert_public_url_blocks_ssrf(monkeypatch):
    monkeypatch.setattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "example.com")
    for bad in (
        "http://example.com/a.jpg",           # 只允许 https
        "https://127.0.0.1/a.jpg",
        "https://192.168.1.7/a.jpg",
        "https://10.0.0.1/a.jpg",
        "https://localhost/a.jpg",
        "https://foo.internal/a.jpg",
        "https://other.com/a.jpg",            # 不在白名单
    ):
        with pytest.raises(ValueError):
            ac._assert_public_url(bad, must_be_whitelisted=True)
    # 图片 CDN 只要求公网 https，不要求文章域名白名单
    p = ac._assert_public_url("https://cdn.thirdparty.net/a.jpg", must_be_whitelisted=False)
    assert p.hostname == "cdn.thirdparty.net"


# ---------------------------------------------------------------- 页面发现
def test_extract_article_links_unwraps_bing_and_filters(monkeypatch):
    monkeypatch.setattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "example.com")
    target = "https://example.com/news/2024/post-1"
    enc = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    doc = f"""
    <a href="https://www.bing.com/ck/a?u=a1{enc}&amp;ntb=1">第一篇</a>
    <a href="https://evil.com/post/2">站外</a>
    <a href="https://example.com/">首页</a>
    <a href="https://example.com/search?q=x">搜索页</a>
    <a href="#anchor">锚点</a>
    <a href="javascript:void(0)">脚本</a>
    """
    links = ac.extract_article_links(doc, "https://www.bing.com/search?q=x", whitelist_only=True)
    assert links == [target]


def test_build_search_urls_respects_template(monkeypatch):
    monkeypatch.setattr(settings, "ASSET_CRAWL_SEARCH_TEMPLATE", "https://example.com/s?q={q}")
    urls = ac.build_search_urls(["人工智能", "写作"])
    assert len(urls) == 1
    assert "q=%E4%BA%BA" in urls[0]
    monkeypatch.setattr(settings, "ASSET_CRAWL_SEARCH_TEMPLATE", "https://example.com/s?x=1")
    assert ac.build_search_urls(["x"]) == []  # 模板没 {q} 就不猜


# ---------------------------------------------------------------- 图片抽取
def test_extract_image_urls_covers_lazy_srcset_og_and_jsonld():
    page = "https://example.com/post/1"
    doc = """
    <html><head>
      <meta property="og:image" content="/og.jpg">
      <script type="application/ld+json">{"image":"https://cdn.example.net/ld.jpg"}</script>
    </head><body>
      <img src="/a.jpg" alt="配图一">
      <img data-src="https://cdn.example.net/b.png" alt="懒加载">
      <img srcset="/small.jpg 320w, /big.jpg 1200w">
      <img src="data:image/png;base64,iVBORw0KGgo=">
      <img src="/logo.png">
      <img src="http://example.com/insecure.jpg">
      <img src="/site-icon.svg">
    </body></html>
    """
    pairs = ac.extract_image_urls(doc, page)
    urls = [u for u, _ in pairs]
    assert "https://example.com/a.jpg" in urls
    assert "https://cdn.example.net/b.png" in urls
    assert "https://example.com/big.jpg" in urls          # srcset 取最大那张
    assert "https://example.com/og.jpg" in urls
    assert "https://cdn.example.net/ld.jpg" in urls
    assert not any("base64" in u for u in urls)
    assert not any("logo" in u for u in urls)
    assert not any(u.startswith("http://") for u in urls)
    assert dict(pairs)["https://example.com/a.jpg"] == "配图一"


# ---------------------------------------------------------------- 缓存
def _cand(owner="u1", cid="c1", age=0):
    return ac.CrawlCandidate(
        id=cid,
        data=b"fake-image-bytes",
        mime="image/jpeg",
        width=800,
        height=600,
        source_url="https://cdn.example.net/a.jpg",
        page_url="https://example.com/post/1",
        page_title="一篇测试文章",
        domain="example.com",
        alt="配图",
        fetched_at=int(time.time()) - age,
        owner=owner,
        filename="a.jpg",
    )


def test_cache_is_owner_scoped_and_expires():
    ac.cache_put(_cand("u1", "c1"))
    assert ac.cache_get("c1", "u1") is not None
    assert ac.cache_get("c1", "u2") is None   # 别人的候选图不能被猜到
    ac.cache_put(_cand("u1", "c2", age=ac._CACHE_TTL + 10))
    assert ac.cache_get("c2", "u1") is None
    assert "c2" not in ac._CACHE


def test_cache_evicts_oldest_over_cap():
    for i in range(ac._CACHE_MAX + 5):
        ac.cache_put(_cand("u1", f"x{i}"))
    assert len(ac._CACHE) == ac._CACHE_MAX
    assert ac.cache_get("x0", "u1") is None


# ---------------------------------------------------------------- 采集主流程护栏
def test_preview_blocks_auto_search_without_whitelist():
    """没白名单又没显式 URL：必须拒绝，且一个网络请求都不能发。"""
    with patch.object(ac, "_fetch_html", new=AsyncMock()) as fetch:
        r = _run(ac.crawl_preview("u1", keywords=["人工智能"]))
    assert r.items == []
    assert r.errors and "白名单" in r.errors[0]["reason"]
    fetch.assert_not_called()


def _mock_page_pipeline():
    """给「显式 URL 模式」铺一条假的采集管线。"""
    html_doc = '<html><title>测试文章</title><img src="/a.jpg" alt="配图"></html>'
    c = _cand("u1", "cc1")
    return (
        patch.object(ac, "_fetch_html", new=AsyncMock(return_value=html_doc)),
        patch.object(ac, "_robots_allows", new=AsyncMock(return_value=True)),
        patch.object(ac, "_download_image", new=AsyncMock(return_value=c.data)),
        patch.object(ac, "_probe", return_value=(800, 600, "jpg")),
    )


def test_preview_explicit_url_works_without_whitelist_and_clamps_limits():
    """显式 URL 模式不依赖白名单；负数上限不能让结果静默变空。"""
    p1, p2, p3, p4 = _mock_page_pipeline()
    with p1, p2, p3, p4:
        r = _run(ac.crawl_preview(
            "u1",
            page_urls=["https://example.com/post/1"],
            max_pages=-5,
            max_images=-3,
        ))
    assert len(r.items) == 1
    assert r.items[0].owner == "u1"
    assert r.items[0].page_url == "https://example.com/post/1"
    assert r.pages_visited == 1
    assert not r.errors


def test_preview_rejects_non_whitelisted_explicit_url(monkeypatch):
    """配了白名单后，显式 URL 也必须落在白名单内。"""
    monkeypatch.setattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "good.com")
    with patch.object(ac, "_fetch_html", new=AsyncMock()) as fetch:
        r = _run(ac.crawl_preview("u1", page_urls=["https://evil.com/post/1"]))
    assert r.items == []
    assert any("不在采集白名单" in e["reason"] for e in r.errors)
    fetch.assert_not_called()


def test_preview_respects_robots():
    p1, p2, p3, p4 = _mock_page_pipeline()
    p2 = patch.object(ac, "_robots_allows", new=AsyncMock(return_value=False))
    with p1, p2, p3, p4:
        r = _run(ac.crawl_preview("u1", page_urls=["https://example.com/post/1"]))
    assert r.items == []
    assert r.pages_visited == 0
    assert any("robots" in e["reason"] for e in r.errors)


# ---------------------------------------------------------------- 重定向 SSRF
class _FakeStream:
    def __init__(self, status: int, headers: dict, chunks: list[bytes] | None = None):
        self.status_code = status
        self.headers = headers
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def aiter_bytes(self, _n):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


def _fake_client(streams):
    inst = MagicMock()
    inst.__aenter__ = AsyncMock(return_value=inst)
    inst.__aexit__ = AsyncMock(return_value=False)
    inst.stream = MagicMock(side_effect=streams)
    return inst


def test_download_image_follows_redirect():
    first = _FakeStream(302, {"location": "https://cdn.example.net/real.jpg"})
    second = _FakeStream(200, {"content-type": "application/octet-stream"}, [b"JPEGDATA"])
    inst = _fake_client([first, second])
    with patch.object(ac.httpx, "AsyncClient", return_value=inst):
        data = _run(ac._download_image("https://example.com/a.jpg", "https://example.com/post/1"))
    assert data == b"JPEGDATA"
    assert inst.stream.call_count == 2
    assert inst.stream.call_args_list[1].args[1] == "https://cdn.example.net/real.jpg"


def test_download_image_blocks_redirect_to_internal():
    """302 到内网时必须当场拒绝，不能因为第一跳是公网就放行。"""
    first = _FakeStream(302, {"location": "http://127.0.0.1/admin"})
    inst = _fake_client([first, _FakeStream(200, {"content-type": "image/jpeg"}, [b"x"])])
    with patch.object(ac.httpx, "AsyncClient", return_value=inst):
        with pytest.raises(ValueError):
            _run(ac._download_image("https://example.com/a.jpg", "https://example.com/post/1"))
    assert inst.stream.call_count == 1


# ---------------------------------------------------------------- 配置端点
def test_crawl_config_distinguishes_auto_search(monkeypatch):
    monkeypatch.setattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "")
    cfg = _run(crawl_config())
    assert cfg["enabled"] is True          # 显式 URL 模式仍可用
    assert cfg["auto_search"] is False
    assert "白名单" in cfg["hint"]
    monkeypatch.setattr(settings, "ASSET_CRAWL_ALLOW_DOMAINS", "example.com")
    cfg2 = _run(crawl_config())
    assert cfg2["auto_search"] is True
    assert cfg2["domains"] == ["example.com"]


# ---------------------------------------------------------------- 导入闸门
def test_import_requires_rights_confirmation():
    with pytest.raises(HTTPException) as ei:
        _run(crawl_import(CrawlImportReq(ids=["c1"], rights_confirmed=False), db=MagicMock(), owner="u1"))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei2:
        _run(crawl_import(CrawlImportReq(ids=[], rights_confirmed=True), db=MagicMock(), owner="u1"))
    assert ei2.value.status_code == 400


def test_import_counts_created_vs_duplicates_and_keeps_source(monkeypatch):
    monkeypatch.setattr(settings, "ASSET_IMPORT_PER_HOUR", 60)
    cands = [_cand("u1", "c1"), _cand("u1", "c2")]
    fake_asset = MagicMock()
    save = AsyncMock(side_effect=[(fake_asset, True), (fake_asset, False)])
    with (
        patch("app.routers.assets.asset_crawler.cache_get", side_effect=cands),
        patch("app.routers.assets.svc.count_recent_imports", new=AsyncMock(return_value=0)),
        patch("app.routers.assets.svc.save_bytes", new=save),
        patch("app.routers.assets.svc.asset_dict", return_value={"id": "a1"}),
    ):
        r = _run(crawl_import(
            CrawlImportReq(ids=["c1", "c2"], rights_confirmed=True),
            db=MagicMock(),
            owner="u1",
        ))
    assert r["imported_count"] == 2
    assert r["created"] == 1        # 只有一条是真新建，另一条 hash 去重命中
    assert r["duplicates"] == 1
    assert r["failed"] == []
    kw = save.call_args_list[0].kwargs
    assert kw["source"] == "crawl"
    assert kw["license"] == "unknown"
    assert "图片地址：https://cdn.example.net/a.jpg" in kw["attribution_text"]
    assert kw["source_url"] == "https://example.com/post/1"


def test_import_stops_at_hourly_quota(monkeypatch):
    monkeypatch.setattr(settings, "ASSET_IMPORT_PER_HOUR", 1)
    with (
        patch("app.routers.assets.asset_crawler.cache_get", return_value=_cand("u1", "c1")),
        patch("app.routers.assets.svc.count_recent_imports", new=AsyncMock(return_value=1)),
        patch("app.routers.assets.svc.save_bytes", new=AsyncMock()) as save,
    ):
        r = _run(crawl_import(
            CrawlImportReq(ids=["c1"], rights_confirmed=True), db=MagicMock(), owner="u1"
        ))
    assert r["imported"] == []
    assert "每小时导入上限" in r["failed"][0]["reason"]
    save.assert_not_called()


def test_count_recent_imports_covers_both_external_sources():
    """在线图源和受控采集必须共用同一个额度，否则等于给采集开了后门。"""
    captured = {}

    async def _exec(stmt):
        captured["stmt"] = stmt
        m = MagicMock()
        m.scalar.return_value = 3
        return m

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_exec)
    assert _run(svc.count_recent_imports(db, "u1")) == 3
    compiled = str(
        captured["stmt"].whereclause.compile(compile_kwargs={"literal_binds": True})
    )
    assert "search" in compiled and "crawl" in compiled
