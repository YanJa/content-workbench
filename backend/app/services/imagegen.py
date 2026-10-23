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
import base64
import binascii
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 中日韩表意文字/假名/谚文：命中任意一种就认为「这描述模型大概率吃不消」
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

# flux-1-schnell 等 Cloudflare 生图模型的提示词翻译系统提示。
# 2026-10-21 实测：中文提示词「一杯咖啡旁的笔记本电脑…俯视图」生成的是古风
# 仕女图（还把汉字当纹理画进角落），同一语义的英文提示词则精确命中——
# 这模型对中文语义基本不识，必须译成英文再喂。
_TRANSLATE_SYS = (
    "你是生图提示词翻译器。用户给你一段中文图片描述，你输出对应的英文提示词，"
    "用于 FLUX 文生图模型。规则：\n"
    "1. 只输出英文提示词本身，不要任何解释、引号、前后缀；\n"
    "2. 完整保留描述里的主体、动作、场景、光线、构图、色调、风格，不增删语义；\n"
    "3. 用英文生图常用的「名词短语 + 逗号分隔」形式，例如：a laptop next to a "
    "cup of coffee, warm light, office desk, flat lay, top view, photorealistic；\n"
    "4. 原文没有风格/质量词时补 2-3 个通用词（如 soft lighting / high detail），"
    "不要改变原意；\n"
    "5. 60 词以内；原文已是英文或无需翻译时原样返回。"
)


async def _translate_prompt(prompt: str, owner: str | None = None) -> str:
    """把中文配图描述译成 flux 友好的英文提示词。

    只在**含 CJK 字符**时才翻（纯英文描述模型本来就懂）。翻译失败 /
    LLM 未配置 / 结果异常（翻回来还是中文、空、过长）一律回落原提示词——
    翻译是增强，不能因为它把生图整个挡死。

    走模型池的 image_prompt_translate 槽（2026-10-24）：以前这里 `LLMClient()`
    新建实例、而且**不传 usage_sink** —— 这笔调用从来没进过用量账。
    """
    if not settings.CLOUDFLARE_IMAGE_TRANSLATE:
        return prompt
    if not _CJK_RE.search(prompt):
        return prompt

    try:
        from app.db.session import SessionLocal
        from app.services.llm import get_llm
        from app.services.usage import make_sink

        # 生图是后台任务，没有请求级 session —— 自开一个（照 knowledge.py 的惯例）
        async with SessionLocal() as db:
            res = await get_llm().chat(
                messages=[
                    {"role": "system", "content": _TRANSLATE_SYS},
                    {"role": "user", "content": prompt},
                ],
                task="lightweight",
                feature="image_prompt_translate",
                owner=owner,
                usage_sink=make_sink(db, owner=owner or "default", scene="imagegen"),
            )
            await db.commit()
        out = (res.content or "").strip().strip('"').strip()
        # sanity：非空、别又把中文吐回来、别长到离谱
        if out and not _CJK_RE.search(out) and len(out) <= 400:
            logger.info("提示词已译为英文: %s -> %s", prompt[:40], out[:80])
            return out
        logger.warning("提示词翻译结果异常，回落原提示词: %s", out[:80])
    except Exception as exc:  # noqa: BLE001
        logger.warning("提示词翻译失败，回落原提示词: %s", exc)
    return prompt


def configured() -> bool:
    """任一文生图通道可用（本地脚本 或 Cloudflare API）即视为可用。"""
    return script_configured() or cloudflare_configured()


def script_configured() -> bool:
    """生图脚本是否已配置且存在。"""
    if not settings.IMAGE_SCRIPT:
        return False
    return Path(settings.IMAGE_SCRIPT).exists()


def cloudflare_configured() -> bool:
    """Cloudflare Workers AI 文生图是否可用（需 token + account_id）。"""
    return bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID)


async def remote_ready(owner: str | None = None, db: Any = None) -> bool:
    """「远程生图」通道是否可用：模型池的 image 槽，或老的 CF 直连。

    2026-10-24：模型池上线后 CF 不再是唯一的远程通道。上层（路由层的前置
    校验、状态面板）应该用这个，而不是 `cloudflare_configured()` —— 后者只认
    .env，池里配了别的厂商也会被判成「未配置」。
    """
    if cloudflare_configured():
        return True
    try:
        from app.services.model_pool import resolve_for

        return await resolve_for("image", owner, db) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型池生图通道探测失败: %s", exc)
        return False


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
    prompt: str,
    size: str = "",
    n: int = 1,
    out_dir: Path | None = None,
    provider: str = "script",
    owner: str | None = None,
    db: Any = None,
) -> list[Path]:
    """生成 n 张图，返回文件路径列表。out_dir 为空则用默认临时目录。

    provider（沿用前端既有取值，语义已扩展）:
      - "script"（默认）：调本地生图脚本（IMAGE_SCRIPT）；
      - "cloudflare"：**远程生图**——优先走模型池的 image 槽（可以是 CF，
        也可以是火山/硅基/百炼等任何配进来的厂商），池里没有可用的才回落
        .env 里的 Cloudflare 直连。名字是历史遗留，不是「只能用 CF」。
    """
    out = out_dir or (Path(settings.UPLOAD_DIR) / "gen" / "_tmp")
    out.mkdir(parents=True, exist_ok=True)

    if provider == "cloudflare":
        return await _run_remote(prompt, n, out, owner=owner, db=db)

    if not script_configured():
        raise RuntimeError("未配置生图脚本（IMAGE_SCRIPT）")
    return await _run_script(prompt, size or settings.IMAGE_SIZE, n, out)


