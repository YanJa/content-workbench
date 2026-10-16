"""音频 IO 兼容垫片（在 `app` 包被导入时自动生效）。

要解决的真实问题
----------------
torchaudio ≥ 2.9 把 `torchaudio.load/save` 的实现换成了 torchcodec。
torchcodec 的原生库（libtorchcodec_core*.so / libtorchcodec_image.so）
是按特定 libtorch 版本编译的，本项目环境里 ABI 对不上，报的错是
`undefined symbol: torch_list_size` —— 这个符号在 torch 2.9.1/2.10 的
libtorch 里都不存在，属于 torchcodec 自身的发布节奏问题，不是我们配错。

于是 TTS（XTTS-v2）一加载参考音频就炸，声音复刻整条路走不通。

为什么不直接改 TTS 源码
-----------------------
试过：把 `torchaudio.load` 换成 soundfile。能用，但 TTS 有 5 个文件都在
import torchaudio，逐个改包文件既脆弱（`pip install -U` 就把补丁冲掉）
又容易改坏。改成**运行时打补丁**：只在 torchaudio 模块对象上替换这两个
函数，源码一行不动，卸载重装也自动恢复。

为什么 soundfile 是等价替代
--------------------------
`torchaudio.load` 的契约是「返回 (channels×samples 的 float32 张量,
采样率)」，soundfile 读出来是 (samples×channels) 的 numpy，转置即可。
XTTS 只用到这两个函数，语义完全对得上。

这个模块必须在任何 TTS / torchaudio 相关 import 之前执行，
所以放在 `app/services/__init__.py` 顶部导入。
"""
from __future__ import annotations


def _patch() -> bool:
    try:
        import numpy as np
        import torch
        import torchaudio
    except Exception:  # noqa: BLE001
        return False

    def _load(uri, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        import soundfile as sf

        frames = kwargs.get("num_frames", -1)
        start = kwargs.get("frame_offset", 0)
        data, sr = sf.read(
            uri, dtype="float32", always_2d=True, frames=frames, start=start
        )
        # soundfile → (samples, channels)；torchaudio 约定 (channels, samples)
        return torch.from_numpy(np.ascontiguousarray(data.T)), sr

    def _save(uri, src, sample_rate, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        import soundfile as sf

        x = src.detach().cpu().numpy() if hasattr(src, "detach") else np.asarray(src)
        if x.ndim == 1:
            x = x[None, :]
        sf.write(uri, np.ascontiguousarray(x.T), int(sample_rate))

    def _info(uri, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        import soundfile as sf

        i = sf.info(uri)
        return {
            "sample_rate": i.samplerate,
            "num_frames": i.frames,
            "num_channels": i.channels,
            "bits_per_sample": 16,
            "encoding": i.subtype,
        }

    torchaudio.load = _load
    torchaudio.save = _save
    torchaudio.info = _info
    return True


PATCHED = _patch()
