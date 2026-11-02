"""把 Markdown 渲染成「能直接粘进知乎编辑器」的干净语义 HTML。

为什么不用公众号那套内联样式
----------------------------
公众号排版（services/layout.py）产出的是一坨 `<section style="...">`，
因为微信的编辑器只认内联样式。知乎正好相反：它的编辑器是 ProseMirror，
粘贴 HTML 时会**按标签语义**（h2/h3/p/ul/blockquote）转成自己的内容块，
内联 style 会被剥掉或变成乱七八糟的 span。所以这里刻意只输出裸语义标签，
不带一个 style 属性。

为什么不用 LLM 做这步
----------------------
这是纯机械转换（Markdown → HTML），没有「创作」成分。用 LLM 做既慢又费钱，
还可能顺手改内容——用户要的是「我写的东西原样变成富文本」，不是再创作一遍。

为什么自己写而不引入 markdown 库
--------------------------------
只要覆盖标题/段落/列表/引用/代码/链接/图片这几种块，正则状态机 100 行够了。
为这点需求加一个依赖，换来的只是部署时多装一个包和一套扩展配置。

刻意不做的事
------------
- **不自动插入配图**：图片是本地路径，粘到知乎是裂图（知乎不会来抓我们的
  内网地址），插入反而制造误导。配图由用户在知乎编辑器里手动上传。
- **不做表格**：提示词里没要求模型写表格，真写了就当普通段落（信息不丢）。
- **不做 LaTeX**：知乎不支持，写了也白写。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 行内

_CODE_SLOT = "\x00"


def _escape(s: str) -> str:
    """转义 HTML 特殊字符。

    正文来自模型，也可能来自用户粘贴 —— 不做转义等于给内容页开一个 XSS 口子
    （队列页的排版预览会把这段 HTML 塞进 iframe）。
    """
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _inline(text: str) -> str:
    """行内标记：加粗 / 斜体 / 行内代码 / 链接 / 图片。"""
    s = _escape(text)

    # 行内代码先抽出来占位：不然 `**` 这类内容会被下面的加粗规则吃掉
    codes: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"{_CODE_SLOT}{len(codes) - 1}{_CODE_SLOT}"

    s = re.sub(r"`([^`]+)`", _stash, s)

    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    # 图片必须排在链接**之前**：`![图](url)` 里也含有 `[图](url)`，
    # 先跑链接规则的话，感叹号会被留在文本里、图片变成链接。
    s = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)\)",
        lambda m: f'<img src="{m.group(2)}" alt="{m.group(1)}" />',
        s,
    )
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        s,
    )
    # 还原行内代码
    s = re.sub(
        f"{_CODE_SLOT}(\\d+){_CODE_SLOT}",
        lambda m: f"<code>{_escape(codes[int(m.group(1))])}</code>",
        s,
    )
    return s


# ---------------------------------------------------------------- 块级

_H_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_UL_RE = re.compile(r"^[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\d+[.)]\s+(.*)$")


def render_zhihu(markdown: str) -> str:
    """Markdown → 知乎可粘贴的语义 HTML 片段。

    标题层级：Markdown 的 `#`/`##` 都降到 `h2`，`###` 及以下降到 `h3` ——
    知乎文章标题（H1）是编辑器里单独的标题框，正文用 H1 会跟它抢层级。
    """
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()

        # 围栏代码块
        if line.startswith("```"):
            flush_para()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 吃掉收尾的 ```
            out.append(f"<pre><code>{_escape(chr(10).join(buf))}</code></pre>")
            continue

        if not line:
            flush_para()
            i += 1
            continue

        if _HR_RE.match(line):
            flush_para()
            out.append("<hr />")
            i += 1
            continue

        m = _H_RE.match(line)
        if m:
            flush_para()
            tag = "h2" if len(m.group(1)) <= 2 else "h3"
            out.append(f"<{tag}>{_inline(m.group(2).strip())}</{tag}>")
            i += 1
            continue

        m = _QUOTE_RE.match(line)
        if m:
            flush_para()
            buf = [m.group(1)]
            i += 1
            while i < n:
                s2 = lines[i].strip()
                m2 = _QUOTE_RE.match(s2)
                if m2:
                    buf.append(m2.group(1))
                    i += 1
                elif s2:  # 懒延续：引用里换行不写 > 也算同一段
                    buf.append(s2)
                    i += 1
                else:
                    break
            out.append(f"<blockquote>{render_zhihu(chr(10).join(buf))}</blockquote>")
            continue

        is_ul = _UL_RE.match(line)
        is_ol = _OL_RE.match(line)
        if is_ul or is_ol:
            flush_para()
            tag = "ul" if is_ul else "ol"
            pat = _UL_RE if is_ul else _OL_RE
            items: list[str] = []
            while i < n:
                s2 = lines[i].strip()
                m2 = pat.match(s2)
                if m2:
                    items.append(m2.group(1))
                    i += 1
                elif s2 and items and not s2.startswith(("#", ">", "```")):
                    # 列表项的第二行（缩进续行），并到上一项里
                    items[-1] += " " + s2
                    i += 1
                else:
                    break
            body = "".join(f"<li>{_inline(t)}</li>" for t in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        para.append(line)
        i += 1

    flush_para()
    return "\n".join(out)


def plain_text(markdown: str) -> str:
    """去掉 Markdown 标记符的纯文本兜底（复制富文本不可用时的降级形态）。

    注意：小红书那份是让模型重写一遍（services/layout.py:_layout_plaintext），
    这里不能照抄 —— 知乎要的是「原样保结构」，让模型重写会把内容改写掉。
    所以只做机械剥离，不动一个字。
    """
    s = (markdown or "").replace("\r\n", "\n")
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "· ", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)\s]+\)", "", s)
    s = re.sub(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", s, flags=re.M)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def render_gzh_plain(markdown: str) -> str:
    """公众号「普通排版」：纯 Markdown → 最素 HTML，**不调 LLM**。

    场景：用户只要把 Markdown 段落干净地呈现出来，不要任何主题配色 / 装饰组件。
    纯机械转换（无需语义判断、不该再创作一次），故复用本文件的思路——
    正则状态机把 Markdown 转成带基础内联样式的 HTML，零 token、秒出。

    与 render_zhihu 的两点不同：
      1. 给每个中文文本节点包 `<span leaf="">`（公众号编辑器硬性要求，否则
         粘贴后样式大面积丢失，见 gzh_validate）；
      2. 块级标签带**中性**基础内联样式（只保证可读性，不引入任何主题配色）。

    配图不在此处处理：排版后处理 `_inject_images` 按 `<!--IMG:n-->` 占位注入；
    普通排版让布局在文末补占位（见 layout.py），与主题排版走同一套插入逻辑。
    """
    P = 'style="margin:1em 0;line-height:1.8;font-size:16px;letter-spacing:.5px;"'
    H2 = 'style="font-size:20px;font-weight:bold;margin:1.4em 0 .6em;line-height:1.4;"'
    H3 = 'style="font-size:17px;font-weight:bold;margin:1.2em 0 .5em;line-height:1.4;"'
    QUOTE = (
        'style="margin:1em 0;padding:.6em 1em;border-left:4px solid #e5e5e5;'
        'color:#888;background:#fafafa;"'
    )
    UL = 'style="padding-left:1.4em;margin:1em 0;line-height:1.8;"'
    LI = 'style="margin:.3em 0;"'
    HR = 'style="border:none;border-top:1px solid #e5e5e5;margin:1.4em 0;"'
    PRE = (
        'style="white-space:pre;background:#f6f6f6;padding:12px 14px;'
        'border-radius:6px;font-size:14px;line-height:1.6;overflow:auto;"'
    )

    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []

    def leaf(text: str) -> str:
        return f'<span leaf="">{text}</span>'

    def flush_para() -> None:
        if para:
            out.append(f"<p {P}>{leaf(_inline(' '.join(para)))}</p>")
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("```"):
            flush_para()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre {PRE}><code><span leaf=\"\">{_escape(chr(10).join(buf))}</span></code></pre>")
            continue
        if not line:
            flush_para()
            i += 1
            continue
        if _HR_RE.match(line):
            flush_para()
            out.append(f"<hr {HR} />")
            i += 1
            continue
        m = _H_RE.match(line)
        if m:
            flush_para()
            tag = "h2" if len(m.group(1)) <= 2 else "h3"
            st = H2 if tag == "h2" else H3
            out.append(f"<{tag} {st}>{leaf(_inline(m.group(2).strip()))}</{tag}>")
            i += 1
            continue
        m = _QUOTE_RE.match(line)
        if m:
            flush_para()
            buf = [m.group(1)]
            i += 1
            while i < n:
                s2 = lines[i].strip()
                m2 = _QUOTE_RE.match(s2)
                if m2:
                    buf.append(m2.group(1))
                    i += 1
                elif s2:
                    buf.append(s2)
                    i += 1
                else:
                    break
            # 引用里可能含二级结构（标题 / 列表），递归渲染保证每段都包 leaf
            out.append(f"<blockquote {QUOTE}>{render_gzh_plain(chr(10).join(buf))}</blockquote>")
            continue
        is_ul = _UL_RE.match(line)
        is_ol = _OL_RE.match(line)
        if is_ul or is_ol:
            flush_para()
            tag = "ul" if is_ul else "ol"
            pat = _UL_RE if is_ul else _OL_RE
            items: list[str] = []
            while i < n:
                s2 = lines[i].strip()
                m2 = pat.match(s2)
                if m2:
                    items.append(m2.group(1))
                    i += 1
                elif s2 and items and not s2.startswith(("#", ">", "```")):
                    items[-1] += " " + s2
                    i += 1
                else:
                    break
            body = "".join(f"<li {LI}>{leaf(_inline(t))}</li>" for t in items)
            out.append(f"<{tag} {UL}>{body}</{tag}>")
            continue
        para.append(line)
        i += 1

    flush_para()
    return "\n".join(out)