async def _run_remote(
    prompt: str, n: int, out_dir: Path, owner: str | None = None, db: Any = None
) -> list[Path]:
    """远程生图：模型池优先，.env 的 CF 通道兜底。

    这个「兜底」不是保守，是必须的：模型池是后加的增强，池里一条 image
    模型都没配（或全被额度熔断）时，老用户点生图不能突然就不可用了。
    """
    from app.services.model_pool import resolve_for

    spec = None
    try:
        spec = await resolve_for("image", owner, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型池解析生图模型失败，回落 .env 通道: %s", exc)

    if spec is None:
        if not cloudflare_configured():
            raise RuntimeError(
                "没有可用的生图模型：模型池的「文生图」槽为空，"
                "且未配置 Cloudflare（CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID）"
            )
        return await _run_cloudflare(prompt, n, out_dir)

    # 中文提示词翻译是 CF flux 的专属补丁：它对中文语义基本不识。国内厂商的
    # 文生图模型中文理解普遍没问题，翻译反而会丢语义——只给 CF 开。
    if spec.provider_key == "cloudflare":
        prompt = await _translate_prompt(prompt, owner)
    return await _run_pool(spec, prompt, n, out_dir)


async def _run_pool(spec: Any, prompt: str, n: int, out_dir: Path) -> list[Path]:
    """走模型池执行器生图，把字节存盘。"""
    from app.services import model_exec

    logger.info("模型池生图: %s / %s (n=%d)", spec.provider_key, spec.model, n)
    raw_list = await model_exec.run_image(spec, prompt, n=n, size=settings.IMAGE_SIZE)
    out_paths: list[Path] = []
    for raw in raw_list:
        dest = out_dir / f"pool_{uuid.uuid4().hex[:12]}.png"
        dest.write_bytes(raw)
        out_paths.append(dest)
    return out_paths


async def _run_cloudflare(prompt: str, n: int, out_dir: Path) -> list[Path]:
    """调 Cloudflare Workers AI 文生图，把返回的 base64 图存盘并返回路径列表。

    默认直连 api.cloudflare.com；配了 CLOUDFLARE_PROXY（socks5/http）则走固定
    出口（SSH -D 隧道那头那台机器），用于直连不稳定/被限流的场景。
    """
    import httpx

    # 中文描述先译成英文再喂模型（见 _translate_prompt 的实测说明）
    prompt = await _translate_prompt(prompt)

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.CLOUDFLARE_ACCOUNT_ID}/ai/run/{settings.CLOUDFLARE_IMAGE_MODEL}"
    )
    headers = {
        "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    logger.info("调 Cloudflare 文生图: %s (n=%d)", settings.CLOUDFLARE_IMAGE_MODEL, n)
    client_kw: dict = {"timeout": settings.CLOUDFLARE_IMAGE_TIMEOUT}
    proxy = (settings.CLOUDFLARE_PROXY or "").strip()
    if proxy:
        # 固定出口代理（SSH -D 隧道），只在直连 api.cloudflare.com 不稳定时开启
        client_kw["proxy"] = proxy
    try:
        async with httpx.AsyncClient(**client_kw) as client:
            resp = await client.post(url, headers=headers, json={"prompt": prompt})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Cloudflare 文生图请求失败：{exc}") from exc

    # 响应形如 {"result": {"images": ["<base64>", ...]}}，部分模型可能嵌套一层。
    # 但实测 flux-1-schnell 返回的是单数字段 {"result": {"image": "<base64>"}}，
    # 只认 images 数组会直接把好图当异常扔掉（2026-10-19 自检踩中）。
    result = data.get("result") or {}
    images_b64 = result.get("images") if isinstance(result, dict) else None
    if not images_b64:
        # 兼容有的模型直接返回 base64 列表
        images_b64 = data.get("images") if isinstance(data, dict) else None
    if not images_b64:
        # 兼容 flux-1-schnell 等模型的单数 image 字段
        single = result.get("image") if isinstance(result, dict) else None
        if isinstance(single, str) and single:
            images_b64 = [single]
    if not images_b64:
        raise RuntimeError(f"Cloudflare 返回结构异常：{str(data)[:300]}")

    out_paths: list[Path] = []
    for i, b64 in enumerate(images_b64[:n]):
        if "," in b64:  # 可能带 data:image/png;base64, 前缀
            b64 = b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(b64)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(f"Cloudflare 返回的图片解码失败：{exc}") from exc
        fname = f"cf_{uuid.uuid4().hex[:12]}.png"
        dest = out_dir / fname
        dest.write_bytes(raw)
        out_paths.append(dest)
    if not out_paths:
        raise RuntimeError("Cloudflare 未返回任何图片")
    return out_paths
