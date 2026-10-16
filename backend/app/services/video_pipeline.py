"""真实视频渲染管线。

管线概览
--------
    口播稿
      │
      ├─(1) 分句 ──────────── SentenceBoundary 精确到毫秒
      ├─(2) edge-tts 配音 ─── 一次请求拿到整段音频 + 每句时间戳
      ├─(3) 按句切音频 ────── ffmpeg wav 无损切片
      ├─(4) PIL 绘制卡片 ─── 渐变背景 + 大字 + 装饰环
      ├─(5) Ken Burns 运镜 ── zoompan 缓慢推近，逐 corridor 交替推/拉
      └─(6) concat 合成 ───── 音画同流 → *.mp4

三条设计决策，都是踩过/预判到坑之后的取舍
------------------------------------------
1. **字幕不用 whisper 转写**。edge-tts 合成时会直接吐 SentenceBoundary（句级
   起止时间，单位 100ns），字幕轴是合成过程的免费副产品。用 faster-whisper
   反推时间轴要额外下载模型 + 每次转写几十秒，纯属绕远路。
2. **画面不用 ffmpeg drawtext**。把文字直接画进 PIL 图片里 —— drawtext 要跟
   fontconfig、转义字符、字体回退打架，而 PIL 能精确控制换行和字号自适应。
   代价是失去后期改字幕的能力，换来的是排版可控。
3. **音频先从 mp3 解成 wav 再切片**。mp3 有帧对齐误差，直接 `-ss` 切会漂移，
   几段累积下来音画就对不上了；wav 是样本级精确寻址，切完拼回去严丝合缝。

性能
----
分辨率取 720p 级（9:16 用 720x1280），编码器是 libopenh264 纯 CPU 软编。
25fps 下 60 秒成片约 30-60 秒渲染时间，够用；上到 1080p 会翻好几倍，
短期没有收益。真把这一步跑量了再考虑接 GPU 硬编。
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.core.config import settings
from app.core.logging import logger

# ------------------------------------------------------------------ 常量
RATIO_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "1:1": (900, 900),
}
FPS = 25
VIDEO_CODEC = "libopenh264"

_FONT_DIR = Path("/usr/share/fonts/opentype/noto")
FONT_BOLD = str(_FONT_DIR / "NotoSansCJK-Bold.ttc")
FONT_REGULAR = str(_FONT_DIR / "NotoSansCJK-Regular.ttc")

# 单个画面停留上限：超过就把长句拆开，否则观众盯着一屏字看 8 秒会走神
MAX_SCENE_SEC = 6.5
MAX_SCENE_CHARS = 26

# 背景渐变调色板（顶/底）。按顺序循环取，相邻两张保证不同色，避免看起来像没换页
PALETTE: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = [
    ((28, 34, 66), (12, 15, 31)),   # 深夜蓝
    ((22, 52, 66), (10, 22, 34)),   # 青碧
    ((56, 28, 62), (22, 11, 30)),   # 紫罗兰
    ((24, 54, 48), (10, 24, 22)),   # 松绿
    ((66, 40, 24), (30, 16, 10)),   # 落日橙
    ((38, 30, 62), (16, 12, 32)),   # 靛青
]
ACCENTS = [(79, 124, 255), (45, 212, 191), (245, 165, 36), (167, 139, 250)]


@dataclass
class Scene:
    """一个画面单位：一句话（或长句拆出来的一段）。

    visual_* 是 PPT 模式特有的：画面不画口播句，而画幻灯片的
    标题 + 要点（口播句只进配音）。纯文案模式这些字段留空。
    """

    text: str
    start: float  # 秒
    end: float
    visual_title: str = ""
    visual_body: tuple[str, ...] = ()
    # PPT 模式：LibreOffice 渲染的真实幻灯片图（相对 OUTPUT_DIR），
    # 非空时画面用真图 + 模糊背景，不再画文字卡片
    slide_image: str = ""

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.05)


@dataclass
class RenderResult:
    output_path: str
    duration_sec: float
    width: int
    height: int
    size_bytes: int
    scenes: int


# ------------------------------------------------------------------ 命令执行
async def _ff(*args: str, check: bool = True, want: str = "stdout") -> str:
    """跑一条 ffmpeg/ffprobe。用子进程 + asyncio 等待，不阻塞事件循环。

    ⚠️ 为什么要区分 stdout / stderr：
      ffmpeg 把进度信息打到 stderr、把结果（如 ffprobe -of json）打到 stdout。
      早期版本只捕获 stderr，导致 ffprobe 的 JSON 永远拿到空串 —— probe_duration
      静默返回 0，直到最后一步校验才炸出来。这里按需返回。
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    stdout = (out or b"").decode("utf-8", "replace")
    stderr = (err or b"").decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"命令失败({' '.join(args[:3])}…): {stderr[-600:]}")
    return stdout if want == "stdout" else stderr


