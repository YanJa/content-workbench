"""services 层包入口。

这里**只做一件事**：在最顶部装上音频 IO 兼容垫片。

torchaudio ≥ 2.9 把音频读写委托给 torchcodec，而 torchcodec 的原生库
与本环境的 libtorch ABI 对不上（`undefined symbol: torch_list_size`），
会导致 XTTS-v2 一加载参考音频就崩。垫片在运行时把 `torchaudio.load/save/info`
换成 soundfile 实现，细节见 `app/core/audio_compat.py` 的模块 docstring。

放在包入口而不是 `voices.py` 里，是为了不依赖任何具体模块的 import 顺序：
只要 `app.services` 下任何一个子模块先被导入，补丁就一定在 TTS 之前装好。
"""

from app.core import audio_compat as _audio_compat  # noqa: F401
