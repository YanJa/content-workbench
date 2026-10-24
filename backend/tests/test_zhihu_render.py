"""知乎渲染：Markdown → 可粘贴的干净语义 HTML。

三条不能退化的性质：
  1. **语义化**：出 h2/h3/p/ul/blockquote，不出内联 style（知乎编辑器剥 style，
     带 style 的公众号 HTML 粘过去会变成一坨 span）。
  2. **不改写内容**：机械转换，一个字都不许改（改内容那是创作，不是渲染）。
  3. **转义**：正文来自模型/用户，不转义等于给排版预览开 XSS 口子。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.db.session import Base
from app.models.content import Content
from app.services.layout import layout_content
from app.services.zhihu_render import plain_text, render_zhihu


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 块级

def test_headings_downgrade():
    """# / ## → h2，### → h3 —— 知乎的 H1 是文章标题框，正文不能抢。"""
    html = render_zhihu("# 大标题\n\n## 第二节\n\n### 第三级")
    assert "<h2>大标题</h2>" in html
    assert "<h2>第二节</h2>" in html
    assert "<h3>第三级</h3>" in html
    assert "<h1" not in html


def test_paragraphs_and_lists():
    html = render_zhihu("第一段\n\n- 要点一\n- 要点二\n\n1. 第一步\n2. 第二步")
    assert "<p>第一段</p>" in html
    assert "<ul><li>要点一</li><li>要点二</li></ul>" in html
    assert "<ol><li>第一步</li><li>第二步</li></ol>" in html


def test_blockquote_and_code():
    html = render_zhihu("> 引用一句\n\n```\nprint(1)\n```")
    assert "<blockquote><p>引用一句</p></blockquote>" in html
    assert "<pre><code>print(1)</code></pre>" in html


def test_inline_marks():
    html = render_zhihu("这里有 **加粗** 和 *斜体* 和 `code`")
    assert "<strong>加粗</strong>" in html
    assert "<em>斜体</em>" in html
    assert "<code>code</code>" in html


def test_link_and_image():
    html = render_zhihu("见 [知乎](https://www.zhihu.com) 与 ![图](https://x/a.png)")
    assert '<a href="https://www.zhihu.com">知乎</a>' in html
    assert '<img src="https://x/a.png" alt="图" />' in html


def test_no_inline_style_anywhere():
    """一个 style 属性都不能有 —— 这是知乎版与公众号版的分界线。"""
    html = render_zhihu("# 标题\n\n正文 **粗**\n\n- 列表\n\n> 引用")
    assert "style=" not in html
    assert "<span" not in html
    assert "<section" not in html


def test_escapes_html_in_body():
    """正文里的 <script> 必须变成文本，不能被当标签执行。"""
    html = render_zhihu("正常文字 <script>alert(1)</script> 结束")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_input():
    assert render_zhihu("") == ""
    assert render_zhihu("   \n\n  ") == ""


def test_content_not_rewritten():
    """渲染不许改内容：字数、用词都得原样。"""
    src = "第一句原话，,\n\n第二句原话。"
    text = plain_text(src)
    assert "第一句原话" in text and "第二句原话" in text


def test_plain_text_strips_marks():
    t = plain_text("# 标题\n\n- 要点\n\n**粗体** [链接](https://a)")
    assert "#" not in t
    assert "**" not in t
    assert "要点" in t and "粗体" in t and "链接" in t


# ---------------------------------------------------------------- 排版入口

def _mk():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(Maker, *, sub_type="answer", body="# 结论\n\n正文"):
    async with Maker() as db:
        db.add(
            Content(
                id="c1", platform="zhihu", sub_type=sub_type, kind="text",
                title="t", body=body, owner="u1",
            )
        )
        await db.commit()


def test_layout_zhihu_writes_html_and_ready():
    engine, Maker = _mk()

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(Maker)
        async with Maker() as db:
            c = await db.get(Content, "c1")
            res = await layout_content(db, c, theme_id="moyu-green", platform="zhihu")
            await db.commit()
            return res

    res = _run(go())
    assert res["ok"] is True
    assert res["theme_name"] == "知乎富文本"
    assert "<h2>结论</h2>" in res["html"]

    async def check():
        async with Maker() as db:
            c = await db.get(Content, "c1")
            return c.layout_status, c.layouts or {}, c.body_html

    status, layouts, body_html = _run(check())
    assert status == "ready"
    assert layouts["zhihu"]["html"].startswith("<h2>")
    # body_html 必须同步：加工面板的预览/复制读的是它
    assert body_html == layouts["zhihu"]["html"]
    asyncio.run(engine.dispose())


def test_layout_zhihu_warns_when_images_exist():
    """有配图时给出提示：本地链接知乎抓不到，得手动上传。"""
    engine, Maker = _mk()

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as db:
            db.add(
                Content(
                    id="c2", platform="zhihu", sub_type="answer", kind="text",
                    title="t", body="正文", owner="u1",
                    images=[{"id": "i1", "url": "/files/a.png"}],
                )
            )
            await db.commit()
        async with Maker() as db:
            c = await db.get(Content, "c2")
            res = await layout_content(db, c, theme_id="moyu-green", platform="zhihu")
            await db.commit()
            return res

    res = _run(go())
    assert res["ok"] is True
    assert any("手动上传" in w for w in res["warnings"])
    asyncio.run(engine.dispose())


def test_layout_zhihu_empty_body_fails_cleanly():
    engine, Maker = _mk()

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(Maker, body="   ")
        async with Maker() as db:
            c = await db.get(Content, "c1")
            return await layout_content(db, c, theme_id="moyu-green", platform="zhihu")

    res = _run(go())
    assert res["ok"] is False
    assert "正文为空" in res["error"]
    asyncio.run(engine.dispose())