# ------------------------------------------------------------------ 1. 配音
# 单段合成的最大字数。实测微软服务端对单次请求约 300 字以上就会返回
# 「No audio was received」，留足余量取 250。
_TTS_CHUNK_CHARS = 250

_voice_cache: list[str] | None = None


async def valid_voices() -> list[str]:
    """edge-tts 当前实际可用的音色清单（带 1 小时缓存）。

    前端可选音色必须以这份清单为准 —— 像 zh-CN-XiaochenNeural 这种
    网上流传但官方已下线的名字，stream() 不会报错参数非法，而是直接
    返回空音频，排查起来非常恶心。
    """
    global _voice_cache
    if _voice_cache is None:
        import edge_tts

        voices = await edge_tts.list_voices()
        _voice_cache = sorted(v["ShortName"] for v in voices)
    return _voice_cache


def _split_for_tts(text: str) -> list[str]:
    """把口播稿切成适合单次合成的段。

    实践经验：一次性把几千字丢给 edge-tts，大概率返回
    「No audio was received. Please verify that your parameters are correct.」
    （微软服务端对超长流不稳定）。按换行/句末标点切成 ≤400 字的段，
    逐段合成再拼接，句级时间戳按段累加偏移即可无缝对齐。
    """
    import re

    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    for para in paras:
        if len(para) <= _TTS_CHUNK_CHARS:
            chunks.append(para)
            continue
        # 长段落按句末标点切，保留标点
        parts = re.split(r"(?<=[。！？；;!?])", para)
        buf = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(buf) + len(part) > _TTS_CHUNK_CHARS and buf:
                chunks.append(buf)
                buf = part
            else:
                buf += part
        if buf:
            chunks.append(buf)
    return chunks or [text.strip()]


async def _synth_chunk(
    text: str, voice: str, path: Path, attempts: int = 3
) -> list[tuple[float, float, str]]:
    """合成一段并捡回句级时间戳；失败自动重试（edge-tts 瞬时错误不少见）。"""
    import edge_tts

    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        boundaries: list[tuple[float, float, str]] = []
        try:
            comm = edge_tts.Communicate(text, voice)
            with open(path, "wb") as f:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
                        # ticks(100ns) → 秒
                        boundaries.append(
                            (chunk["offset"] / 1e7, chunk["duration"] / 1e7, chunk["text"])
                        )
            if path.stat().st_size > 1000 and boundaries:
                return boundaries
            last_err = RuntimeError("TTS 返回空音频或无时间戳")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        logger.warning("「%s…」第 %d 次合成尝试失败: %s", text[:12], attempt, last_err)
        await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"配音合成失败（已重试 {attempts} 次）: {last_err}")


