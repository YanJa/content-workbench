"""本地生图脚本适配器。

设计取舍（2026-10-16）：
    后端 FastAPI 进程**调不到宿主 Agent 的绘图工具**（跨进程、非 HTTP），
    要让网页按钮「点一下出图」，只能由后端自己触发生图。

    用户会提供一个本地生图脚本。本模块作为适配器调用它：
      - 脚本未配置（IMAGE_SCRIPT 为空）→ `configured()` 返回 False，
        上层接口降级提示「可手动上传图片」；
      - 脚本到位后，按下面约定的 CLI 契约调用（若脚本接口不同，改 `_run_script`）。

脚本调用契约（约定，脚本到位后按实际调整）：
    python <IMAGE_SCRIPT> --prompt "<提示词>" --size 1024x1024 --count 1 --out <输出目录>
    - 退出码 0 表示成功；
    - 生成的图片写入 --out 目录，文件名不限（png/jpg/webp）；
    - 也允许脚本把图片路径按行打印到 stdout（兼容「不认 --out」的脚本）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from app.core.config import settings
from app.core.logging import logger

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def configured() -> bool:
    """生图脚本是否已配置且存在。"""
    if not settings.IMAGE_SCRIPT:
        return False
    return Path(settings.IMAGE_SCRIPT).exists()


async def _run_script(prompt: str, size: str, n: int, out_dir: Path) -> list[Path]:
    """调用生图脚本，返回生成的图片文件路径列表。

    实现要点：脚本可能是 .py，也可能是可执行文件，统一用「能跑就行」的方式调用。
    """
    script = settings.IMAGE_SCRIPT
    py = [sys.executable, script] if script.endswith(".py") else [script]
    cmd = py + [
        "--prompt", prompt,
        "--size", size,
        "--count", str(n),
        "--out", str(out_dir),
    ]
    logger.info("调生图脚本: %s", " ".join(cmd))

    # 记录调用前目录里已有的文件，调用后取「新增的」——避免把历史图当成本次产出
    before = {f.name for f in out_dir.iterdir() if f.suffix.lower() in _IMG_EXTS}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.IMAGE_SCRIPT_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError(f"生图脚本超时（>{settings.IMAGE_SCRIPT_TIMEOUT}s）") from exc

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace")[:500]
        raise RuntimeError(f"生图脚本退出码 {proc.returncode}: {err}")

    # 1) 优先用目录里的新增文件
    new_files = sorted(
        f for f in out_dir.iterdir()
        if f.suffix.lower() in _IMG_EXTS and f.name not in before
    )
    # 2) 兜底：脚本可能把路径打在 stdout
    if not new_files:
        for line in (stdout or b"").decode("utf-8", "replace").splitlines():
            p = Path(line.strip())
            if p.suffix.lower() in _IMG_EXTS and p.exists():
                new_files.append(p)

    if not new_files:
        raise RuntimeError("生图脚本未产出任何图片文件")

    # 统一落到 out_dir（脚本可能输出到别处）
    result: list[Path] = []
    for f in new_files:
        if f.parent != out_dir:
            dest = out_dir / f.name
            shutil.move(str(f), str(dest))
            f = dest
        result.append(f)
    return result


async def generate_image(
    prompt: str, size: str = "", n: int = 1, out_dir: Path | None = None
) -> list[Path]:
    """生成 n 张图，返回文件路径列表。out_dir 为空则用默认临时目录。"""
    if not configured():
        raise RuntimeError("未配置生图脚本（IMAGE_SCRIPT）")

    out = out_dir or (Path(settings.UPLOAD_DIR) / "gen" / "_tmp")
    out.mkdir(parents=True, exist_ok=True)
    return await _run_script(prompt, size or settings.IMAGE_SIZE, n, out)
