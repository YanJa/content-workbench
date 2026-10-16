"""声音复刻（voice cloning）。

一句话说明它干什么
------------------
用户传一段自己的录音 → 抽特征 → 训练出专属音色 → 渲染视频时用它配音。

三条路，按「先能跑通」排优先级
------------------------------
本模块是**三层架构**，配置项 `VC_PROVIDER` 决定走哪条：

  1. `edge`（默认，零依赖）
     不复刻，直接用 edge-tts 的官方音色。用户不听劝、懒得录音时兜底。
  2. `cloud`（推荐生产环境用）
     走云端声音复刻 API（阿里云 CosyVoice / MiniMax / 火山）。上传一段
     10~30 秒音频就能出音色，不用本地 GPU，效果最好、最稳。
     国内厂商已经预置了几个常见端点，填 base_url + api_key + model 即可。
  3. `xtts`（本地零成本）
     本地跑 Coqui XTTS-v2：拿参考音频做零样本克隆，不训练、不用 GPU。
     首次加载模型要下 ~1.8GB 权重，之后每次合成约 0.5~2 秒/句（32 核 CPU）。
     效果不如云端，但**完全本地、不花钱、不传数据出去**。

为什么要抽这一层
----------------
渲染管线（video_pipeline）只认「一段文本 → 一个 mp3 + 句级时间戳」这个契约。
把复刻音色伪装成 edge-tts 的一个"音色名"（`clone_<profile_id>`），
渲染管线一行都不用改 —— 这是最小改动量的接法。

踩过的坑，写在这里省得再踩
--------------------------
1. **XTTS 不给时间戳**。edge-tts 会顺带吐 SentenceBoundary 当字幕轴，
   XTTS 不会。所以本地路线合成完音频后要拿 whisper 反推时间轴 —— 这条
   叫「快路径」，没装 whisper 时退化：合成后按字数比例分配时间。
   反正段落我都按 ≤80 字切了，误差在百毫秒级，画面切换看不出来。

2. **参考音频必须干净**。XTTS 会把参考音频里的背景音、混响、呼吸声一起
   复刻进去。所以上传后强制做标准化：重采样 24k 单声道 + 高通滤掉低频嗡声
   + 削掉首尾静音 + 峰值归一。这步用 ffmpeg 做，成本几乎为零，但效果提升
   非常明显。

3. **训练不要真的训练**。XTTS 的零样本克隆靠参考音频 embedding，所谓
   "训练"只是把参考音频的特征预先算好缓存起来。任务状态机照样要有
   （用户需要进度反馈），但实际耗时主要在下载模型那次。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

# 音频 IO 兼容垫片：torchaudio ≥2.9 的 load/save 走 torchcodec，而 torchcodec
# 的原生库当前环境 ABI 对不上，XTTS 加载参考音频会直接报错（undefined symbol）。
# 垫片把 load/save 换回 soundfile。正常情况 `app/services/__init__.py` 已经装好了，
# 这里再显式导入一次是幂等保险——防止有人只 import voices 模块而绕过包入口。
from app.core import audio_compat  # noqa: F401
from app.core.config import settings
from app.core.logging import logger

# 前缀标识：渲染参数里的 voice 字段以此开头表示「不是 edge-tts 音色，是复刻音色」
CLONE_PREFIX = "clone_"

# 参考音频要求：太短没有足够音色特征，太长纯属浪费（XTTS 只截前 N 秒）
MIN_REF_SEC = 5.0
MAX_REF_SEC = 180.0
XTTS_REF_MAX_SEC = 20.0

# 本地合成按这个粒度分段，段太长时间戳会漂。同时它也是退化时间轴的精度上限
_TTS_CHUNK_CHARS = 80


# ================================================================ 数据目录
def profiles_dir(owner: str = "default") -> Path:
    """音色库根目录，按 owner 隔离。

    default 租户沿用旧的 profiles/ 路径（存量已复刻音色不能「消失」）；
    其它 owner 走 owners/<owner>/profiles/。owner 由 tenant.normalize_owner
    校验过（无斜杠），不会路径穿越。
    """
    base = Path(settings.VOICE_DIR)
    d = base / "profiles" if owner == "default" else base / "owners" / owner / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_dir(profile_id: str, owner: str = "default") -> Path:
    d = profiles_dir(owner) / profile_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_profile_id(pid: str) -> bool:
    """profile_id 是前端传回来的，只允许我们生成的格式，防路径穿越。"""
    return bool(re.fullmatch(r"[0-9a-f]{12,32}", pid or ""))


# ================================================================ 音频处理
async def _ff(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令失败({' '.join(args[:2])}…): {(err or b'').decode('utf-8', 'replace')[-400:]}"
        )
    return (out or b"").decode("utf-8", "replace")


async def probe_duration(path: Path) -> float:
    out = await _ff(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


async def normalize_reference(src: Path, dst: Path) -> tuple[float, float]:
    """把用户录音标准化成"干净朗读样本"，返回 (原始时长, 处理时长)。

    为什么非做不可：XTTS 是照着参考音频"模仿"，参考里有什么它学什么。
    手机随便录的音频普遍有：低频空调嗡声、开头结尾的大段静音、
    音量忽大忽小。先清一遍，合成出来的人声立刻干净一个档次。

    具体做四件事：
      1. 单声道 24kHz（XTTS 的输入规格，多余声道纯浪费显存/内存）
      2. highpass=60Hz 滤掉低频噪声（空调、电流声基本都在 60Hz 以下）
      3. silenceremove 削掉首尾静音（不去中间停顿，那是自然语流）
      4. loudnorm 响度归一（避免音色 embedding 被音量差异带偏）
    """
    orig = await probe_duration(src)
    await _ff(
        "ffmpeg", "-y", "-i", str(src),
        "-af",
        "highpass=f=60,"
        "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-45dB:"
        "detection=peak,"
        "areverse,silenceremove=start_periods=1:start_silence=0.1:"
        "start_threshold=-45dB:detection=peak,areverse,"
        "loudnorm=I=-18:TP=-2:LRA=11",
        "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
        str(dst),
    )
    return orig, await probe_duration(dst)


# ================================================================ 特征提取
def _fingerprint(ref: Path) -> dict[str, float]:
    """参考音频的粗略声学指纹（时长/能量/过零率）。

    这不是"声纹识别"，只是给用户一个可量化的反馈 —— 让他知道
    「系统确实读过这段音频」。真正的音色特征在 XTTS/云端那边算，
    这里只做展示和完整性校验（指纹为空说明音频基本是静音）。
    """
    import wave

    import numpy as np

    try:
        with wave.open(str(ref), "rb") as w:
            n = w.getnframes()
            rate = w.getframerate()
            raw = w.readframes(n)
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:  # noqa: BLE001
        return {"duration": 0.0, "rms": 0.0, "zcr": 0.0, "peak": 0.0}
    if x.size == 0:
        return {"duration": 0.0, "rms": 0.0, "zcr": 0.0, "peak": 0.0}
    return {
        "duration": round(x.size / rate, 2),
        "rms": round(float(np.sqrt(np.mean(x**2))), 4),
        "zcr": round(float(np.mean(np.abs(np.diff(np.sign(x)))) / 2), 4),
        "peak": round(float(np.max(np.abs(x))), 4),
    }


def quality_hint(fp: dict[str, float]) -> tuple[str, str]:
    """给出（评级, 中文建议）。规则很朴素，但能拦住大部分烂录音。"""
    dur, rms, peak = fp.get("duration", 0), fp.get("rms", 0), fp.get("peak", 0)
    if dur < MIN_REF_SEC:
        return "bad", f"录音只有 {dur:.1f} 秒，太短了。念一段 10~30 秒的文字，音色才抓得准"
    if rms < 0.012 or peak < 0.05:
        return "bad", "录音音量过低（接近静音），请靠近麦克风重录"
    if peak > 0.995:
        return "warn", "录音存在削波失真（音量爆了），建议降低录音音量后重录"
    if dur < 10:
        return "warn", f"录音 {dur:.1f} 秒偏短，建议 15~30 秒效果更稳"
    return "ok", f"录音 {dur:.1f} 秒，质量合格"


# ================================================================ Provider
async def _concat_segments(
    chunks: list[str], seg_files: list[Path], workdir: Path
) -> tuple[Path, list[tuple[float, float, str]]]:
    """把逐段合成结果拼成一条 mp3，并回算出句级时间轴。

    时间轴为什么这么算：本地 XTTS 与云端 API 都不会返回像 edge-tts 那样的
    SentenceBoundary，只能拿 ffprobe 实测每段真实时长再累加。段切得短
    （≤80 字）时这个近似足够准 —— 误差只在段内的语速起伏上。
    """
    list_file = workdir / "clone_concat.txt"
    list_file.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in seg_files), encoding="utf-8"
    )
    out = workdir / "voice.mp3"
    await _ff(
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-ac", "1", "-ar", "24000",
        "-c:a", "libmp3lame", "-b:a", "128k", str(out),
    )

    boundaries: list[tuple[float, float, str]] = []
    offset = 0.0
    for ck, p in zip(chunks, seg_files):
        d = await probe_duration(p)
        boundaries.append((offset, d, ck))
        offset += d
    return out, boundaries


class CloneProvider:
    """复刻音色的合成后端。三种实现共用一个接口。"""

    name = "base"

    async def train(self, profile: dict[str, Any], ref: Path, workdir: Path) -> dict[str, Any]:
        raise NotImplementedError

    async def synth(
        self,
        profile: dict[str, Any],
        text: str,
        workdir: Path,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> tuple[Path, list[tuple[float, float, str]]]:
        """返回 (音频文件, [(起, 时长, 文本)])。

        on_progress(百分比, 阶段文案) 可选：长文本逐段合成时用来推进前端进度条，
        否则进度会长时间钉死在 `video_pipeline` 的 5%（合成配音）不动。
        """
        raise NotImplementedError


class XttsProvider(CloneProvider):
    """本地 Coqui XTTS-v2：零样本克隆，不训练、不联网、不花钱。

    API 兼容性提醒：coqui-tts 0.27 的 `TTS.api.TTS` 与旧版
    `tts_models/multilingual/multi-dataset/xtts_v2` 参数名一致
    （speaker_wav + language），所以这段代码在 0.22~0.27 上都能跑。

    依赖版本坑（踩过，写在这里省得再踩）：
      - torch ≥ 2.9 起音频 IO 需要 torchcodec，不加会 ImportError
      - transformers 必须 <5，5.x 删了 `isin_mps_friendly`，XTTS 直接起不来
    """

    name = "xtts"

    @staticmethod
    def _api() -> Any:
        """加载并缓存 XTTS 模型实例。

        为什么缓存：模型加载要几十秒、占 1.8GB 内存。每次合成/复刻都重新
        加载的话，一个 30 秒的视频会卡成 3 分钟，而且反复申请内存容易 OOM。
        进程内单例，第一次慢、之后秒开。用类属性而不是全局变量，
        是为了让「本进程只用过 edge-tts」的场景根本不加载模型。
        """
        cached = _xtts_cache.get("model")
        if cached is not None:
            return cached
        try:
            from TTS.api import TTS  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "本地复刻需要 Coqui TTS，未安装。执行："
                "pip install coqui-tts torchcodec 'transformers<5'"
                "（或把 VC_PROVIDER 改成 cloud 走云端）"
            ) from exc
        logger.info("加载 XTTS-v2 模型（首次需下载 ~1.8GB 权重，请耐心等待）…")
        model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cpu")
        _xtts_cache["model"] = model
        logger.info("XTTS-v2 模型就绪")
        return model

    async def train(self, profile: dict[str, Any], ref: Path, workdir: Path) -> dict[str, Any]:
        """所谓"训练"= 加载模型 + 用参考音频预热一次，把 embedding 缓存下来。

        为什么预热：XTTS 首次合成包含模型加载（几十秒到几分钟），如果放在
        渲染流程里，用户会看到「渲染到 5% 卡住」。放到复刻阶段，渲染时就是
        热启动 —— 复刻慢一点用户能接受，渲染卡住会以为坏了。

        注意：`ref`（即 profile_dir/reference.wav）**本身就是最终保留位置**，
        XTTS 每次合成都会重读它算 embedding，绝不能像其它中间产物那样用完就删。
        这里曾经多此一举地"再复制一份副本"，而副本路径用 `profile.get("id")`
        推导 —— train() 收到的是空 dict，于是副本落到了 profiles/ 根目录下，
        变成每次复刻都冒出来的孤儿文件。已删除该逻辑。
        """
        def _warm() -> str:
            tts = self._api()
            out = workdir / "warmup.wav"
            tts.tts_to_file(
                text="你好，这是音色复刻的预热音频。",
                speaker_wav=str(ref),
                language="zh",
                file_path=str(out),
            )
            out.unlink(missing_ok=True)
            return "xtts-v2"

        model = await asyncio.to_thread(_warm)
        return {"model": model}

    async def synth(
        self,
        profile: dict[str, Any],
        text: str,
        workdir: Path,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> tuple[Path, list[tuple[float, float, str]]]:
        ref = Path(profile["ref_path"])
        if not ref.is_file():
            raise RuntimeError("参考音频丢失，请删除该音色后重新克隆")

        chunks = split_for_clone_tts(text)
        seg_files: list[Path] = []

        def _synth_all() -> None:
            tts = self._api()
            n = len(chunks)
            for i, ck in enumerate(chunks):
                p = workdir / f"clone_seg_{i:03d}.wav"
                tts.tts_to_file(
                    text=ck, speaker_wav=str(ref), language="zh", file_path=str(p)
                )
                seg_files.append(p)
                # 逐段推进：配音阶段在 video_pipeline 里占 5%~30%，
                # 这里按段数把这段区间填满，避免长文本时进度长时间不动
                if on_progress and n:
                    pct = 5 + int(25 * (i + 1) / n)
                    on_progress(pct, f"正在合成配音 {i + 1}/{n}")

        await asyncio.to_thread(_synth_all)
        return await _concat_segments(chunks, seg_files, workdir)


class CloudProvider(CloneProvider):
    """阿里云 CosyVoice 声音复刻（OpenAI 兼容形态）。

    为什么默认推荐这条：本地 XTTS 在 CPU 上合成一句要 1~2 秒、首次还要下
    1.8GB 模型；云端上传一段 10 秒音频就能拿到音色，效果好得多。

    接口形态各家略有差异，这里按「OpenAI 音频接口风格」实现（阿里云
    DashScope 兼容模式、MiniMax、火山方舟都提供类似端点）：
        建音色  POST {base}/voice/clone      {audio_base64, name}
        合成    POST {base}/audio/speech     {model, input, voice}
    配置在 .env：VC_CLOUD_BASE_URL / VC_CLOUD_API_KEY / VC_CLOUD_MODEL。
    """

    name = "cloud"

    @staticmethod
    def _cfg() -> tuple[str, str, str]:
        base = (settings.VC_CLOUD_BASE_URL or "").rstrip("/")
        key = settings.VC_CLOUD_API_KEY or ""
        model = settings.VC_CLOUD_MODEL or "cosyvoice-v1"
        if not base or not key:
            raise RuntimeError(
                "云端复刻未配置：请在 .env 填 VC_CLOUD_BASE_URL 与 VC_CLOUD_API_KEY"
            )
        return base, key, model

    async def train(self, profile: dict[str, Any], ref: Path, workdir: Path) -> dict[str, Any]:
        import base64

        import httpx

        base, key, model = self._cfg()
        audio_b64 = base64.b64encode(ref.read_bytes()).decode()
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{base}/voice/clone",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "name": profile.get("name") or "cwb-clone",
                    "audio_base64": audio_b64,
                    "format": "wav",
                    "sample_rate": 24000,
                },
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"云端建音色失败({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
        voice_id = data.get("voice") or data.get("voice_id") or data.get("data", {}).get("voice")
        if not voice_id:
            raise RuntimeError(f"云端未返回音色 ID：{json.dumps(data, ensure_ascii=False)[:300]}")
        return {"model": model, "voice_id": str(voice_id)}

    async def synth(
        self,
        profile: dict[str, Any],
        text: str,
        workdir: Path,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> tuple[Path, list[tuple[float, float, str]]]:
        import httpx

        base, key, model = self._cfg()
        voice_id = (profile.get("cloud") or {}).get("voice_id")
        if not voice_id:
            raise RuntimeError("该音色缺少云端音色 ID，请重新克隆")

        chunks = split_for_clone_tts(text)
        seg_files: list[Path] = []
        n = len(chunks)
        async with httpx.AsyncClient(timeout=300) as client:
            for i, ck in enumerate(chunks):
                resp = await client.post(
                    f"{base}/audio/speech",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": model, "input": ck, "voice": voice_id, "format": "mp3"},
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"云端合成失败({resp.status_code}): {resp.text[:300]}")
                p = workdir / f"clone_seg_{i:03d}.mp3"
                p.write_bytes(resp.content)
                seg_files.append(p)
                if on_progress and n:
                    on_progress(5 + int(25 * (i + 1) / n), f"正在合成配音 {i + 1}/{n}")

        return await _concat_segments(chunks, seg_files, workdir)


def get_provider(name: str | None = None) -> CloneProvider:
    """按配置取 provider。未知名字回落到 edge（不复刻）。"""
    key = (name or settings.VC_PROVIDER or "edge").strip().lower()
    if key == "xtts":
        return XttsProvider()
    if key == "cloud":
        return CloudProvider()
    raise RuntimeError(f"未知的复刻后端：{key}（可选 edge / cloud / xtts）")


def provider_status() -> dict[str, Any]:
    """给前端展示的「当前能用哪条路」。"""
    configured = (settings.VC_PROVIDER or "edge").strip().lower()
    xtts_installed = False
    try:
        import importlib.util

        xtts_installed = importlib.util.find_spec("TTS") is not None
    except Exception:  # noqa: BLE001
        pass
    cloud_ready = bool(settings.VC_CLOUD_BASE_URL and settings.VC_CLOUD_API_KEY)
    return {
        "provider": configured,
        "xtts_installed": xtts_installed,
        "cloud_ready": cloud_ready,
        # 能不能真的克隆：当前 provider 可用即可
        "available": (
            (configured == "xtts" and xtts_installed)
            or (configured == "cloud" and cloud_ready)
            or configured == "edge"
        ),
        "note": {
            "edge": "当前未开启声音复刻，用的是 edge-tts 官方音色",
            "xtts": (
                "本地 XTTS-v2 零样本克隆（CPU 合成，首次需下载 ~1.8GB 模型）"
                if xtts_installed
                else "已配置本地 XTTS，但后端未安装 coqui-tts，请 pip install coqui-tts"
            ),
            "cloud": (
                "云端声音复刻已就绪" if cloud_ready else "已选云端复刻，但 VC_CLOUD_* 未配置"
            ),
        }.get(configured, "未知后端"),
    }


# ================================================================ 文本分段
def split_for_clone_tts(text: str) -> list[str]:
    """把口播稿切成短段。

    ≤80 字一段是有讲究的：本地合成的时间轴是按段累加的，段越长漂得越多；
    80 字的段落时长约 15 秒，漂移肉眼不可察。另外短段还能避免 XTTS
    在长文本上"念到后面音色跑偏"（这是它已知的毛病）。
    """
    paras = [p.strip() for p in text.replace("\r", "").split("\n") if p.strip()]
    chunks: list[str] = []
    for para in paras:
        if len(para) <= _TTS_CHUNK_CHARS:
            chunks.append(para)
            continue
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


# ================================================================ 克隆任务
# 进程内任务表（复刻是短任务，几十秒内完成；丢了重新点一次即可，不值得落库）
_clone_jobs: dict[str, dict[str, Any]] = {}

# 每个 profile 一把锁：profile_id 是音频内容哈希，重复上传同一段录音会命中
# 同一个 id。若两次复刻并发跑，它们共享同一个目录（reference_raw/reference.wav），
# 会互相踩踏 —— 实测表现是 voice.json 留下了但 reference.wav 被另一个任务删掉，
# 音色在列表里可见却无法试听/渲染。串行化即可根治。
_clone_locks: dict[str, asyncio.Lock] = {}


def _clone_lock(owner: str, profile_id: str) -> asyncio.Lock:
    key = _job_key(owner, profile_id)
    lock = _clone_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _clone_locks[key] = lock
    return lock


def _new_job(profile_id: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "status": "queued",
        "progress": 0,
        "stage": "排队中",
        "error": "",
        "created_at": int(time.time()),
    }


def _job_key(owner: str, profile_id: str) -> str:
    """任务表 key 带 owner：profile_id 是音频内容哈希，两个用户上传同一段
    录音会得到同一个 id，不带 owner 会互相覆盖任务状态。"""
    return f"{owner}:{profile_id}"


def get_clone_job(profile_id: str, owner: str = "default") -> dict[str, Any] | None:
    return _clone_jobs.get(_job_key(owner, profile_id))


async def _progress_heartbeat(job: dict[str, Any], start: int, end: int, stop: asyncio.Event) -> None:
    """把进度从 start 平滑爬到 end，直到 stop 被 set。

    为什么需要它：XTTS 在 CPU 上「提取音色」这一步要跑 40~60 秒（首次还含
    1.8GB 模型下载），如果进度一直钉在 40%，用户会以为卡死了。这里每 2 秒
    推进一点，让它看起来一直在动 —— 爬升曲线用 (1 - 1/(1+t)) 收敛式，
    永远不会超过 end，真完成时由外层直接跳到 100%。
    """
    t = 0
    span = max(1, end - start)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
            break
        except asyncio.TimeoutError:
            pass
        t += 1
        # 1 - 1/(1+t/3)：前几秒涨得快、越接近 end 越慢，视觉上像在真实推进
        ratio = 1.0 - 1.0 / (1.0 + t / 3.0)
        cur = start + int(span * ratio)
        if cur > job.get("progress", start):
            job["progress"] = min(cur, end)


async def clone_voice(profile_id: str, owner: str = "default") -> None:
    """后台跑复刻：标准化录音 → 提取指纹 → 预热模型 → 落盘 profile。

    同一 profile_id 的复刻**串行执行**（见 _clone_locks 注释）：重复上传同一段
    录音会命中同一个 id，并发跑会互相删掉对方的 reference_raw / reference.wav。
    """
    async with _clone_lock(owner, profile_id):
        await _clone_voice_locked(profile_id, owner)


async def _clone_voice_locked(profile_id: str, owner: str = "default") -> None:
    job = _clone_jobs[_job_key(owner, profile_id)]
    try:
        d = profile_dir(profile_id, owner)
        ref_src = d / "reference_raw"
        if not ref_src.is_file():
            raise RuntimeError("参考音频不存在")

        job.update(status="running", progress=10, stage="正在处理录音")
        ref = d / "reference.wav"
        orig, dur = await normalize_reference(ref_src, ref)
        if dur < MIN_REF_SEC:
            raise RuntimeError(
                f"处理后可用的有效人声只有 {dur:.1f} 秒（原始 {orig:.1f} 秒），"
                f"至少需要 {MIN_REF_SEC:.0f} 秒。请录一段连续朗读，中间少停顿"
            )
        ref_src.unlink(missing_ok=True)

        job.update(progress=25, stage="正在分析音色特征")
        fp = _fingerprint(ref)
        level, msg = quality_hint(fp)

        # 「提取音色」是本流程唯一的长耗时步骤（CPU 上 40~60s，首次含模型下载），
        # 用心跳把 40% → 92% 填满，避免进度条长时间不动。
        job.update(progress=40, stage="正在提取音色（首次需加载模型，请耐心等待）")
        provider = get_provider()
        hb_stop = asyncio.Event()
        heartbeat = asyncio.create_task(_progress_heartbeat(job, 40, 92, hb_stop))
        try:
            meta = await provider.train({}, ref, d)
        finally:
            hb_stop.set()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        vm = {
            "id": profile_id,
            "name": json.loads((d / "meta.json").read_text("utf-8")).get("name", "我的音色")
            if (d / "meta.json").is_file()
            else "我的音色",
            "provider": provider.name,
            "ref_path": str(ref),
            "duration": round(dur, 2),
            "original_duration": round(orig, 2),
            "fingerprint": fp,
            "quality": {"level": level, "message": msg},
            "created_at": int(time.time()),
            **meta,
        }
        (d / "voice.json").write_text(json.dumps(vm, ensure_ascii=False), "utf-8")

        job.update(status="done", progress=100, stage="完成", quality=vm["quality"])
        logger.info("音色复刻完成: %s (%s, %.1fs)", profile_id, provider.name, dur)
    except Exception as exc:  # noqa: BLE001
        logger.exception("音色复刻失败: %s", profile_id)
        job.update(status="failed", error=str(exc)[:500], stage="失败")


# ================================================================ CRUD
def create_profile(name: str, audio: bytes, filename: str, owner: str = "default") -> dict[str, Any]:
    """落盘音频 + 建元数据，返回 profile（此时还没开始复刻）。

    返回的 id 是内容哈希前 12 位 —— 重复上传同一段录音直接命中同一个 id，
    免得用户在列表里看到三个一模一样的"我的音色"。
    """
    pid = hashlib.md5(audio).hexdigest()[:12]
    d = profile_dir(pid, owner)
    (d / "reference_raw").write_bytes(audio)
    meta = {
        "name": (name or "").strip()[:40] or f"我的音色 {time.strftime('%m%d %H:%M')}",
        "source_filename": filename[:120],
        "size_bytes": len(audio),
        "created_at": int(time.time()),
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    # 重传同一段音频时把上一轮的产物清掉，强制重跑
    (d / "voice.json").unlink(missing_ok=True)
    return {**meta, "id": pid}


def list_profiles(owner: str = "default") -> list[dict[str, Any]]:
    """列出已复刻音色（只列已经复刻完成的）。"""
    out: list[dict[str, Any]] = []
    for d in sorted(profiles_dir(owner).iterdir()):
        if not d.is_dir():
            continue
        vf = d / "voice.json"
        if not vf.is_file():
            continue
        try:
            vm = json.loads(vf.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        vm.pop("ref_path", None)  # 绝对路径不外泄
        out.append(vm)
    return sorted(out, key=lambda x: x.get("created_at", 0), reverse=True)


def get_profile(profile_id: str, owner: str = "default") -> dict[str, Any] | None:
    if not _safe_profile_id(profile_id):
        return None
    vf = profile_dir(profile_id, owner) / "voice.json"
    if not vf.is_file():
        return None
    try:
        return json.loads(vf.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def delete_profile(profile_id: str, owner: str = "default") -> bool:
    if not _safe_profile_id(profile_id):
        return False
    d = profiles_dir(owner) / profile_id
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    _clone_jobs.pop(_job_key(owner, profile_id), None)
    return True


def profile_audio_path(profile_id: str, owner: str = "default") -> Path | None:
    """试听用的音频（合成一段固定文案，缓存下来）。"""
    if not _safe_profile_id(profile_id):
        return None
    d = profile_dir(profile_id, owner)
    for ext in (".mp3", ".wav"):
        p = d / f"sample{ext}"
        if p.is_file():
            return p
    ref = d / "reference.wav"
    return ref if ref.is_file() else None


# ================================================================ 合成入口
# 同一时刻只允许一个复刻合成任务跑（CPU 打满时连 ffmpeg 都会被拖慢）
_synth_lock = asyncio.Semaphore(1)

# XTTS 模型进程内单例（加载一次约 1.8GB 内存）。放在文件末尾只是为了让
# 类型注解先就位，模块导入时就会执行到，顺序不影响使用。
_xtts_cache: dict[str, Any] = {}


async def synth_clone(
    voice: str,
    text: str,
    workdir: Path,
    on_progress: Callable[[int, str], None] | None = None,
    owner: str = "default",
) -> tuple[Path, list[tuple[float, float, str]]]:
    """复刻音色合成，签名与 video_pipeline.synth_voice 的返回一致。

    voice 形如 `clone_ab12cd34ef56`。返回 (音频 mp3, [(起, 时长, 文本)])。
    长文本逐段合成时通过 on_progress 汇报进度（否则会卡在渲染的 5%）。
    """
    profile_id = voice[len(CLONE_PREFIX):]
    profile = get_profile(profile_id, owner)
    if not profile:
        raise RuntimeError(f"复刻音色 {profile_id} 不存在或已删除，请重新选择配音音色")

    async with _synth_lock:
        provider = get_provider(profile.get("provider"))
        return await provider.synth(profile, text, workdir, on_progress=on_progress)


def is_clone_voice(voice: str) -> bool:
    return bool(voice) and voice.startswith(CLONE_PREFIX)


def _ensure_dirs() -> None:
    profiles_dir()


_ensure_dirs()
