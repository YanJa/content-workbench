"""AI 生成检测报告单（HTML，A4 打印友好，可存 PDF）。

版式参考腾讯朱雀官方报告单：
  ① 检测结果 —— 三分类环形图（人工特征/疑似AI/AI特征）+ 一句结论横幅
  ② AI 分布图 —— 横向色带，按每段 AIGC 值着色（0~0.5 绿 / 0.5~0.9 黄 / 0.9~1 红），
     段宽 = 该段字符数占全文比例
  ③ 片段解析 —— 序号 / 片段摘要 / 占全文比例 / 字符数 / AIGC 值（值按阈值着色）
  ④ 片段详情 —— 每段原文
本工作台在此基础上追加两块自有内容：
  ⑤ 本地人工痕迹体检（humanize.scan：命中项 + 统计指标 + 怎么改）
  ⑥ 免责声明（官方同款口径 + 本地体检的口径说明）

数据源可以是：纯朱雀（本地体检未跑）、纯本地体检（朱雀未配置/失败）、或两者合并。
报告不落库，随请求即时生成——检测结果是一次性快照，没有"历史报告"的概念。
"""
from __future__ import annotations

import html
import time

from app.services import zhuque as zhuque_svc

_BAND_COLORS = {"human": "#7bd88f", "suspect": "#f6c15a", "ai": "#f27b8e"}
_LABEL_COLORS = {"人工特征": "#7bd88f", "疑似AI": "#f6c15a", "AI特征": "#f27b8e"}


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _fmt_dt(ts: int | None = None) -> str:
    from datetime import datetime, timezone, timedelta

    cst = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(ts or int(time.time()), cst).strftime("%Y/%m/%d %H:%M:%S")


def _donut_svg(human: float, suspect: float, ai: float) -> str:
    """三分类环形图（SVG stroke-dasharray，不用图表库）。"""
    r = 54
    c = 2 * 3.141592653589793 * r
    segs = [("人工特征", human, _BAND_COLORS["human"]), ("疑似AI", suspect, _BAND_COLORS["suspect"]), ("AI特征", ai, _BAND_COLORS["ai"])]
    offset = 0.0
    circles = []
    for name, val, color in segs:
        if val <= 0:
            continue
        dash = max(0.0, val * c)
        circles.append(
            f'<circle cx="70" cy="70" r="{r}" fill="none" stroke="{color}" stroke-width="26" '
            f'stroke-dasharray="{dash:.2f} {c - dash:.2f}" stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 70 70)"><title>{_e(name)} {_fmt_pct(val)}</title></circle>'
        )
        offset += val * c
    return (
        '<svg viewBox="0 0 140 140" width="150" height="150" role="img" aria-label="检测结果环形图">'
        f'<circle cx="70" cy="70" r="{r}" fill="none" stroke="#eee" stroke-width="26"/>'
        + "".join(circles)
        + "</svg>"
    )


def _band_html(segments: list[dict], total_chars: int) -> str:
    """AI 分布图：每段一个色块，宽度 = 段字符占比。"""
    if not segments or total_chars <= 0:
        return '<div class="band-empty">无分段数据</div>'
    parts = []
    for seg in segments:
        n = len(seg["text"])
        w = max(0.6, n / total_chars * 100)
        band_key, _ = zhuque_svc.band_of(seg["conf"])
        parts.append(
            f'<span class="band-seg" style="width:{w:.3f}%;background:{_BAND_COLORS[band_key]}" '
            f'title="片段{seg["order"]}：AIGC 值 {seg["conf"]:.4f}"></span>'
        )
    return '<div class="band">' + "".join(parts) + "</div>"


