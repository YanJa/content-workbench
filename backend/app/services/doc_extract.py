"""上传文档 → 纯文本抽取（知识库「文档上传」来源）。

为什么单独成模块：录入链路的文本来源从「手动粘贴 / 复用成稿」扩到「文件」，
抽取是文件特有的脏活（编码、各格式解析、缺依赖降级），不该和写入逻辑混在一起。

设计取舍：
  - 不依赖重型 OCR：扫描版 PDF（图片型）抽不出字是预期内的，给友好提示，
    而不是假装成功存了个空素材。
  - 缺某个解析库时整体不崩：每个解析器 import 包在 try 里，失败了返回
    明确的中文错误，让用户知道「装了 X 才能解析 Y」，而不是 500。
  - 文本编码容错：txt/md 这类纯文本不知道编码，依次试 utf-8 → gbk → latin-1，
    总有一种能读出来（latin-1 永远成功，只是可能乱码，但至少不崩）。
  - 大文件在路由层就拦（见 KB_UPLOAD_MAX_BYTES），这里只负责「读出来」。

返回约定：`extract_text(path, filename) -> (text, error)`：
  - 成功：(纯文本, None)；text 可能为空串（文件里确实没字），由调用方决定怎么提示；
  - 失败：( "", "可读的中文错误" )。
"""
from __future__ import annotations

import json
import os

# 上传大小上限：20MB。纯文本/Markdown 远到不了，主要是给 PDF / Word 兜底，
# 太大的文件抽出来也没法当素材用（上限 50 万字），还拖慢抽取。
KB_UPLOAD_MAX_BYTES = 20 * 1024 * 1024

# 支持的扩展名 → 解析器名。不在表里的直接拒绝，提示用户格式不支持。
TEXT_EXTS = {"txt", "text", "md", "markdown"}
DATA_EXTS = {"json"}
ALLOWED_EXTS = TEXT_EXTS | DATA_EXTS | {"pdf", "doc", "docx"}


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _read_text_tolerant(path: str) -> str:
    """纯文本：依次试常见编码，latin-1 兜底（永不抛异常，可能乱码）。"""
    raw = _read_bytes(path)
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 理论到不了（latin-1 总能解码），但保险起见
    return raw.decode("latin-1", errors="replace")


def _extract_pdf(path: str) -> str:
    # 优先 pdfplumber（基于 pdfminer.six，API 干净），失败回退 pdfminer 高层接口。
    try:
        import pdfplumber

        parts: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        text = "\n\n".join(p for p in parts if p.strip())
        if text.strip():
            return text
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - 换 pdfminer 再试一次
        last = exc
    else:
        last = None

    try:
        from pdfminer.high_level import extract_text as pdfminer_extract

        text = pdfminer_extract(path) or ""
        if text.strip():
            return text
    except ImportError:
        if last:
            raise last
        raise
    return ""


def _extract_docx(path: str) -> str:
    try:
        import docx

        document = docx.Document(path)
        parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
        return "\n".join(parts)
    except ImportError as exc:
        raise RuntimeError("未安装 python-docx，无法解析 .docx") from exc


def _extract_doc(path: str) -> str:
    try:
        import docx2txt

        text = docx2txt.process(path) or ""
        return text
    except ImportError as exc:
        raise RuntimeError("未安装 docx2txt，无法解析 .doc（旧版 Word）") from exc


def _extract_json(path: str) -> str:
    """JSON：读成结构化对象再美化输出，比直接塞原始串更适合做素材。

    数组/对象都行；超大 JSON 由下游 create_item 的 50 万字上限兜住。
    """
    raw = _read_text_tolerant(path).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON 解析失败：{exc}") from exc
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001 - 极少触发，退化为原样
        return raw


def extract_text(path: str, filename: str = "") -> tuple[str, str | None]:
    """从上传文件里抽纯文本。

    入参 path 是落盘后的绝对/相对路径；filename 用于取扩展名（path 本身可能是
    哈希名，所以以 filename 为准）。返回 (text, error)。
    """
    ext = os.path.splitext(filename or path)[1].lower().lstrip(".")

    if ext not in ALLOWED_EXTS:
        if not ext:
            return "", f"无法识别文件类型（缺少扩展名）。支持：{', '.join(sorted(ALLOWED_EXTS))}"
        return "", f"不支持的文件类型 .{ext}。支持：{', '.join(sorted(ALLOWED_EXTS))}"

    try:
        if ext in TEXT_EXTS:
            return _read_text_tolerant(path), None
        if ext in DATA_EXTS:
            return _extract_json(path), None
        if ext == "pdf":
            text = _extract_pdf(path)
            if not text.strip():
                return "", "未能从 PDF 提取出文字（可能是扫描版/图片型 PDF，需先转成可复制文字）"
            return text, None
        if ext == "docx":
            text = _extract_docx(path)
            if not text.strip():
                return "", "未能从 Word 文档提取出文字（文档可能为空）"
            return text, None
        if ext == "doc":
            text = _extract_doc(path)
            if not text.strip():
                return "", "未能从 Word 文档提取出文字（文档可能为空）"
            return text, None
    except RuntimeError as exc:
        # 明确的中文可操作错误（缺依赖 / 格式坏）
        return "", str(exc)
    except Exception as exc:  # noqa: BLE001 - 解析器抛的其它错，转成友好提示
        return "", f"解析文件失败：{exc}"

    return "", "未知错误"
