#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
码上聊聊 · 公众号封面图生成器
统一品牌主色 #FF8A3D → #FF3B30

用法示例：
  python make-cover.py --title "主标题" --sub "副标题" --tag "AI 工具 · 第 12 期" --style gradient
  python make-cover.py --title "标题" --style dark --size square
  python make-cover.py --title "标题" --sub "副标题" --style light --date "2026.10.13"

参数：
  --title   主标题（必填，支持 \n 手动换行）
  --sub     副标题
  --tag     左上角标签，如「第 12 期」「教程」
  --date    日期，显示在标签后
  --style   gradient(渐变) / dark(深色) / light(浅色)  默认 gradient
  --size    wide(900x383 公众号封面) / square(800x800 分享方形)  默认 wide
  --out     输出文件名（默认自动命名）
"""
import subprocess, os, sys, shutil, argparse, datetime, re

# 脚本自身所在目录（跨平台：不再写死 Windows 绝对路径）
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")


def _find_chrome() -> str:
    """探测可用的 Chrome/Chromium 可执行文件。

    顺序：环境变量 COVER_CHROME → 常见绝对路径 → PATH 里的命令名。
    找不到时返回空串，render() 会给出可读报错。
    """
    env = os.environ.get("COVER_CHROME")
    if env and os.path.exists(env):
        return env
    candidates = [
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


CHROME = _find_chrome()

# ---------- 品牌配置 ----------
# 默认品牌（码上聊聊）：未指定排版主题时回落到它，保证行为不变。
# 指定主题后由 --palette 传入该主题的封面配色，覆盖这里的 main/warm/gradient。
BRAND = {
    "name": "码上聊聊",
    "slogan": "有问题，码上聊聊",
    "gradient": "linear-gradient(135deg, #FF8A3D 0%, #FF3B30 100%)",
    "main": "#FF3B30",
    "warm": "#FF8A3D",
}


def _hex_to_rgb(hex_color: str) -> str:
    """#RRGGBB -> 'r, g, b'，用于拼 rgba()。"""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return ", ".join(str(int(h[i : i + 2], 16)) for i in (0, 2, 4))


def _light(hex_color: str) -> str:
    """把主色提亮一档，用作深色风格里的浅色文字/点缀（简单线性提亮）。"""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    r = min(255, r + 60); g = min(255, g + 60); b = min(255, b + 60)
    return f"#{r:02X}{g:02X}{b:02X}"


def build_styles(brand: dict) -> dict:
    """按品牌配色生成三种氛围风格（渐变/深色/浅色）。

    主色 main、辅助 warm、渐变 gradient 全部来自 brand，使封面与排版主题同源。
    """
    main, warm, gradient = brand["main"], brand["warm"], brand["gradient"]
    mr = _hex_to_rgb(main)
    wr = _hex_to_rgb(warm)
    return {
        "gradient": {
            "bg": gradient,
            "overlay": "none",
            "title": "#FFFFFF",
            "sub": "rgba(255,255,255,0.85)",
            "tag_bg": "rgba(255,255,255,0.24)",
            "tag_text": "#FFFFFF",
            "deco": "rgba(255,255,255,0.13)",
            "brand_text": "rgba(255,255,255,0.92)",
            "accent": "#FFFFFF",
            "title_shadow": "0 2px 20px rgba(0,0,0,0.10)",
        },
        "dark": {
            "bg": "#16181D",
            "overlay": (
                f"radial-gradient(120% 140% at 105% -10%, {warm}40 0%, {warm}00 55%), "
                f"radial-gradient(100% 120% at -10% 110%, {main}38 0%, {main}00 60%)"
            ),
            "title": "#FFFFFF",
            "sub": "#8B90A0",
            "tag_bg": f"rgba({mr},0.18)",
            "tag_text": _light(main),
            "deco": f"rgba({wr},0.10)",
            "brand_text": "#FFFFFF",
            "accent": main,
            "title_shadow": "0 2px 24px rgba(0,0,0,0.35)",
        },
        "light": {
            "bg": "#FFFFFF",
            "overlay": f"radial-gradient(90% 120% at 95% 5%, {warm}28 0%, {warm}00 55%)",
            "title": "#1B1D22",
            "sub": "#6E7385",
            "tag_bg": f"rgba({mr},0.10)",
            "tag_text": main,
            "deco": f"rgba({wr},0.16)",
            "brand_text": "#1B1D22",
            "accent": main,
            "title_shadow": "none",
        },
    }


STYLES = build_styles(BRAND)

SIZES = {
    "wide":   (900, 383),   # 公众号封面标准 2.35:1
    "square": (800, 800),   # 分享 / 次条方图
    "og":     (1200, 630),  # 外部分享卡片
}

