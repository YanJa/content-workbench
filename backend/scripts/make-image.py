#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
码上聊聊 · 文内配图（文字 / 图标类）生成器
------------------------------------------------------------------
不同于封面图（标题为主），文内配图走「概念插图」路线：
根据提示词里的关键词映射出一个 emoji / 图标，再配一层由提示词派生出的
稳定渐变底色，渲染成一张干净的概念卡片。确定性、零外部依赖（只需本机
Chrome 截图），适合做公众号文章里的段落插图 / 分隔图。

它是 IMAGE_SCRIPT 的默认实现，与后端 imagegen._run_script 的契约对齐：
  python make-image.py --prompt "<提示词>" --size 1024x1024 --count 1 --out <目录>
  - 退出码 0 表示成功；
  - 生成的图片直接写入 --out 目录（文件名任意，png/jpg/webp）；
  - 同时把每个文件路径按行打到 stdout（兼容后端「不认 --out」的兜底读取）。

可选参数（不破坏契约）：
  --style   vibrant(鲜艳,默认) / soft(柔和) / dark(深色)
  --palette 排版主题调色板 JSON，如 '{"main":"#059669","warm":"#34D399"}'
           传入时用其主色派生渐变，使配图与当篇排版主题同源。
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

# 脚本自身所在目录（跨平台）
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")


def _find_chrome() -> str:
    """探测可用的 Chrome/Chromium（顺序：环境变量 → 常见路径 → PATH）。"""
    env = os.environ.get("IMAGE_CHROME") or os.environ.get("COVER_CHROME")
    if env and os.path.exists(env):
        return env
    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


CHROME = _find_chrome()

# ---------- 关键词 → 图标（emoji）映射 ----------
# 命中顺序即匹配顺序；命中多个时取第一个。覆盖常见内容主题。
KW_EMOJI = [
    ("咖啡", "☕"), ("茶", "🍵"), ("奶茶", "🥤"), ("美食", "🍜"), ("早餐", "🥐"),
    ("笔记", "📝"), ("写", "✍️"), ("书", "📚"), ("阅读", "📖"), ("学习", "🎓"),
    ("知识", "🧠"), ("思考", "💡"), ("想法", "💭"), ("灵感", "✨"), ("创意", "🎨"),
    ("电脑", "💻"), ("笔记本", "💻"), ("手机", "📱"), ("代码", "💻"), ("程序", "🧩"),
    ("数据", "📊"), ("图表", "📈"), ("统计", "📉"), ("报表", "📊"), ("增长", "🚀"),
    ("时间", "⏰"), ("效率", "⚡"), ("计划", "🗓️"), ("目标", "🎯"), ("待办", "✅"),
    ("团队", "👥"), ("协作", "🤝"), ("会议", "💬"), ("沟通", "📣"), ("客户", "🤝"),
    ("钱", "💰"), ("理财", "💹"), ("省钱", "🪙"), ("生意", "🏪"), ("创业", "🚀"),
    ("健康", "🍎"), ("运动", "🏃"), ("睡眠", "😴"), ("健身", "💪"), ("养生", "🌿"),
    ("旅行", "✈️"), ("风景", "🏞️"), ("城市", "🏙️"), ("家", "🏠"), ("自然", "🌿"),
    ("爱", "❤️"), ("朋友", "👫"), ("家庭", "👪"), ("礼物", "🎁"), ("节日", "🎉"),
    ("天气", "🌤️"), ("雨", "🌧️"), ("太阳", "☀️"), ("月亮", "🌙"), ("雪", "❄️"),
    ("音乐", "🎵"), ("电影", "🎬"), ("游戏", "🎮"), ("相机", "📷"), ("艺术", "🖼️"),
    ("问题", "❓"), ("注意", "⚠️"), ("重要", "⭐"), ("新", "🆕"), ("免费", "🎁"),
    ("AI", "🤖"), ("机器人", "🤖"), ("智能", "🤖"), ("工具", "🛠️"), ("技巧", "🔧"),
    ("安全", "🔒"), ("隐私", "🛡️"), ("网络", "🌐"), ("云", "☁️"), ("服务器", "🖥️"),
    ("成功", "🏆"), ("胜利", "🥇"), ("奖", "🏅"), ("点赞", "👍"), ("火", "🔥"),
]
# 没命中关键词时的兜底图标池（按 hash 轮转）
FALLBACK_EMOJI = ["🎨", "💡", "✨", "📌", "🌟", "🧩", "🔖", "📎"]


