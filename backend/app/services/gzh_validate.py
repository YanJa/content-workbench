"""公众号 HTML 合规校验（移植自 gzh-design 的 validate_gzh_html.py）。

把「平台限制」从模型自觉变成**确定性兜底**：排版产出后必跑一遍，
有 ERROR 就回喂模型重修。逻辑与技能里的脚本保持等价，
区别只是这里做成可 import 的函数（脚本是 CLI）。

⚠️ 若 gzh-design 技能的校验规则更新，这里的正则表要同步（见文末日期）。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# (正则, 说明) —— 命中即 ERROR：会被公众号编辑器过滤或导致粘贴后样式失效
FORBIDDEN = [
    (re.compile(r"<style[\s>]", re.I), "<style> 标签会被过滤，样式必须内联"),
    (re.compile(r"<script[\s>]", re.I), "<script> 标签会被过滤"),
    (re.compile(r"</?div[\s>]", re.I), "<div> 会被改写，请用 <section>"),
    (re.compile(r"<link[\s>]", re.I), "外部 <link>（CSS/字体）会被过滤"),
    (re.compile(r"\sclass\s*=", re.I), "class 属性会被剥离，请用内联 style"),
    (re.compile(r"\sid\s*=", re.I), "id 属性会被剥离"),
    (re.compile(r"position\s*:\s*(fixed|absolute|sticky)", re.I),
     "position fixed/absolute/sticky 不被支持"),
    (re.compile(r"float\s*:", re.I), "float 不被支持"),
    (re.compile(r"@media", re.I), "@media 媒体查询不被支持"),
    (re.compile(r"@keyframes", re.I), "@keyframes 动画不被支持"),
    (re.compile(r"@import", re.I), "@import 不被支持"),
    (re.compile(r"display\s*:\s*grid", re.I), "display:grid 不被支持，请用 flex"),
    (re.compile(r"var\s*\(\s*--", re.I), "CSS 变量 var(--x) 不被支持，请写死值"),
    (re.compile(r"url\s*\(\s*['\"]?https?://[^)]*\.(woff2?|ttf|otf|eot)", re.I),
     "外部字体不被支持"),
]

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
SKIP_TAGS = {"head", "title", "style", "script"}
# 中文字后紧跟半角逗号/分号/叹号/问号（应改全角）；只查"中文在前"避免误伤中英混排
HALF_PUNCT = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf][,;!?]")
ASCII_QUOTE = re.compile(r"[\"']")
# 代码区特征：等宽字体或 white-space:pre —— 其内半角符号是正常的
CODE_STYLE = re.compile(r"monospace|white-space\s*:\s*pre|courier|consolas|sf mono", re.I)


class _LeafChecker(HTMLParser):
    """检查每个中文文本节点是否处于 <span leaf> 内。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.leaf_depth = 0
        self.code_depth = 0
        self.span_leaf_count = 0
        self.unwrapped: list[tuple[str, str]] = []
        self.half_punct: list[str] = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        is_leaf = tag == "span" and "leaf" in ad
        is_code = bool(CODE_STYLE.search(ad.get("style", "") or ""))
        if is_leaf:
            self.span_leaf_count += 1
            self.leaf_depth += 1
        if is_code:
            self.code_depth += 1
        self.stack.append((tag, is_leaf, is_code))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _, was_leaf, was_code in self.stack[i:]:
                    if was_leaf:
                        self.leaf_depth -= 1
                    if was_code:
                        self.code_depth -= 1
                del self.stack[i:]
                break

    def handle_data(self, data):
        text = data.strip()
        if not text or not CJK.search(text):
            return
        if any(t in SKIP_TAGS for t, _, _ in self.stack):
            return
        if self.leaf_depth == 0:
            parent = self.stack[-1][0] if self.stack else "(root)"
            snippet = text[:24] + ("…" if len(text) > 24 else "")
            self.unwrapped.append((snippet, parent))
        if self.code_depth == 0 and (HALF_PUNCT.search(text) or ASCII_QUOTE.search(text)):
            snippet = text[:24] + ("…" if len(text) > 24 else "")
            self.half_punct.append(snippet)


def validate_gzh_html(html: str) -> tuple[list[str], list[str], int]:
    """校验公众号 HTML。返回 (errors, warnings, span_leaf_count)。

    errors 非空 = 必须修复（否则粘贴到公众号后失效）。
    """
    errors: list[str] = []
    warnings: list[str] = []

    for rx, msg in FORBIDDEN:
        hits = len(rx.findall(html))
        if hits:
            errors.append(f"{msg}（命中 {hits} 处）")

    checker = _LeafChecker()
    try:
        checker.feed(html)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"HTML 解析中断: {exc}")

    has_cjk = bool(CJK.search(html))
    if has_cjk and checker.span_leaf_count == 0:
        errors.append("全文没有任何 <span leaf=\"\"> 包裹——粘贴到公众号后样式会大面积丢失")
    elif checker.unwrapped:
        sample = "；".join(f"「{s}」(在 <{p}> 内)" for s, p in checker.unwrapped[:5])
        warnings.append(
            f"{len(checker.unwrapped)} 处中文文本未被 <span leaf> 包裹，样式可能丢失。例：{sample}"
        )

    if checker.half_punct:
        sample = "；".join(f"「{s}」" for s in checker.half_punct[:5])
        warnings.append(
            f"{len(checker.half_punct)} 处正文疑似半角标点/英文引号，应改中文全角（代码块内不计）。例：{sample}"
        )

    return errors, warnings, checker.span_leaf_count