# ---------- 标题字号自适应 ----------
def pick_font_size(text, w, h, has_sub=False):
    """按「最长行」宽度 + 总高度估算标题字号（换行由最长行决定，不能用平均）"""
    lines = [l for l in text.split("\n") if l.strip()]
    n_lines = len(lines)
    base_w = w * 0.75                      # 保守可用宽度
    # 高度预算：总高 - 底部留白(22%) - 副标题区 - 顶部标签区
    top_reserve = h * 0.20
    bottom_reserve = h * 0.22 + (h * 0.11 if has_sub else 0)
    avail_h = h - top_reserve - bottom_reserve

    def line_w(line, size):
        # 中文全宽；西文/数字/空格按 0.52 估（微软雅黑实测近似值）
        return sum(size if ord(c) > 0x2E80 else size * 0.52 for c in line)

    for size in range(96, 30, -2):   # 硬上限 96px：防止短标题在方图上过大
        max_w = max(line_w(l, size) for l in lines)
        total_h = size * 1.18 * n_lines
        if max_w <= base_w and total_h <= avail_h:
            return size
    return 30

# ---------- 品牌气泡图形 ----------
def bubble_svg(color, opacity=1.0, size=100):
    return (f'<svg viewBox="0 0 400 400" width="{size}" height="{size}" '
            f'style="opacity:{opacity}">'
            f'<path d="M120 96 h160 a42 42 0 0 1 42 42 v72 a42 42 0 0 1 -42 42 h-76 '
            f'l-46 46 v-46 h-38 a42 42 0 0 1 -42 -42 v-72 a42 42 0 0 1 42 -42 z" fill="{color}"/></svg>')

def logo_svg(size=34, brand=BRAND):
    """右下角品牌标识：主色圆底 + 白色气泡（主色随主题变化）"""
    g0, g1 = brand["warm"], brand["main"]
    return f'''<svg viewBox="0 0 400 400" width="{size}" height="{size}">
  <defs><linearGradient id="lg" x1="0%" y1="0%" x2="100%" y2="100%">
    <stop offset="0%" stop-color="{g0}"/><stop offset="100%" stop-color="{g1}"/>
  </linearGradient></defs>
  <circle cx="200" cy="200" r="200" fill="url(#lg)"/>
  <g transform="translate(200,200) scale(0.62) translate(-200,-200)">
    <path d="M120 96 h160 a42 42 0 0 1 42 42 v72 a42 42 0 0 1 -42 42 h-76 l-46 46 v-46 h-38 a42 42 0 0 1 -42 -42 v-72 a42 42 0 0 1 42 -42 z" fill="#FFFFFF"/>
    <rect x="136" y="142" width="128" height="28" rx="11" fill="{g1}" opacity="0.92"/>
    <rect x="136" y="184" width="88" height="28" rx="11" fill="{g1}" opacity="0.92"/>
  </g></svg>'''

# ---------- HTML 生成 ----------
def build_html(title, sub, tag, date, style, w, h, brand=BRAND):
    s = STYLES[style]
    fs = pick_font_size(title, w, h, has_sub=bool(sub))
    sub_fs = max(round(fs * 0.30), 18)
    pad = round(w * 0.062)
    deco_size = round(h * 0.92) if w / h < 2 else round(h * 1.15)

    # 标题手动换行
    title_html = title.replace("\n", "<br>")

    tag_row = ""
    if tag:
        tag_row = f'<div class="tag">{tag}</div><div class="dot1"></div><div class="dot2"></div>'
    if date:
        if not tag_row:
            tag_row = '<div class="dot1"></div><div class="dot2"></div>'
        tag_row += f'<div class="date">{date}</div>'

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:{w}px; height:{h}px; overflow:hidden; background:{s["bg"]}; }}
  body {{ font-family:"Microsoft YaHei","PingFang SC","Hiragino Sans GB",sans-serif;
         -webkit-font-smoothing:antialiased; }}
  .cover {{ position:relative; width:{w}px; height:{h}px; background:{s["bg"]}; overflow:hidden; }}
  .overlay {{ position:absolute; inset:0; background:{s["overlay"]}; }}

  /* 品牌气泡装饰 */
  .deco {{ position:absolute; right:-{round(w*0.06)}px; top:-{round(h*0.16)}px;
           transform:rotate(-8deg); pointer-events:none; }}

  /* 左上标签行 */
  .tagrow {{ position:absolute; left:{pad}px; top:{pad}px; display:flex;
             align-items:center; gap:{round(w*0.014)}px; z-index:4; }}
  .tag {{ display:inline-block; padding:{round(h*0.022)}px {round(w*0.022)}px;
          background:{s["tag_bg"]}; color:{s["tag_text"]};
          font-size:{round(w*0.021)}px; font-weight:700; letter-spacing:.5px;
          border-radius:{round(h*0.05)}px; }}
  .date {{ font-size:{round(w*0.020)}px; color:{s["sub"]}; font-weight:500; }}

  /* 标签旁的品牌点缀：两个小圆点 */
  .dot1 {{ width:{round(w*0.008)}px; height:{round(w*0.008)}px;
           border-radius:50%; background:{s["deco"]}; }}
  .dot2 {{ width:{round(w*0.005)}px; height:{round(w*0.005)}px;
           border-radius:50%; background:{s["deco"]}; }}

  /* 左下标题区 */
  .main {{ position:absolute; left:{pad}px; bottom:{round(h*0.22)}px;
           width:{round(w*0.80)}px; z-index:3; }}
  .title {{ font-size:{fs}px; font-weight:800; color:{s["title"]};
            line-height:1.18; letter-spacing:-.5px; text-shadow:{s["title_shadow"]};
            word-break:break-word; }}
  .sub {{ margin-top:{round(h*0.035)}px; font-size:{sub_fs}px; color:{s["sub"]};
          font-weight:500; line-height:1.5; opacity:.95; }}

  /* 右下品牌 */
  .brand {{ position:absolute; right:{pad}px; bottom:{round(h*0.085)}px;
            display:flex; align-items:center; gap:{round(w*0.012)}px; z-index:3; }}
  .brand span {{ font-size:{round(w*0.022)}px; font-weight:700; color:{s["brand_text"]};
                 letter-spacing:1px; }}

  /* 底部渐变条（深色/浅色风格用，gradient 背景本身即品牌色故省略） */
  .bar {{ position:absolute; left:0; bottom:0; width:100%; height:{round(h*0.018)}px;
          background:{brand["gradient"]}; {"display:none;" if style == "gradient" else ""} }}