def pick_emoji(prompt: str, offset: int = 0) -> str:
    for kw, em in KW_EMOJI:
        if kw.lower() in prompt.lower():
            return em
    h = int(hashlib.md5(prompt.encode()).hexdigest(), 16)
    return FALLBACK_EMOJI[(h + offset) % len(FALLBACK_EMOJI)]


def _hash_hue(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest(), 16) % 360


def _palette_hue(palette: dict | None) -> int | None:
    """从调色板主色取色相；拿不到返回 None。"""
    if not palette:
        return None
    main = palette.get("main") or palette.get("gradient") or ""
    m = re.search(r"#([0-9a-fA-F]{6})", main)
    if not m:
        return None
    h = m.group(1)
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    # RGB → H（简化，够用于配色派生）
    rn, gn, bn = r / 255, g / 255, b / 255
    mx, mn = max(rn, gn, bn), min(rn, gn, bn)
    d = mx - mn
    if d == 0:
        return 0
    if mx == rn:
        hh = ((gn - bn) / d) % 6
    elif mx == gn:
        hh = (bn - rn) / d + 2
    else:
        hh = (rn - gn) / d + 4
    return int(hh * 60)


def build_html(prompt: str, size: tuple[int, int], style: str, hue: int) -> str:
    w, h = size
    # 三种风格决定渐变 / 文字色
    if style == "dark":
        c1 = f"hsl({hue} 45% 32%)"
        c2 = f"hsl({(hue + 30) % 360} 50% 22%)"
        text = "#FFFFFF"
        sub = "rgba(255,255,255,0.82)"
        card = "rgba(255,255,255,0.12)"
    elif style == "soft":
        c1 = f"hsl({hue} 80% 86%)"
        c2 = f"hsl({(hue + 35) % 360} 75% 78%)"
        text = "#2A2D36"
        sub = "rgba(42,45,54,0.72)"
        card = "rgba(255,255,255,0.55)"
    else:  # vibrant
        c1 = f"hsl({hue} 72% 58%)"
        c2 = f"hsl({(hue + 35) % 360} 76% 48%)"
        text = "#FFFFFF"
        sub = "rgba(255,255,255,0.90)"
        card = "rgba(255,255,255,0.16)"

    emoji = pick_emoji(prompt)
    # 标题：用提示词做说明，最多保留 24 字，过长截断
    cap = prompt.strip().replace("\n", " ")
    if len(cap) > 24:
        cap = cap[:23] + "…"
    sub_text = "文内配图"  # 卡片副标，标明这是插图

    pad = round(w * 0.08)
    emo_size = round(h * 0.30)
    title_fs = round(w * 0.052)
    sub_fs = round(w * 0.026)

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:{w}px; height:{h}px; overflow:hidden; background:{c2}; }}
  /* 渐变放在 fixed 外扩层上：headless 新模式下根元素渐变不会传播到画布，
     视口下方会留白；fixed+inset 外扩可保证整张截图都被铺满 */
  .bg {{ position:fixed; inset:-120px; z-index:0;
         background:linear-gradient(135deg, {c1} 0%, {c2} 100%); }}
  body {{ font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",
         "Noto Sans","Noto Color Emoji",sans-serif;
         -webkit-font-smoothing:antialiased; }}
  .wrap {{ position:relative; z-index:1; width:{w}px; height:{h}px; overflow:hidden; }}
  .glow {{ position:absolute; width:{round(w*0.7)}px; height:{round(w*0.7)}px;
           left:{round(w*0.15)}px; top:{round(h*0.10)}px; border-radius:50%;
           background:radial-gradient(circle, rgba(255,255,255,0.35) 0%, rgba(255,255,255,0) 70%); }}
  .emoji {{ position:absolute; left:0; right:0; top:{round(h*0.22)}px;
            text-align:center; font-size:{emo_size}px; line-height:1;
            filter:drop-shadow(0 6px 18px rgba(0,0,0,0.18)); }}
  .card {{ position:absolute; left:{pad}px; right:{pad}px; bottom:{round(h*0.16)}px;
           background:{card}; backdrop-filter:blur(6px); border-radius:{round(h*0.04)}px;
           padding:{round(h*0.05)}px {round(w*0.05)}px; text-align:center;
           box-shadow:0 8px 30px rgba(0,0,0,0.12); }}
  .title {{ font-size:{title_fs}px; font-weight:800; color:{text};
            line-height:1.32; letter-spacing:.3px; word-break:break-word;
            text-shadow:0 1px 8px rgba(0,0,0,0.10); }}
  .sub {{ margin-top:{round(h*0.02)}px; font-size:{sub_fs}px; color:{sub}; font-weight:600;
          letter-spacing:2px; }}
