"""封面图适配器（模板化封面，与 `imagegen.py` 的 AI 文内配图是两条独立通道）。

为什么单开一条通道，不复用 `imagegen`
-----------------------------------
两者产物定位不同、契约也不同：

| | `imagegen.py`（AI 生图） | 本模块（封面模板） |
|---|---|---|
| 参数 | `--prompt --size --count --out` | `--title --sub --tag --style --size --out` |
| 出图方式 | 文生图模型（随机、需写提示词） | HTML 模板 + 无头浏览器截图（确定、秒出） |
| 用途 | 文内配图 | **头图 / 封面**（公众号封面、小红书首图） |

把封面脚本硬塞进 `IMAGE_SCRIPT` 会因参数不兼容直接失败，所以独立成模块。

脚本调用契约
------------
    python <COVER_SCRIPT> --title <标题> [--sub <副标题>] [--tag <标签>]
                          [--style gradient|dark|light] [--size wide|square|og]
                          --out <输出目录>
    - 标题里的换行用 `\\n` 字面量传（脚本内部 replace 成真换行）；
    - 脚本把产出 PNG 写进 `--out` 目录，并把路径打到 stdout；
    - 依赖本机有 Chrome/Chromium（脚本自动探测，可用 `COVER_CHROME` 指定）。

未配置 / 缺浏览器时 `available()` 返回 False，上层降级为「手动上传封面」，
不抛 500。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from app.core.config import settings
from app.core.logging import logger
from app.assets.gzh.themes import cover_palette

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# 允许的风格 / 尺寸（与 make-cover.py 的 STYLES / SIZES 对齐）
COVER_STYLES: dict[str, str] = {
    "gradient": "渐变（醒目，常规更新/教程）",
    "dark": "深色（高级，深度评测/资讯）",
    "light": "浅色（清爽，生活/快讯）",
}
COVER_SIZES: dict[str, str] = {
    "wide": "公众号头条 900×383",
    "square": "方形 800×800",
    "og": "分享卡 1200×630",
}


def _script_path() -> Path:
    """解析封面脚本路径：绝对路径直接用，相对路径按「后端项目根」解析。"""
    raw = (settings.COVER_SCRIPT or "").strip()
    if not raw:
        return Path()
    p = Path(raw)
    if p.is_absolute():
        return p
    # config.py 在 backend/app/core/，往上两级 = backend/
    backend_root = Path(__file__).resolve().parents[2]
    return backend_root / raw


def available() -> bool:
    """封面通道是否可用：脚本存在 + 本机能找到无头浏览器。

    浏览器探测：把脚本当模块加载，直接读它探测出来的 `CHROME`。
    脚本文件名带 `-`（不能直接 import），所以用 importlib 按文件路径加载。
    """
    script = _script_path()
    if not script.exists():
        return False

    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location("_cwb_make_cover", script)
        if spec is None or spec.loader is None:
            return False
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return bool(getattr(mod, "CHROME", ""))
    except Exception:  # noqa: BLE001  探测失败就当不可用，不炸上层
        logger.warning("封面脚本加载失败，封面通道视为不可用：%s", script)
        return False


def derive_cover_text(body: str) -> tuple[str, str]:
    """从正文 Markdown 里解析出封面用的 (标题, 副标题)。

    规则（够用就好，用户可在前端改）：
      1. 标题 = 第一个 Markdown 标题行（`# xxx`）或第一行非空文本；
      2. 去掉常见 Markdown 记号（#、*、>、`），压缩空白；
      3. 标题超过 18 字按语义断点（、，。！？；空格）折成两行，避免字号被压得过小；
      4. 副标题 = 标题之后的第一段非空文本，截到 20 字。
    """
    if not body:
        return "", ""

    def clean(s: str) -> str:
        s = re.sub(r"^\s*#{1,6}\s*", "", s)  # 去标题记号
        s = re.sub(r"^\s*[>\-*+]\s*", "", s)  # 去引用/列表记号
        s = s.replace("*", "").replace("`", "").strip()
        return re.sub(r"\s+", " ", s)

    lines = [clean(x) for x in (body or "").splitlines()]
    lines = [x for x in lines if x]
    if not lines:
        return "", ""

    title = lines[0]
    sub = lines[1] if len(lines) > 1 else ""

    # 标题折行：过长时在靠中间位置的标点处断
    if len(title) > 18:
        mid = len(title) // 2
        for offset in range(0, len(title) // 2):
            for idx in (mid - offset, mid + offset):
                if 0 < idx < len(title) and title[idx] in "、，。！？；： ,.!?;:":
                    title = title[:idx] + "\n" + title[idx + 1 :]
                    break
            else:
                continue
            break

    title = title[:40]
    sub = sub[:20]
    return title, sub


async def generate_cover(
    title: str,
    out_dir: Path,
    *,
    sub: str = "",
    tag: str = "",
    style: str = "",
    size: str = "",
    date: str = "",
    theme: str = "",
    brand_name: str = "",
    brand_slogan: str = "",
) -> Path:
    """生成一张封面，返回 PNG 路径。

    失败一律抛 RuntimeError（带上可读中文），由路由层决定怎么提示。
    """
    if not available():
        raise RuntimeError(
            "封面通道不可用：请确认 COVER_SCRIPT 指向的脚本存在，且本机装有 Chrome/Chromium。"
        )
    if not (title or "").strip():
        raise RuntimeError("封面标题不能为空")

    script = _script_path()
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {f.name for f in out_dir.iterdir() if f.suffix.lower() in _IMG_EXTS}

    cmd = [
        sys.executable,
        str(script),
        "--title",
        title.strip(),
        "--style",
        (style or settings.COVER_STYLE or "gradient"),
        "--size",
        (size or settings.COVER_SIZE or "wide"),
        "--out",
        str(out_dir),
    ]
    if sub.strip():
        cmd += ["--sub", sub.strip()]
    tag = tag.strip() or (settings.COVER_TAG or "").strip()
    if tag:
        cmd += ["--tag", tag]
    if date.strip():
        cmd += ["--date", date.strip()]
    # 品牌名/口号：多公众号场景下让封面右下角跟随实际选用的账号，
    # 不再一律写死默认号的「码上聊聊」。两者都留空则沿用脚本默认品牌。
    if brand_name.strip():
        cmd += ["--name", brand_name.strip()]
    if brand_slogan.strip():
        cmd += ["--slogan", brand_slogan.strip()]
    # 排版主题调色板：让封面配色跟随选中的排版主题（不传则用默认品牌色，向后兼容）
    if theme.strip():
        pal = cover_palette(theme.strip())
        import json as _json

        cmd += ["--palette", _json.dumps(pal, ensure_ascii=False)]

    logger.info("调封面脚本: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.COVER_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError(f"封面渲染超时（>{settings.COVER_TIMEOUT}s）") from exc

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError(f"封面脚本退出码 {proc.returncode}: {err}")

    new_files = sorted(
        f
        for f in out_dir.iterdir()
        if f.suffix.lower() in _IMG_EXTS and f.name not in before
    )
    if not new_files:
        # 兜底：脚本把路径打在 stdout
        for line in (stdout or b"").decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.startswith("OK "):  # 脚本自带前缀
                line = line[3:].split(" (")[0].strip()
            p = Path(line)
            if p.suffix.lower() in _IMG_EXTS and p.exists():
                new_files.append(p)
    if not new_files:
        raise RuntimeError("封面脚本未产出图片文件")
    return new_files[-1]