</style></head><body>
<div class="cover">
  <div class="overlay"></div>
  <div class="deco">{bubble_svg(s["deco"], 1.0, deco_size)}</div>
  <div class="tagrow">{tag_row}</div>
  <div class="main">
    <div class="title">{title_html}</div>
    {f'<div class="sub">{sub}</div>' if sub else ''}
  </div>
  <div class="brand">{logo_svg(round(h*0.11), brand)}<span>{brand["name"]}</span></div>
  <div class="bar"></div>
</div>
</body></html>'''

# ---------- 渲染 ----------
def render(html, out_path, w, h, scale=2):
    if not CHROME:
        raise RuntimeError(
            "找不到 Chrome/Chromium（封面图靠无头浏览器截图生成）。"
            "请安装 Chrome，或设环境变量 COVER_CHROME 指向其可执行文件。"
        )
    tmp = os.path.join(BASE, "_tmp_cover.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    proc = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                           "--hide-scrollbars",
                           f"--force-device-scale-factor={scale}",
                           f"--window-size={w},{h}",
                           f"--screenshot={out_path}", tmp],
                          capture_output=True)
    if os.path.exists(tmp):
        os.remove(tmp)
    if not os.path.exists(out_path):
        err = (proc.stderr or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError(f"封面渲染失败（Chrome 退出码 {proc.returncode}）：{err}")
    return out_path

def make_cover(title, sub="", tag="", date="", style="gradient", size="wide", out=None, scale=2, brand=None):
    global STYLES
    # 指定主题调色板时，与默认品牌合并：保留名称/口号，颜色用主题配色覆盖
    if brand:
        brand = {**BRAND, **brand}
        STYLES = build_styles(brand)
    if style not in STYLES:
        raise ValueError(f"未知风格 {style}，可选：{list(STYLES)}")
    if size not in SIZES:
        raise ValueError(f"未知尺寸 {size}，可选：{list(SIZES)}")
    w, h = SIZES[size]
    html = build_html(title, sub, tag, date, style, w, h, brand=brand or BRAND)
    today = datetime.datetime.now().strftime("%Y%m%d")
    if out and (out.endswith(("/", os.sep)) or os.path.isdir(out)):
        # 传目录（后端统一传输出目录）：按「风格-尺寸-日期」自动命名落进去
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, f"cover-{style}-{size}-{today}.png")
    elif out:
        path = out if os.path.isabs(out) else os.path.join(OUT, out)
    else:
        slug = re.sub(r"[^\w\u4e00-\u9fff]", "", title)[:12] or "cover"
        path = os.path.join(OUT, f"{BRAND['name']}-{style}-{size}-{slug}-{today}.png")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    render(html, path, w, h, scale)
    print(f"OK {path} ({w}x{h} @{scale}x = {w*scale}x{h*scale})")
    return path

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="公众号封面图生成器（配色跟随排版主题）")
    ap.add_argument("--title", required=True, help="主标题（支持 \\n 换行）")
    ap.add_argument("--sub", default="", help="副标题")
    ap.add_argument("--tag", default="", help="左上角标签")
    ap.add_argument("--date", default="", help="日期")
    ap.add_argument("--style", default="gradient", choices=list(STYLES))
    ap.add_argument("--size", default="wide", choices=list(SIZES))
    ap.add_argument("--out", default=None, help="输出文件名或目录（目录则自动命名）")
    ap.add_argument("--scale", type=int, default=2, help="渲染倍数，默认2")
    ap.add_argument(
        "--palette", default=None,
        help='排版主题调色板 JSON，如 \'{"gradient":"...","main":"#059669","warm":"#34D399"}\'；'
             "不传则用默认品牌色（向后兼容）。",
    )
    a = ap.parse_args()
    brand_arg = None
    if a.palette:
        try:
            import json as _json
            brand_arg = _json.loads(a.palette)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"--palette 不是合法 JSON：{exc}")
    make_cover(
        a.title.replace("\\n", "\n"), a.sub, a.tag, a.date,
        a.style, a.size, a.out, a.scale, brand=brand_arg,
    )