</style></head><body>
<div class="bg"></div>
<div class="wrap">
  <div class="glow"></div>
  <div class="emoji">{emoji}</div>
  <div class="card">
    <div class="title">{cap}</div>
    <div class="sub">{sub_text}</div>
  </div>
</div>
</body></html>'''


def render(html: str, out_path: str, w: int, h: int) -> str:
    if not CHROME:
        raise RuntimeError(
            "找不到 Chrome/Chromium（文内配图靠无头浏览器截图生成）。"
            "请安装 Chrome，或设环境变量 IMAGE_CHROME 指向其可执行文件。"
        )
    tmp = os.path.join(BASE, "_tmp_image.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    # 窗口高度多给 200px：headless 新模式的视口比窗口矮约 88px，
    # 内容若按窗口高度铺会底部缺一截；多给缓冲保证视口 >= 目标高度，
    # 截图后再裁掉多余部分（PIL）。
    EXTRA = 200
    proc = subprocess.run(
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=1",
            f"--window-size={w},{h + EXTRA}", f"--screenshot={out_path}", tmp,
        ],
        capture_output=True,
    )
    if os.path.exists(tmp):
        os.remove(tmp)
    if not os.path.exists(out_path):
        err = (proc.stderr or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError(f"配图渲染失败（Chrome 退出码 {proc.returncode}）：{err}")
    _crop_to(out_path, w, h)
    return out_path


def _crop_to(path: str, w: int, h: int) -> None:
    """把截图裁剪到精确的 w×h（去掉窗口缓冲产生的多余高度）。"""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("需要 Pillow 库做尺寸裁剪：pip install pillow")
    im = Image.open(path)
    if im.size != (w, h):
        im = im.crop((0, 0, w, h))
        im.save(path)


def make_images(prompt: str, size: tuple[int, int], count: int, out_dir: str,
                style: str = "vibrant", palette: dict | None = None) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    base_hue = _palette_hue(palette)
    if base_hue is None:
        base_hue = _hash_hue(prompt + style)  # 同 prompt+风格 → 同色（稳定）
    paths: list[str] = []
    for i in range(max(1, count)):
        hue = (base_hue + i * 23) % 360
        # 多张时轮转兜底图标，命中关键词的图保持主图标
        html = build_html(prompt, size, style, hue)
        name = f"illu-{i}-{int(hashlib.md5((prompt + str(i)).encode()).hexdigest(), 16) % 10**10:010d}.png"
        path = os.path.join(out_dir, name)
        render(html, path, size[0], size[1])
        paths.append(path)
        print(path)  # 同时打到 stdout（后端兜底读取）
    return paths


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="文内配图（文字/图标类）生成器")
    ap.add_argument("--prompt", required=True, help="配图描述")
    ap.add_argument("--size", default="1024x1024", help="输出尺寸 WxH，默认 1024x1024")
    ap.add_argument("--count", type=int, default=1, help="生成张数，默认 1")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--style", default="vibrant", choices=["vibrant", "soft", "dark"])
    ap.add_argument("--palette", default=None, help='主题调色板 JSON，如 \'{"main":"#059669"}\'')
    a = ap.parse_args()

    if "x" in a.size:
        try:
            w, h = (int(x) for x in a.size.lower().split("x"))
        except ValueError:
            w, h = 1024, 1024
    else:
        w, h = 1024, 1024

    palette_arg = None
    if a.palette:
        import json as _json
        try:
            palette_arg = _json.loads(a.palette)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"--palette 不是合法 JSON：{exc}")

    try:
        paths = make_images(a.prompt, (w, h), a.count, a.out, a.style, palette_arg)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"生成失败：{exc}")
    print(f"DONE {len(paths)} 张", file=sys.stderr)
    sys.exit(0)
