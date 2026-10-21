"""正文图片转存回归测试（2026-10-21 线上 bug 修复）。

bug 背景：本地图片地址是 HMAC 签名直链（`?o={owner}&sig=...`），早期
transfer_inline_images 的正则写死 `src="([^"]*fname)"`（文件名紧贴结束
引号），签名上线后**全部静默失配**——正文图一张都没转存，微信抓不到图，
推送到草稿箱的文章只剩封面、正文全空。

这里锁死三类行为：
1. 带签名查询串（含 &amp; 转义）的 src 必须能整体替换成微信 url；
2. 同一张图多处引用只上传一次、全部替换；
3. 磁盘上没有 / html 里没引用的图不触发上传。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.services.wechat import WeChatClient, _replace_src_by_filename

CID = "23c14b52-dcb0-4e96-9672-ba92fa316823"
SIGNED = (
    f"/api/content/images/{CID}/abc123.png"
    "?o=619437c8504e43a4adbb0eb6811e48ba&amp;sig=7c7d80d0b7fa6b8876012473ed0f865f"
)
WX_URL = "https://mmbiz.qpic.cn/mmbiz_png/test/abc123?wx_fmt=png"


def test_old_regex_would_fail_on_signed_url():
    """留档：旧正则对签名直链恒失配——这个 bug 的根因，防止有人改回去。"""
    html = f'<img src="{SIGNED}" alt="x">'
    old = re.compile(r'src="([^"]*abc123\.png)"')
    assert old.search(html) is None, "旧正则不该匹配上签名直链"
    out, n = _replace_src_by_filename(html, "abc123.png", WX_URL)
    assert n == 1
    assert out == f'<img src="{WX_URL}" alt="x">'


def test_replace_signed_src_double_quotes():
    html = f'<p><img src="{SIGNED}" style="max-width:100%"></p>'
    out, n = _replace_src_by_filename(html, "abc123.png", WX_URL)
    assert n == 1
    assert f'src="{WX_URL}"' in out
    assert "sig=" not in out and "?o=" not in out
    # 外层属性（alt/style）不受影响
    assert 'style="max-width:100%"' in out


def test_replace_signed_src_single_quotes():
    html = f"<img src='{SIGNED}' alt='x'>"
    out, n = _replace_src_by_filename(html, "abc123.png", WX_URL)
    assert n == 1
    assert f"src='{WX_URL}'" in out
    assert "sig=" not in out


def test_multiple_occurrences_same_image():
    html = (
        f'<img src="{SIGNED}"><img src="{SIGNED}">'
        f"<img src='{SIGNED}'>"
    )
    out, n = _replace_src_by_filename(html, "abc123.png", WX_URL)
    assert n == 3
    assert out.count(WX_URL) == 3
    assert "sig=" not in out


def test_probe_only_mode():
    """new_url=None 只探测不改动。"""
    html = f'<img src="{SIGNED}">'
    out, n = _replace_src_by_filename(html, "abc123.png")
    assert (out, n) == (html, 1)
    out2, n2 = _replace_src_by_filename(html, "not-exist.png")
    assert (out2, n2) == (html, 0)


def test_absolute_urls_never_matched():
    """守卫：只替换站点相对路径（/ 开头）。

    微信 uploadimg 返回的 url 可能含原文件名（mock 就是这么构造的），
    不设守卫会把已转存的微信 url 再当本地引用重复上传。
    """
    wx = "https://mmbiz.qpic.cn/mmbiz_png/xyz/abc123.png?wx_fmt=png"
    html = f'<img src="{wx}"><img src="{SIGNED}">'
    out, n = _replace_src_by_filename(html, "abc123.png", "https://mmbiz.qpic.cn/new")
    assert n == 1  # 只有本地那处被换
    assert f'src="{wx}"' in out  # 微信 url 原样保留
    assert out.count("https://mmbiz.qpic.cn/new") == 1


def test_relative_guard_ignores_other_hosts():
    html = '<img src="https://example.com/a/abc123.png"><img src="http://x/abc123.png">'
    out, n = _replace_src_by_filename(html, "abc123.png", WX_URL)
    assert n == 0
    assert out == html


def test_other_images_in_same_html_untouched():
    html = (
        f'<img src="/api/content/images/{CID}/other.png?o=x&amp;sig=y">'
        f'<img src="{SIGNED}">'
    )
    out, n = _replace_src_by_filename(html, "abc123.png", WX_URL)
    assert n == 1
    assert 'src="/api/content/images/%s/other.png?o=x&amp;sig=y"' % CID in out


async def _run_transfer():
    """transfer_inline_images 端到端（mock 上传，真实文件系统）。"""
    import tempfile

    from app.core.config import settings

    tmp = Path(tempfile.mkdtemp())
    old_upload_dir = settings.UPLOAD_DIR
    settings.UPLOAD_DIR = str(tmp)
    try:
        cid = "cid-1111"
        gen = tmp / "gen" / cid
        gen.mkdir(parents=True)
        # 磁盘上放三张图：a/b 元数据有记录，c 只在正文里被引用（元数据漏记）
        for name in ("a.png", "b.png", "c.png"):
            (gen / name).write_bytes(b"fake-png")
        images = [
            {"path": f"gen/{cid}/a.png", "url": f"/api/content/images/{cid}/a.png?o=1&amp;sig=2"},
            {"path": f"gen/{cid}/b.png", "url": f"/api/content/images/{cid}/b.png?o=1&amp;sig=2"},
        ]
        html = (
            f'<img src="/api/content/images/{cid}/a.png?o=1&amp;sig=2">'
            f'<img src="/api/content/images/{cid}/a.png?o=1&amp;sig=2">'
            f'<img src="/api/content/images/{cid}/b.png?o=1&amp;sig=2">'
            # 元数据漏记、但磁盘上真实存在的图 —— 扫正文路径必须能转
            f'<img src="/api/content/images/{cid}/c.png?o=1&amp;sig=2">'
            # 正文引了、磁盘上没有的图（dangling）—— 转不了，原样保留
            f'<img src="/api/content/images/{cid}/gone.png?o=1&amp;sig=2">'
            # 外部图片不动
            '<img src="https://example.com/external.png">'
        )

        uploaded: list[str] = []

        async def fake_upload(file_path: str) -> str:
            uploaded.append(Path(file_path).name)
            return f"https://mmbiz.qpic.cn/{Path(file_path).name}"

        client = WeChatClient(appid="x", secret="y")
        client.upload_image = fake_upload  # type: ignore[assignment]

        out = await client.transfer_inline_images(html, images)

        # a/b/c 各上传一次（a 在正文出现两次只上传一次）；gone 不触发上传
        assert sorted(uploaded) == ["a.png", "b.png", "c.png"], uploaded
        # 四处本地引用全部换成微信 url
        assert out.count("https://mmbiz.qpic.cn/a.png") == 2
        assert out.count("https://mmbiz.qpic.cn/b.png") == 1
        assert out.count("https://mmbiz.qpic.cn/c.png") == 1
        # 签名串清零（只剩 gone.png 那一处悬挂引用保留原样）
        assert out.count("&amp;sig=") == 1
        assert f'src="/api/content/images/{cid}/gone.png?o=1&amp;sig=2"' in out
        assert 'src="https://example.com/external.png"' in out

        # images 为空时扫正文路径依然生效（元数据不是必需品）
        out2 = await client.transfer_inline_images(html, [])
        assert out2.count("https://mmbiz.qpic.cn/") == 4

        # 幂等：对已转存过的 html 再跑一次，不重复上传
        uploaded.clear()
        out3 = await client.transfer_inline_images(out, images)
        assert uploaded == [], f"不该重复上传: {uploaded}"
        assert out3 == out

        # html 为空 → 原样返回
        assert await client.transfer_inline_images("", images) == ""
    finally:
        settings.UPLOAD_DIR = old_upload_dir


def test_transfer_inline_images_end_to_end():
    asyncio.run(_run_transfer())
