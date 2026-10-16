"""PPT 转口播稿解析。

策略：每页优先取**演讲者备注** —— 备注本来就是"这一页要讲的话"，
没有备注时才拼接页面上的可见文字（标题 + 正文）。

限制
----
python-pptx 只支持 .pptx。老 .ppt（OLE2 复合文档）格式读不了，
解析失败时明确提示用户另存为 .pptx，而不是抛一个看不懂的异常。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path


def render_slide_images(content: bytes, pages: int) -> list[str]:
    """用 LibreOffice 把 pptx 的每一页渲染成 PNG（真实幻灯片画面）。

    返回相对 OUTPUT_DIR 的路径列表（slides/<md5>/page_N.png），与页码一一对应；
    渲染失败的页对应位置为空串。libreoffice 或转换失败时返回全空列表，
    调用方回退到文字卡片，不让整个解析挂掉。

    按内容 md5 缓存：同一份 PPT 重复上传/重渲染不再转换。
    """
    import hashlib

    from app.core.config import settings

    if not shutil.which("soffice"):
        return [""] * pages
    digest = hashlib.md5(content).hexdigest()[:16]
    out_dir = Path(settings.OUTPUT_DIR) / "slides" / digest
    out_dir.mkdir(parents=True, exist_ok=True)

    if not list(out_dir.glob("page_*.png")):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "deck.pptx"
                src.write_bytes(content)
                subprocess.run(
                    ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
                    capture_output=True,
                    timeout=180,
                )
                pdf = Path(tmp) / "deck.pdf"
                if pdf.exists():
                    subprocess.run(
                        ["pdftoppm", "-png", "-r", "96", str(pdf), str(out_dir / "page")],
                        capture_output=True,
                        timeout=180,
                    )
        except Exception:  # noqa: BLE001
            return [""] * pages

    # pdftoppm 产出 page-1.png，规整成 page_1.png
    result: list[str] = []
    for i in range(1, pages + 1):
        dashed = out_dir / f"page-{i}.png"
        target = out_dir / f"page_{i}.png"
        if dashed.exists() and not target.exists():
            dashed.rename(target)
        result.append(f"slides/{digest}/page_{i}.png" if target.exists() else "")
    return result


class PPTParseError(Exception):
    """可预期的解析失败（坏文件、老格式、空文件），转成 400 给前端。"""


def _slide_text(slide) -> str:
    """一页里所有文本框的文字，按自上而下、自左而右排。"""
    parts: list[str] = []
    shapes = sorted(
        (s for s in slide.shapes if getattr(s, "has_text_frame", False)),
        key=lambda s: (s.top or 0, s.left or 0),
    )
    for shape in shapes:
        text = shape.text_frame.text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _slide_notes(slide) -> str:
    """演讲者备注。没有备注页时返回空。"""
    try:
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            return slide.notes_slide.notes_text_frame.text.strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _slide_title_body(slide) -> tuple[str, list[str]]:
    """拆出幻灯片的标题和要点列表，供「PPT 画面卡片」使用。

    标题优先取占位符标题（title placeholder）；取不到就用第一个文本框的第一行。
    要点是其余文本框的行，超长行截断 —— 卡片宽度有限，完整内容在画面里
    放不下，宁缺毋滥。
    """
    title = ""
    try:
        if slide.shapes.title is not None:
            title = (slide.shapes.title.text or "").strip().splitlines()[0:1]
            title = title[0].strip() if title else ""
    except Exception:  # noqa: BLE001
        title = ""

    lines: list[str] = []
    for shape in slide.shapes:
        if not getattr(shape, "has_text_frame", False):
            continue
        try:
            if slide.shapes.title is not None and shape == slide.shapes.title:
                continue  # 标题已单独展示
        except Exception:  # noqa: BLE001
            pass
        for ln in (shape.text_frame.text or "").splitlines():
            ln = ln.strip()
            if ln:
                lines.append(ln[:40])

    if not title and lines:
        title, lines = lines[0], lines[1:]
    return title, lines[:8]


def pptx_to_script(content: bytes, filename: str) -> dict:
    """把 pptx 解析成可朗读的口播稿。"""
    from pptx import Presentation

    if not content:
        raise PPTParseError("文件是空的")
    try:
        prs = Presentation(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise PPTParseError(
            "解析失败。如果是老版 .ppt 格式，请先用 PowerPoint / WPS 另存为 .pptx 再上传"
        ) from exc

    slides = []
    script_parts = []
    visual_scenes = []
    for i, slide in enumerate(prs.slides, start=1):
        text = _slide_text(slide)
        notes = _slide_notes(slide)
        slide_title, slide_lines = _slide_title_body(slide)
        slides.append(
            {"index": i, "text": text, "notes": notes, "title": slide_title, "points": slide_lines}
        )

        # 讲稿来源：备注优先，页面文字兜底
        body = notes or text
        if body.strip():
            script_parts.append(f"第{i}页。{body}")

        # 画面卡片：标题 + 要点。口播句和画面内容分开 ——
        # 备注常常是对画面的口头展开，直接上屏反而把要点淹掉
        visual_scenes.append(
            {"narration": body.strip(), "title": slide_title, "body": slide_lines}
        )

    script = "\n".join(script_parts).strip()
    if not script:
        raise PPTParseError("这份 PPT 里没有可读的文字（可能全是图片/图表）")

    title = filename.rsplit(".", 1)[0] or "PPT"
    return {
        "title": title,
        "slides_count": len(slides),
        "script": script,
        "slides": slides,
        "visual_scenes": visual_scenes,
        "chars": len(script.replace("\n", "")),
        # 约 4.5 字/秒的中文播报速度，给前端预估时长用
        "estimate_seconds": max(1, round(len(script.replace("\n", "")) / 4.5)),
    }