def _segments_table(segments: list[dict], total_chars: int) -> str:
    rows = []
    for seg in segments:
        n = len(seg["text"])
        ratio = n / total_chars if total_chars else 0
        conf = seg["conf"]
        color = _LABEL_COLORS.get(seg["label_name"], "#666")
        rows.append(
            "<tr>"
            f"<td>{seg['order']}</td>"
            f"<td class='seg-text'>{_e(seg['text'][:38])}{'…' if len(seg['text']) > 38 else ''}</td>"
            f"<td>{_fmt_pct(ratio)}</td>"
            f"<td>{n}</td>"
            f"<td style='color:{color};font-weight:600'>{conf:.4f}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>序号</th><th>片段</th><th>占全文比例</th><th>占字符数</th>"
        "<th>AIGC 值</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _segment_details(segments: list[dict]) -> str:
    if not segments:
        return ""
    blocks = []
    for seg in segments:
        color = _LABEL_COLORS.get(seg["label_name"], "#666")
        blocks.append(
            f'<div class="seg-detail"><h4>NO.{seg["order"]}　片段{seg["order"]}　'
            f'<span style="color:{color}">AIGC 值 {seg["conf"]:.4f}</span></h4>'
            f'<p>{_e(seg["text"])}</p></div>'
        )
    return "<h3>检测片段详情</h3>" + "".join(blocks)


def _local_scan_html(scan: dict | None) -> str:
    """本地人工痕迹体检区（我们自己的规则 + 统计）。"""
    if not scan:
        return ""
    score = scan.get("score", 0)
    hits = scan.get("hits") or []
    stats = scan.get("stats") or {}
    hit_rows = []
    for h in hits:
        sev = h.get("severity", "low")
        sev_text = {"high": "高", "mid": "中", "low": "低"}.get(sev, sev)
        sample = "；".join(h.get("samples") or [])[:120]
        hit_rows.append(
            "<tr>"
            f"<td>{_e(h.get('label', ''))}</td>"
            f"<td>{sev_text}</td>"
            f"<td>{h.get('count', 0)}</td>"
            f"<td>{_e(sample)}</td>"
            f"<td>{_e(h.get('how', ''))}</td>"
            "</tr>"
        )
    hits_html = (
        "<table><thead><tr><th>问题</th><th>严重度</th><th>次数</th><th>命中片段</th><th>怎么改</th></tr></thead><tbody>"
        + "".join(hit_rows)
        + "</tbody></table>"
        if hit_rows
        else "<p class='ok-line'>未命中已知的 AI 痕迹模式。</p>"
    )
    stats_items = "".join(
        f"<li>{_e(k)}：{v}</li>" for k, v in list(stats.items())[:8]
    )
    return (
        "<h3>本地人工痕迹体检（规则 + 统计，零模型）</h3>"
        f'<div class="local-score"><span class="local-score-num">{score}</span>'
        "<span class='local-score-label'>人工痕迹指数（0–100，越高越像人写的）</span></div>"
        f"{hits_html}"
        f"<details><summary>统计指标（{_e(scan.get('level_label', ''))}）</summary><ul>{stats_items}</ul></details>"
        f"<p class='note'>{_e(scan.get('note', ''))}</p>"
    )


_CSS = """
* { box-sizing: border-box; }
body { font-family: "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif;
       color: #222; max-width: 780px; margin: 0 auto; padding: 28px 20px 60px; background: #fff; }
h1 { font-size: 20px; margin: 0 0 4px; }
h3 { font-size: 15px; margin: 26px 0 10px; border-left: 4px solid #4a7cff; padding-left: 8px; }
.meta { color: #888; font-size: 12px; margin-bottom: 20px; }
.banner { border-radius: 8px; padding: 12px 16px; font-size: 15px; margin: 16px 0; }
.banner.warn { background: #fff7e6; color: #a05b00; border: 1px solid #ffe1a8; }
.banner.bad { background: #fff1f2; color: #b3223a; border: 1px solid #ffcdd5; }
.banner.good { background: #edfaf0; color: #137a3c; border: 1px solid #bfe8cd; }
.tip { background: #f6f8fb; border: 1px solid #e3e8f0; border-radius: 8px; padding: 10px 14px;
       font-size: 12px; color: #667; margin: 14px 0; }
.result-row { display: flex; align-items: center; gap: 32px; flex-wrap: wrap; }
.legend { font-size: 13px; }
.legend div { display: flex; align-items: center; gap: 8px; margin: 8px 0; }
.dot { width: 14px; height: 14px; border-radius: 4px; display: inline-block; }
.band { display: flex; width: 100%; height: 22px; border-radius: 6px; overflow: hidden; margin: 8px 0 4px; }
.band-seg { height: 100%; }
.band-empty { color: #999; font-size: 12px; }
.band-legend { font-size: 12px; color: #667; display: flex; gap: 18px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0; }
th, td { border: 1px solid #e6e9ef; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f6f8fb; font-weight: 600; }
.seg-text { max-width: 320px; }
.seg-detail { border: 1px solid #e6e9ef; border-radius: 8px; padding: 10px 14px; margin: 10px 0; }
.seg-detail h4 { margin: 0 0 6px; font-size: 13px; }
.seg-detail p { margin: 0; font-size: 13px; line-height: 1.8; white-space: pre-wrap; }
.local-score { display: flex; align-items: baseline; gap: 10px; margin: 10px 0; }
.local-score-num { font-size: 32px; font-weight: 700; color: #4a7cff; }
.local-score-label { font-size: 12px; color: #667; }
.ok-line { color: #137a3c; font-size: 13px; }
.note { font-size: 12px; color: #889; background: #fafbfd; border-radius: 6px; padding: 8px 10px; }
.disclaimer { margin-top: 36px; border-top: 1px dashed #dde; padding-top: 14px;
              font-size: 12px; color: #99a; line-height: 1.9; }
details summary { cursor: pointer; font-size: 13px; color: #4a7cff; }
@media print { body { padding: 0; } .seg-detail { break-inside: avoid; } }
"""


def render_report(
    *,
    text: str,
    scan: dict | None,
    zhuque_result: dict | None,
    platform: str = "",
    generated_at: int | None = None,
) -> str:
    """生成完整报告单 HTML。

    scan: humanize.scan() 的结果（可 None）
    zhuque_result: zhuque.classify() 的结果（可 None 或 ok=False——降级为纯本地报告）
    """
    total_chars = len("".join((text or "").split()))
    zq_ok = bool(zhuque_result and zhuque_result.get("ok"))
    segments = (zhuque_result or {}).get("segments") or []

    # ---- 检测结果区 ----
    if zq_ok:
        human, suspect, ai = (
            zhuque_result["human_ratio"],
            zhuque_result["suspect_ratio"],
            zhuque_result["ai_ratio"],
        )
        tone, conclusion_text = zhuque_svc.conclusion(human, ai, suspect)
        result_html = (
            f'<h3>检测结果</h3>'
            f'<div class="banner {tone}">{_e(conclusion_text)}</div>'
            '<div class="result-row">'
            + _donut_svg(human, suspect, ai)
            + '<div class="legend">'
            f'<div><span class="dot" style="background:{_BAND_COLORS["human"]}"></span>人工特征　{_fmt_pct(human)}</div>'
            f'<div><span class="dot" style="background:{_BAND_COLORS["suspect"]}"></span>疑似AI　{_fmt_pct(suspect)}</div>'
            f'<div><span class="dot" style="background:{_BAND_COLORS["ai"]}"></span>AI特征　{_fmt_pct(ai)}</div>'
            "</div></div>"
            '<div class="tip">提示：本结果仅为辅助判断，不应作为任何审核或处罚的决定性依据。'
            "AI 生成内容检测对中文文本存在固有误差，低分不代表一定人工创作，高分也不代表一定 AI 生成。</div>"
        )
        band_html = (
            "<h3>AI 分布图</h3>"
            + _band_html(segments, total_chars)
            + '<div class="band-legend">'
            '<span><span class="dot" style="background:#7bd88f"></span> 人工特征（AIGC值 0–0.5）</span>'
            '<span><span class="dot" style="background:#f6c15a"></span> 疑似AI（0.5–0.9）</span>'
            '<span><span class="dot" style="background:#f27b8e"></span> AI特征（0.9–1）</span></div>'
        )
        seg_table_html = "<h3>片段解析</h3>" + _segments_table(segments, total_chars)
        seg_detail_html = _segment_details(segments)
    else:
        reason = (zhuque_result or {}).get("error") or "未运行朱雀检测"
        result_html = (
            "<h3>检测结果</h3>"
            f'<div class="banner warn">朱雀 AI 检测未执行：{_e(reason)}</div>'
            '<div class="tip">配置 ZHUQUE_API_KEY（腾讯 EdgeOne Makers API Key）后，'
            "报告单将包含官方同款的三分类占比、AI 分布图与片段解析。</div>"
        )
        band_html = ""
        seg_table_html = ""
        seg_detail_html = ""

    local_html = _local_scan_html(scan)

    title = "AI 生成检测报告单"
    platform_text = f"　平台：{_e(platform)}" if platform else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{_e(title)}</h1>
<div class="meta">检测时间：{_fmt_dt(generated_at)}{platform_text}　|　正文 {total_chars} 字　|　content-workbench 人工痕迹工作台</div>
{result_html}
{band_html}
{seg_table_html}
{seg_detail_html}
{local_html}
<div class="disclaimer">
  1. 本报告由 content-workbench 生成，结合「本地规则 + 统计体检」与「朱雀 AI 生成检测（腾讯云）」两部分，两部分独立计算、互不影响。<br>
  2. 朱雀检测结果仅为辅助判断，不应作为任何审核或处罚的决定性依据；本地人工痕迹指数是基于公开研究样本的统计特征扫描，同样不构成「是否 AI 生成」的判定。<br>
  3. 报告为检测时点的单次快照，不存储正文内容。
</div>
</body>
</html>"""