async def synth_voice(
    text: str,
    voice: str,
    workdir: Path,
    on_progress: Callable[[int, str], None] | None = None,
    owner: str = "default",
) -> tuple[Path, list[Scene]]:
    """分段合成整段配音，顺手把句级时间轴捡回来。

    各段 mp3 参数一致（同音色同码率），直接按字节拼接即可；段时间偏移用
    ffprobe 实测累加，不靠估算。拿不到任何 boundary 时退化成「整段一个画面」
    —— 宁可出一版少切换的片，也不要因为拿不到时间轴就让整个任务失败。

    音色有两种：
      - edge-tts 官方音色名（zh-CN-XiaoxiaoNeural…）→ 走下面的分段逻辑
      - 复刻音色 `clone_<id>` → 交给 voices 模块，它负责自己的分段与时间轴

    on_progress 可选：复刻音色逐段合成很慢（CPU 上每段 1~2 秒），透传下去
    让进度条逐段推进，否则会长时间钉在 5%。
    """
    audio_path = workdir / "voice.mp3"

    # 复刻音色：完全独立的一条路（本地 XTTS / 云端 API），时间轴由它自报
    from app.services.voices import is_clone_voice, synth_clone

    if is_clone_voice(voice):
        logger.info("使用复刻音色 %s 合成配音", voice)
        cloned, clone_bounds = await synth_clone(
            voice, text, workdir, on_progress=on_progress, owner=owner
        )
        if cloned.resolve() != audio_path.resolve():
            import shutil as _sh

            _sh.copyfile(cloned, audio_path)
        total = await probe_duration(audio_path)
        scenes = (
            _expand_long_scenes(clone_bounds, total)
            if clone_bounds
            else [Scene(text=text, start=0.0, end=total)]
        )
        logger.info("复刻配音完成 %.1fs，切成 %d 个画面", total, len(scenes))
        return audio_path, scenes

    boundaries: list[tuple[float, float, str]] = []
    offset = 0.0

    chunks = _split_for_tts(text)
    if len(chunks) > 1:
        logger.info("配音分 %d 段合成（共 %d 字）", len(chunks), len(text))

    with open(audio_path, "wb") as out:
        for i, ck in enumerate(chunks):
            seg_path = workdir / f"voice_seg_{i:03d}.mp3"
            seg_b = await _synth_chunk(ck, voice, seg_path)
            out.write(seg_path.read_bytes())
            for s, d, t in seg_b:
                boundaries.append((s + offset, d, t))
            offset += await probe_duration(seg_path)
            seg_path.unlink(missing_ok=True)

    total = await probe_duration(audio_path)

    if not boundaries:
        logger.warning("TTS 未返回时间戳，退化为单画面")
        return audio_path, [Scene(text=text, start=0.0, end=total)]

    scenes = _expand_long_scenes(boundaries, total)
    logger.info("配音完成 %.1fs，切成 %d 个画面", total, len(scenes))
    return audio_path, scenes


def _expand_long_scenes(
    boundaries: list[tuple[float, float, str]], total: float
) -> list[Scene]:
    """把过长的场景再切一刀。

    切点优先落在逗号/顿号上（读起来自然的停顿处）；没有标点就按字数均分。
    内部时间按字符数比例线性插值 —— 这是近似：同一句内语速并非严格匀速，
    但误差在几百毫秒级，画面切换不会明显错位，比多拿一次 TTS 请求划算。
    """
    scenes: list[Scene] = []
    for start, dur, text in boundaries:
        end = start + dur
        if dur <= MAX_SCENE_SEC and len(text) <= MAX_SCENE_CHARS:
            scenes.append(Scene(text.strip(), start, end))
            continue

        parts = _split_text(text, n=math.ceil(len(text) / MAX_SCENE_CHARS))
        if len(parts) <= 1:
            scenes.append(Scene(text.strip(), start, end))
            continue

        weights = [len(p) for p in parts]
        total_w = sum(weights) or 1
        cursor = start
        for part, w in zip(parts, weights):
            seg = dur * (w / total_w)
            scenes.append(Scene(part.strip(), cursor, cursor + seg))
            cursor += seg
        # 浮点累积误差会让最后一段收尾偏一点，强制对齐到边界
        if scenes:
            scenes[-1].end = end
    return scenes


_SPLIT_RE = re.compile(r"[，,、；;：:!！?？]")


def _split_text(text: str, n: int) -> list[str]:
    """按标点切成 n 段左右；标点太少时退化成按字数均分。"""
    if n <= 1:
        return [text]
    pieces = [p for p in _SPLIT_RE.split(text) if p.strip()]
    if len(pieces) >= 2:
        return pieces
    size = math.ceil(len(text) / n)
    return [text[i : i + size] for i in range(0, len(text), size)]


async def probe_duration(path: Path) -> float:
    out = await _ff(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ 2. 音频切片
async def slice_audio(audio: Path, scenes: list[Scene], workdir: Path) -> list[Path]:
    """按场景区间把音频切开。先转 wav 再切，避免 mp3 帧对齐漂移。"""
    full_wav = workdir / "voice_full.wav"
    await _ff(
        "ffmpeg", "-y", "-i", str(audio),
        "-ac", "1", "-ar", "24000", "-acodec", "pcm_s16le", str(full_wav),
    )

    paths: list[Path] = []
    for i, sc in enumerate(scenes):
        out = workdir / f"audio_{i:03d}.wav"
        await _ff(
            "ffmpeg", "-y",
            "-ss", f"{sc.start:.3f}",
            "-t", f"{sc.duration:.3f}",
            "-i", str(full_wav),
            "-ac", "1", "-ar", "24000",
            str(out),
        )
        paths.append(out)
    return paths


# ------------------------------------------------------------------ 3. 画卡片
def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    """取字体。系统在容器里未必装 CJK 字体，找不到就退回 PIL 默认字体
    —— 那样中文会画成方块，但至少不会让渲染任务直接崩掉。"""
    candidates = [FONT_BOLD if bold else FONT_REGULAR, FONT_REGULAR]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    logger.warning("未找到 CJK 字体(%s)，中文可能显示为方块", _FONT_DIR)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """逐字试排换行。中文没有空格，只能按字宽累加。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if draw.textlength(trial, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def build_card(scene: Scene, idx: int, total: int, size: tuple[int, int], out: Path) -> None:
    """绘制一张卡片：渐变底 + 装饰环 + 大字 + 页码。

    字号自适应：从大到小试探，取第一个能在画布里放下的字号。
    这样 6 字短句能撑满画面、26 字长句也不会溢出。
    """
    W, H = size
    top, bottom = PALETTE[idx % len(PALETTE)]
    accent = ACCENTS[idx % len(ACCENTS)]

    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)

    # 垂直渐变
    for y in range(H):
        t = y / max(H - 1, 1)
        draw.line(
            [(0, y), (W, y)],
            fill=(
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
            ),
        )

    # 装饰：右上角大圆环 + 一个实心点，给纯色背景加层次
    ring_r = int(min(W, H) * 0.28)
    cx, cy = int(W * 0.88), int(H * 0.16)
    r, g, b = accent
    ring = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
        outline=(r, g, b, 70),
        width=max(3, W // 200),
    )
    ImageDraw.Draw(ring).ellipse(
        [int(W * 0.1), int(H * 0.72), int(W * 0.1) + W // 24, int(H * 0.72) + W // 24],
        fill=(r, g, b, 90),
    )
    img = Image.alpha_composite(img.convert("RGBA"), ring).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 页码
    page_font = _font(W // 26, bold=False)
    draw.text((W * 0.08, H * 0.09), f"{idx + 1:02d} / {total:02d}", font=page_font, fill=(200, 210, 230))

    # PPT 模式：有真实幻灯片图就用真图（模糊背景 + 完整包含），
    # 否则画标题+要点的文字卡片；口播句都不进画面
    if scene.slide_image:
        slide_path = Path(settings.OUTPUT_DIR) / scene.slide_image
        if slide_path.is_file():
            _slide_photo_card(slide_path, size, out)
            return
    if scene.visual_title or scene.visual_body:
        _draw_slide_content(draw, img, scene, accent, size)
        img.save(out, quality=95)
        return

    # 主文案：自适应字号
    max_w = int(W * 0.84)
    max_h = int(H * 0.52)
    body = scene.text
    for fs in range(int(W / 7), int(W / 18), -4):
        font = _font(fs)
        lines = _wrap(draw, body, font, max_w)
        line_h = fs * 1.42
        if len(lines) * line_h <= max_h:
            break
    else:
        font = _font(int(W / 18))
        lines = _wrap(draw, body, font, max_w)

    line_h = font.size * 1.42
    block_h = len(lines) * line_h
    y = H * 0.5 - block_h * 0.5

    # 文字 + 柔阴影（直接画一层深色偏移，比 render_filters 的模糊快得多）
    for ln in lines:
        x = (W - draw.textlength(ln, font=font)) / 2
        draw.text((x + 3, y + 4), ln, font=font, fill=(0, 0, 0))
        draw.text((x, y), ln, font=font, fill=(255, 255, 255))
        y += line_h

    # 底部强调条
    bar_w = int(W * 0.22)
    draw.rectangle(
        [int((W - bar_w) / 2), int(H * 0.86), int((W + bar_w) / 2), int(H * 0.86) + max(4, H // 320)],
        fill=accent,
    )

    img.save(out, quality=95)


def _slide_photo_card(slide_path: Path, size: tuple[int, int], out: Path) -> None:
    """真实幻灯片卡片：模糊填充背景 + 完整等比包含幻灯片。

    幻灯片大多是 4:3/16:9 横版，竖屏画布直接 cover 会裁掉内容，
    所以用「模糊背景 + contain 前景」的电视墙式构图。
    """
    W, H = size
    slide = Image.open(slide_path).convert("RGB")
    sw, sh = slide.size

    scale = max(W / sw, H / sh)
    bg = slide.resize((math.ceil(sw * scale), math.ceil(sh * scale)), Image.LANCZOS)
    left, top = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(48))
    bg = Image.blend(bg, Image.new("RGB", size, (8, 10, 16)), 0.45)

    s2 = min(W / sw, H / sh) * 0.9
    fg = slide.resize((max(1, round(sw * s2)), max(1, round(sh * s2))), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    bg.save(out, quality=95)


def _draw_slide_content(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    scene: Scene,
    accent: tuple[int, int, int],
    size: tuple[int, int],
) -> None:
    """PPT 幻灯片卡片：左上小标签 + 大标题（≤2 行）+ 强调下划线 + 要点列表。

    和 build_card 的大字报风格保持一致（白字、柔阴影、强调色），
    只改信息层级：标题在上、要点在下。
    """
    W, H = size
    margin_x = int(W * 0.08)

    # 顶部标签
    kicker_font = _font(W // 24, bold=False)
    draw.text((margin_x, H * 0.13), "SLIDE", font=kicker_font, fill=accent)

    # 标题：自适应到最多 2 行
    title = scene.visual_title.strip() or scene.text.strip()
    title_fs = int(W / 10)
    max_w = int(W * 0.84)
    for fs in range(title_fs, int(W / 22), -4):
        font = _font(fs, bold=True)
        lines = _wrap(draw, title, font, max_w)
        if len(lines) <= 2:
            break
    else:
        font = _font(int(W / 22), bold=True)
        lines = _wrap(draw, title, font, max_w)[:2]

    y = H * 0.19
    line_h = font.size * 1.4
    for ln in lines:
        draw.text((margin_x + 3, y + 4), ln, font=font, fill=(0, 0, 0))
        draw.text((margin_x, y), ln, font=font, fill=(255, 255, 255))
        y += line_h

    # 标题下的强调短线
    y += H * 0.015
    draw.rectangle(
        [margin_x, int(y), margin_x + int(W * 0.18), int(y) + max(4, H // 320)],
        fill=accent,
    )
    y += H * 0.045

    # 要点列表：每条自动换行，行首加 "·"，最多铺满到 82% 高度
    body_font = None
    body_lines: list[str] = []
    max_bottom = H * 0.82
    for fs in range(int(W / 16), int(W / 30), -2):
        body_font = _font(fs)
        body_lines = []
        for item in scene.visual_body or (scene.text,):
            item = item.strip()
            if not item:
                continue
            wrapped = _wrap(draw, item, body_font, max_w - int(W * 0.05))
            for i, ln in enumerate(wrapped):
                body_lines.append(("· " if i == 0 else "  ") + ln)
        if (y + len(body_lines) * fs * 1.5) <= max_bottom or fs <= int(W / 30) + 2:
            break

    assert body_font is not None
    line_h = body_font.size * 1.5
    for ln in body_lines:
        draw.text((margin_x + 2, y + 3), ln, font=body_font, fill=(0, 0, 0))
        draw.text((margin_x, y), ln, font=body_font, fill=(232, 236, 245))
        y += line_h
        if y > max_bottom:
            break


async def build_cards(
    scenes: list[Scene], size: tuple[int, int], workdir: Path
) -> list[Path]:
    """PIL 是 CPU 密集的同步活，丢线程池跑，别卡住事件循环。"""
    paths = [workdir / f"card_{i:03d}.png" for i in range(len(scenes))]

    def _work() -> None:
        for i, (sc, p) in enumerate(zip(scenes, paths)):
            build_card(sc, i, len(scenes), size, p)

    await asyncio.to_thread(_work)
    return paths


# ------------------------------------------------------------------ 4. Ken Burns
async def render_clip(
    card: Path, audio: Path, dur: float, size: tuple[int, int], out: Path, zoom: bool = True
) -> None:
    """单段：静止图 + 缓慢推近 + 该段音频 → 一段等长视频。"""
    W, H = size
    frames = max(int(dur * FPS) + 25, 30)

    if zoom:
        # 让镜头在 dur 秒内从 1.0 匀速推到 target，运镜速率与时长解耦：
        # 每帧增量 = (target-1)/frames。不这么算的话短的画面推得慢、长的推过头。
        target = 1.12
        delta = (target - 1.0) / frames
        vf = (
            f"scale={int(W*1.2)}:{int(H*1.2)},"
            f"zoompan=z='min(1+{delta:.8f}*on,{target})'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={W}x{H}:fps={FPS},format=yuv420p"
        )
    else:
        vf = f"scale={W}:{H},format=yuv420p"

    await _ff(
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", str(FPS), "-i", str(card),
        "-i", str(audio),
        "-vf", vf,
        "-c:v", VIDEO_CODEC, "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{dur:.3f}", "-shortest",
        str(out),
        check=False,
    )


# ------------------------------------------------------------------ 5. 合成
def _fmt_srt_time(seconds: float) -> str:
    """SRT 时间戳格式：HH:MM:SS,mmm（注意是逗号不是点）。"""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(scenes: list[Scene], out: Path) -> Path:
    """把口播句写成 SRT 字幕文件。

    字幕内容 = 每个 Scene 的口播文本，时间轴 = 该 Scene 的 start/end。
    Scene 的时间轴本来就是按实测音频时长累加出来的（见 synth_voice），
    所以句级对齐是准的 —— 不需要额外转写。

    为什么要做字幕：口播稿在「纯文案模式」下是画进画面卡片的，切句后一句话
    一闪而过、还跟朗读不完全同步；PPT 模式下更是完全不显示口播内容。把它做成
    独立字幕轨，既能让用户看到「念到哪」，又能导出 srt 给其它平台用。
    """
    lines: list[str] = []
    # 序号从 1 开始，跳过空白句（纯标点/空串）
    n = 0
    for sc in scenes:
        text = (sc.text or "").strip()
        if not text:
            continue
        # 内部换行会破坏 SRT 单条结构，压成一行
        text = " ".join(text.split())
        n += 1
        lines.append(str(n))
        lines.append(f"{_fmt_srt_time(sc.start)} --> {_fmt_srt_time(sc.end)}")
        lines.append(text)
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


async def concat_clips(clips: list[Path], out: Path, *, subtitle: Path | None = None) -> None:
    """拼接。优先 concat demuxer 直copy（秒级），失败降级到 filter（会重编码）。

    subtitle 非空时把 SRT 作为**软字幕轨**（mov_text）封装进 mp4：
    播放器里可开关、也能导出，不影响画面像素。
    """
    list_file = out.parent / "clips.txt"
    list_file.write_text(
        "".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8"
    )
    try:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
        ]
        if subtitle is not None:
            # 第二个输入是 srt；-c:s mov_text 让 mp4 认这条字幕轨
            cmd += ["-i", str(subtitle), "-c:v", "copy", "-c:a", "copy",
                    "-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
        else:
            cmd += ["-c", "copy"]
        cmd.append(str(out))
        await _ff(*cmd)
        return
    except RuntimeError as exc:
        logger.warning("concat copy 失败(%s)，降级用 filter 重编码", exc)

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    filters = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(clips)))
    cmd = [
        "ffmpeg", "-y", *inputs,
    ]
    if subtitle is not None:
        cmd += ["-i", str(subtitle)]
    cmd += [
        "-filter_complex", f"{filters}concat=n={len(clips)}:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", VIDEO_CODEC, "-pix_fmt", "yuv420p", "-c:a", "aac",
    ]
    if subtitle is not None:
        cmd += ["-map", f"{len(clips)}:s:0", "-c:s", "mov_text",
                "-metadata:s:s:0", "language=chi"]
    cmd.append(str(out))
    await _ff(*cmd)


async def probe_video(path: Path) -> dict:
    out = await _ff(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size",
        "-show_entries", "stream=width,height",
        "-of", "json", str(path),
    )
    import json

    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return {
        "duration": float(fmt.get("duration") or 0),
        "size": int(fmt.get("size") or 0),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
    }


# ------------------------------------------------------------------ 主入口
ProgressCb = Callable[[int, str], None]


def _apply_visual_scenes(scenes: list[Scene], visual_scenes: list[dict]) -> None:
    """把 PPT 解析出的「每页标题+要点」映射到时间轴场景上。

    时间轴场景数由配音句数决定，幻灯片页数由 PPT 决定，两者不保证相等：
    - 页数 == 场景数：一一对应；
    - 页数 < 场景数：按比例铺开（一页可能停留多个镜头）；
    - 页数 > 场景数：后面的页展示不下，丢弃（画面总比配音短，观感尚可）。
    """
    visuals = [v for v in visual_scenes if str(v.get("narration") or v.get("title") or "").strip()]
    if not visuals or not scenes:
        return
    n, m = len(scenes), len(visuals)
    for i, sc in enumerate(scenes):
        v = visuals[min(int(i * m / n), m - 1)]
        title = str(v.get("title") or "")
        body = v.get("body") or []
        if isinstance(body, str):
            body = [body]
        sc.visual_title = title
        sc.visual_body = tuple(str(x) for x in body if str(x).strip())
        sc.slide_image = str(v.get("slide_image") or "")


async def render_video(
    *,
    script: str,
    voice: str,
    ratio: str = "9:16",
    workdir: Path,
    output_path: Path,
    on_progress: ProgressCb | None = None,
    visual_scenes: list[dict] | None = None,
    subtitle: bool = True,
    owner: str = "default",
) -> RenderResult:
    """跑完整条管线，产出成片。

    每个阶段都会回进度，失败时抛 RuntimeError —— 上层负责把异常转成 job.error。

    subtitle=True 时把口播稿作为**软字幕轨**封装进成片（见 build_srt）。
    """
    size = RATIO_SIZES.get(ratio, RATIO_SIZES["9:16"])
    workdir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(p: int, s: str) -> None:
        if on_progress:
            on_progress(p, s)

    emit(5, "正在合成配音")
    audio, scenes = await synth_voice(script, voice, workdir, on_progress=emit, owner=owner)
    if visual_scenes:
        _apply_visual_scenes(scenes, visual_scenes)
        logger.info("PPT 模式：%d 页幻灯片映射到 %d 个镜头", len(visual_scenes), len(scenes))

    emit(30, "正在切分配音")
    audios = await slice_audio(audio, scenes, workdir)

    emit(45, "正在绘制画面")
    cards = await build_cards(scenes, size, workdir)

    clips: list[Path] = []
    for i, (card, seg_audio, sc) in enumerate(zip(cards, audios, scenes)):
        clip = workdir / f"clip_{i:03d}.mp4"
        # 奇偶交替推近/拉远，避免所有镜头方向完全一致显得呆板
        await render_clip(card, seg_audio, sc.duration, size, clip, zoom=(i % 2 == 0))
        clips.append(clip)
        pct = 55 + int(35 * (i + 1) / max(len(scenes), 1))
        emit(pct, f"正在渲染画面 {i + 1}/{len(scenes)}")

    emit(92, "正在合成成片")
    # 口播稿写成 SRT 作为软字幕轨（播放器可开关 / 可导出）
    srt_path: Path | None = None
    if subtitle:
        srt_path = build_srt(scenes, workdir / "subtitle.srt")
        logger.info("已生成字幕 %d 条", max(0, srt_path.read_text('utf-8').count('-->')))
    await concat_clips(clips, output_path, subtitle=srt_path)

    emit(97, "正在校验成片")
    info = await probe_video(output_path)
    if info["duration"] < 0.5 or info["size"] < 1024:
        raise RuntimeError(
            f"成片异常：时长 {info['duration']}s / 大小 {info['size']}B，疑似编码失败"
        )

    emit(100, "完成")
    return RenderResult(
        output_path=str(output_path),
        duration_sec=round(info["duration"], 2),
        width=info["width"],
        height=info["height"],
        size_bytes=info["size"],
        scenes=len(scenes),
    )
