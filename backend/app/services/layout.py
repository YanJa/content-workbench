"""公众号排版服务：把 Markdown 装配成可直接粘贴的公众号 HTML。

原理（方案 A'）：gzh-design 技能的组件库搬进后端当提示词资产，
用 LLM 按组件库装配 HTML，再跑确定性校验，有问题回喂重修。

为什么不用「纯代码模板填充」：组件库的价值在于「判断文章类型 → 选组件配方 →
给每段标关键词下划线」这类**语义判断**，纯字符串替换做不出来。
LLM 读组件库文本即可复现这套判断，无需把组件库编译成 Python 模板。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assets.gzh.themes import (
    DEFAULT_THEME,
    THEMES,
    load_common_lib,
    load_theme_lib,
    list_themes,
)
from app.core.config import settings
from app.core.logging import logger
from app.core.security import link_url
from app.db.session import SessionLocal
from app.models.content import Content, QueueItem
from app.prompts.layout_prompt import LAYOUT_FIX_HINT, build_layout_messages
from app.services.author import apply_author_placeholders, apply_placeholders
from app.services.gzh_validate import validate_gzh_html
from app.services.llm import get_llm

# 最多让模型重修几次（每次把校验报错回喂）。2 次足够，再多是浪费 token。
_MAX_FIX_ROUNDS = 2

# 进度回调：(percent, stage) -> None
ProgressCb = Callable[[int, str], None]

# ------------------------------------------------------------------ 异步任务
# 为什么排版要开后台任务（2026-10-16）：公众号排版走 pro 模型装配 + 校验回修，
# 实测单次 ~2:49（170 秒），而前端 axios 全局 timeout 只有 60 秒 —— 请求早就
# 断了，后端却还在跑，用户看到的现象是「一直转圈然后超时，稍后再打开发现已排好」。
# 改成「提交任务 → 秒回 task_id → 前端轮询进度」，既不再超时，又能看到真实进度。
#
# 进程内任务表即可（不像视频渲染要落库）：排版幂等，最坏情况进程重启丢任务，
# 用户重新点一次即可，代价只是一次 LLM 调用。完成后保留 1 小时再清，供前端补取结果。
_LAYOUT_JOBS: dict[str, dict[str, Any]] = {}
_LAYOUT_LOCK = asyncio.Semaphore(1)  # 串行排版，防并发烧 token / 抢 CPU
_JOB_TTL = 3600  # 完成 / 失败后保留 1 小时

# 模型偶尔仍会包一层 ```html 围栏，或带上文档外壳 —— 这两种都剥掉
_FENCE_RE = re.compile(r"^\s*```(?:html)?\s*|\s*```\s*$", re.I)


def _clean_html(raw: str) -> str:
    """剥掉代码围栏与文档外壳，只留 <section> 片段。"""
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    # 若模型给了完整文档，截取 body 内容
    m = re.search(r"<body[^>]*>(.*)</body>", text, re.I | re.S)
    if m:
        text = m.group(1).strip()
    # 从第一个 <section 开始截（前面可能有零星说明文字）
    idx = text.find("<section")
    if idx > 0:
        text = text[idx:]
    # 去掉末尾多余说明（</section> 之后的东西）
    end = text.rfind("</section>")
    if end != -1:
        text = text[: end + len("</section>")]
    return text.strip()


def _image_manifest(images: list[dict[str, Any]] | None) -> str:
    """把配图列表转成给模型的清单文本。

    第 1 张若是封面（source=cover）会显式标出来——模型据此知道
    `<!--IMG:1-->` 是头图，通常应放在正文最前面。
    """
    if not images:
        return ""
    lines = []
    for i, img in enumerate(images, start=1):
        alt = (img.get("alt") or img.get("prompt") or "配图").strip()
        prefix = "【封面/头图】" if img.get("source") == "cover" else ""
        lines.append(f"{i}. {prefix}{alt[:60]}")
    return "\n".join(lines)


_TOPIC_RE = re.compile(r"#([^#\s]{1,20})#")


async def generate_topic_tags(markdown: str, on_progress: ProgressCb | None = None) -> list[str]:
    """按正文主题生成 2-3 个精准微信话题标签（#话题#）。

    仅作锦上添花：失败（限流/超时/解析不出）一律降级为空列表，绝不拖累排版。
    """
    if not markdown.strip():
        return []
    if on_progress:
        on_progress(92, "生成文末话题标签…")
    llm = get_llm()
    messages = [
        {
            "role": "system",
            "content": (
                "你是公众号话题标签专家。根据文章提炼 2-3 个精准、可被搜索的长尾话题标签。"
                "要求：每个标签 2-8 个字；必须是具体主题词（如 #西安马拉松攻略#、#时间管理技巧#），"
                "不要宽泛词（如 #生活# #日常# #分享# #随笔#）；不要重复；"
                "只输出标签本身，用 #标签# 包裹、空格分隔，不要任何解释、标点或换行。"
            ),
        },
        {"role": "user", "content": markdown[:2000]},
    ]
    try:
        result = await llm.chat(messages, task="lightweight")
        text = (result.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成话题标签失败（跳过）: %s", exc)
        return []
    tags: list[str] = []
    for m in _TOPIC_RE.findall(text):
        if m and m not in tags:
            tags.append(m)
        if len(tags) >= 3:
            break
    return tags


def _inject_images(html: str, images: list[dict[str, Any]] | None) -> str:
    """把模型留的 `<!--IMG:n-->` 占位替换成真实 <img>。

    公众号要求 img 居中自适应：max-width:100%;height:auto;display:block;margin:0 auto。

    注意：模型偶尔会把占位符塞进某个 <img> 标签内部（如 `<img src="<!--IMG:1-->" />`）。
    若直接把占位符原地替换成 `<img>`，会把 HTML 插进 src 属性值里，破坏外层标签、
    图片裂掉、残留的 style 属性变成可见文字。所以先整体替换「携带占位符的 <img> 标签」，
    再做常规占位符替换。
    """
    if not images:
        # 没有配图就把占位标记清掉，不留脏注释
        return re.sub(r"<!--\s*IMG:\d+\s*-->", "", html)

    def make_img(m: re.Match[str]) -> str:
        try:
            n = int(m.group(1))
        except (IndexError, ValueError):
            return ""
        if n < 1 or n > len(images):
            return ""
        img = images[n - 1]
        src = img.get("url", "")
        if not src:
            return ""
        alt = (img.get("alt") or "").replace('"', "&quot;")
        return (
            '<p style="margin:16px 0;text-align:center;">'
            f'<img src="{src}" alt="{alt}" '
            'style="max-width:100%;height:auto;display:block;margin:0 auto;'
            'border-radius:8px;" />'
            "</p>"
        )

    # 第 1 遍：占位符嵌在 <img> 标签内（如 src="<!--IMG:1-->"），整体替换该 img 标签
    out = re.sub(r"<img\b[^>]*?<!--\s*IMG:(\d+)\s*-->[^>]*>", make_img, html)
    # 第 2 遍：常规独立占位符
    out = re.sub(r"<!--\s*IMG:(\d+)\s*-->", make_img, out)
    return out


async def _llm_layout(
    markdown: str,
    theme_id: str,
    image_manifest: str,
    on_progress: ProgressCb | None = None,
) -> tuple[str, list[str], list[str]]:
    """调 LLM 装配 HTML + 校验回修。返回 (html, errors, warnings)。"""

    def tick(p: int, s: str) -> None:
        if on_progress:
            on_progress(p, s)

    theme_lib = load_theme_lib(theme_id)
    common_lib = load_common_lib()
    llm = get_llm()

    messages = build_layout_messages(
        markdown, theme_lib, common_lib, image_manifest=image_manifest
    )
    tick(30, "AI 正在按主题装配排版…")
    # 用 reasoning（pro 模型）：排版是「理解结构 + 严格照规则装配」，质量优先
    result = await llm.chat(messages, task="reasoning")
    html = _clean_html(result.content or "")
    errors, warnings, _ = validate_gzh_html(html)

    round_no = 0
    while errors and round_no < _MAX_FIX_ROUNDS:
        round_no += 1
        logger.info("排版校验未过（第 %d 次回修）：%s", round_no, errors[:3])
        tick(60 + round_no * 10, f"合规校验未过，AI 回修第 {round_no} 次…")
        fix_messages = messages + [
            {"role": "assistant", "content": html},
            {"role": "user", "content": LAYOUT_FIX_HINT.format(errors="\n".join(f"- {e}" for e in errors))},
        ]
        result = await llm.chat(fix_messages, task="reasoning")
        html = _clean_html(result.content or "")
        errors, warnings, _ = validate_gzh_html(html)

    tick(90, "校验通过，正在装订配图…")
    return html, errors, warnings


# ------------------------------------------------------------------ 关注卡
# 组件库用 `<!--OPENING_FOLLOW_CARD-->…<!--/OPENING_FOLLOW_CARD-->` 包住开头关注卡。
# 本号只在文末放一次关注卡，因此排版后把这一整块（含标记）剥离。
_OPEN_CARD_RE = re.compile(
    r"<!--OPENING_FOLLOW_CARD-->.*?<!--/OPENING_FOLLOW_CARD-->",
    re.DOTALL,
)
# 兜底：模型有时会把注释标记丢掉，只留组件本体。此时用「关注我，每周更新」
# 这个开头卡专属文案定位它所在的最外层 <section>…</section>。
_OPEN_CARD_TEXT = "关注我，每周更新"


def _strip_opening_follow_card(html: str) -> str:
    """剥离「开头关注卡」（本号只在文末放一次关注卡）。

    两级策略：
      1. 优先用显式标记 `<!--OPENING_FOLLOW_CARD-->…` 精确整块删除（最稳）；
      2. 模型丢了标记时，用开头卡专属文案定位其最外层 `<section>` 并删除。
    其它主题不含该文案 → 天然 no-op，零破坏。
    """
    if not html:
        return html
    n = len(_OPEN_CARD_RE.findall(html))
    if n:
        html = _OPEN_CARD_RE.sub("", html)
        logger.info("已剥离开头关注卡 %d 处", n)
        return html

    if _OPEN_CARD_TEXT not in html:
        return html

    # 无标记兜底：把开头卡作为一个 `<section>` 子树的起始位置来定位。
    # 做法：先扫出所有 `<section` / `</section>` 的位置，用「配对栈」求出每对
    # section 的区间，再选**包含该文案的最外层**那一段删掉。
    # 直接 rfind("<section") 会命中最近的**内层** section（错误），故不能那样做。
    idx = html.find(_OPEN_CARD_TEXT)
    starts: list[tuple[int, int]] = []  # (起止) 栈，元素是 <section 的起始下标
    sections: list[tuple[int, int]] = []  # 每个顶层 section 的 (start, end)
    for m in re.finditer(r"<section\b|</section\s*>", html):
        if m.group(0).startswith("</"):
            if starts:
                s = starts.pop()
                if not starts:  # 配平到最外层
                    sections.append((s, m.end()))
        else:
            starts.append(m.start())

    # 选包含文案、且区间最小的那一整段（最内层完整包裹它的 section）
    chosen: tuple[int, int] | None = None
    for s, e in sections:
        if s <= idx < e:
            if chosen is None or (e - s) < (chosen[1] - chosen[0]):
                chosen = (s, e)
    if chosen is None:
        return html
    logger.info("已剥离开头关注卡（无标记兜底）1 处")
    return html[: chosen[0]] + html[chosen[1] :]


async def _resolve_account(db: AsyncSession, content: Content, account_id: str):
    """取用于关注卡的公众号：显式 account_id → 默认号 → None。"""
    from sqlalchemy import select as _select

    from app.models.settings import WeChatAccount

    owner = content.owner or "default"
    if account_id:
        acc = await db.get(WeChatAccount, account_id)
        if acc and (acc.owner or "default") == owner:
            return acc
    stmt = (
        _select(WeChatAccount)
        .where(WeChatAccount.owner == owner, WeChatAccount.is_default.is_(True))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _apply_account_placeholders(html: str, account) -> str:
    """替换关注卡里的 `{{号名}}` / `{{slogan}}`。

    `{{号名}}` 回退顺序：gzh_name → name（避免空值导致占位符残留）。
    `{{slogan}}` 为空则保留占位符（与作者维度同语义：让用户看到「还没填」）。
    """
    if not html or account is None:
        return html
    return apply_placeholders(
        html,
        {
            "号名": (account.gzh_name or account.name or "").strip(),
            "slogan": (account.slogan or "").strip(),
        },
    )


def _qrcode_public_url(owner: str, qrcode_path: str) -> str:
    """二维码相对路径 → 可直连 URL（HMAC 签名做租户隔离）。"""
    parts = (qrcode_path or "").split("/")
    if len(parts) != 3:
        return ""
    _, aid, fname = parts
    return link_url(f"{settings.API_PREFIX}/settings/wechat/qrcodes/{aid}/{fname}", owner)


def _inject_qrcode(html: str, account) -> str:
    """把 `{{二维码}}` 换成该号的二维码 `<img>`；没配二维码则整块占位删除。

    - 占位块（含其外层 `<p>`）由主题库以 `<!--QRCODE_SLOT-->` 标记包裹，
      这里整块替换，避免留下空白行。
    - `<img>` 放在 `<span leaf>` 之外（微信对 span 内块级 img 会套行内样式）。
    """
    if not html:
        return html

    slot_re = re.compile(
        r"<!--QRCODE_SLOT-->.*?<!--/QRCODE_SLOT-->",
        re.DOTALL,
    )
    has_slot = "<span leaf=\"\">{{二维码}}</span>" in html
    if account is None or not getattr(account, "qrcode_path", ""):
        # 无二维码：删掉整块占位（含标记与空 <p>）
        if slot_re.search(html):
            html = slot_re.sub("", html)
        elif has_slot:
            html = re.sub(
                r"<p[^>]*>\s*<span leaf=\"\">\s*\{\{二维码\}\}\s*</span>\s*</p>",
                "",
                html,
            )
        return html

    owner = account.owner or "default"
    src = _qrcode_public_url(owner, account.qrcode_path)
    if not src:
        return html
    img = (
        f'<img src="{src}" alt="扫码关注" '
        'style="width:180px;max-width:60%;height:auto;display:block;margin:0 auto;">'
    )
    # 优先整块替换（保留居中容器）；退化到只替换 span 内层文字
    if slot_re.search(html):
        html = slot_re.sub(
            '<p style="margin:16px 0 0;text-align:center;"><span leaf="">'
            + img
            + "</span></p>",
            html,
        )
    elif has_slot:
        html = html.replace(
            '<span leaf="">{{二维码}}</span>', '<span leaf="">' + img + "</span>"
        )
    return html


async def layout_content(
    db: AsyncSession,
    content: Content,
    theme_id: str = DEFAULT_THEME,
    platform: str = "gzh",
    on_progress: ProgressCb | None = None,
    author_name: str = "",
    author_intro: str = "",
    account_id: str = "",
    auto_tags: bool = True,
) -> dict[str, Any]:
    """把内容排版。写回 content.body_html / theme / layout_status，并把队列项置 ready。

    author_name / author_intro 非空时，在写库前替换产出的 HTML 里的
    `{{作者名}}` / `{{一句话简介}}` 占位符（见 services/author.py）。

    account_id 为空时用默认公众号；用它替换 `{{号名}}` / `{{slogan}}`，
    并把 `{{二维码}}` 换成该号的二维码图，同时剥离「开头关注卡」（本号只在文末放一次）。

    返回 {ok, html_len, errors, warnings, theme, theme_name}。
    """

    def tick(p: int, s: str) -> None:
        if on_progress:
            on_progress(p, s)

    if theme_id not in THEMES:
        theme_id = DEFAULT_THEME

    markdown = (content.body or "").strip()
    if not markdown:
        return {"ok": False, "error": "内容正文为空，无法排版"}

    if platform == "xiaohongshu":
        tick(40, "AI 正在规整小红书笔记文本…")
        return await _layout_xiaohongshu(db, content)

    images = content.images or []
    manifest = _image_manifest(images)

    tick(15, "正在准备主题组件库…")
    html, errors, warnings = await _llm_layout(markdown, theme_id, manifest, on_progress=on_progress)
    # 把配图占位替换成真实图片
    html = _inject_images(html, images)
    # 关注卡处理：先剥离开头卡，再替换账号占位符、注入二维码。
    # 顺序很重要：二维码替换要在账号替换之后（两者互不干扰，但保持可读的先后语义）。
    html = _strip_opening_follow_card(html)
    account = await _resolve_account(db, content, account_id)
    html = _apply_account_placeholders(html, account)
    html = _inject_qrcode(html, account)
    # 作者占位符替换（在写库前，与排版同一次落库；放在校验之后不影响合规性）
    if author_name or author_intro:
        html = apply_author_placeholders(html, author_name, author_intro)

    # 文末自动补话题标签（仅公众号）：让 LLM 按正文主题生成 2-3 个精准 #话题#，
    # 追加到文末（关注卡/二维码之后）。微信发布时会把 #话题# 自动识别成可点的话题入口，
    # 利于「搜一搜」发现与话题聚合；失败静默跳过，不拖累排版。
    if auto_tags and platform == "gzh":
        tags = await generate_topic_tags(markdown, on_progress)
        if tags:
            tag_html = " ".join(f"#{t}#" for t in tags)
            html += (
                '<p style="margin-top:18px;color:#8a8a8a;font-size:14px;'
                'text-align:left;letter-spacing:1px;">'
                f"{tag_html}</p>"
            )

    now = int(time.time())
    content.body_html = html
    content.theme = theme_id
    content.layout_status = "ready" if not errors else "none"
    content.updated_at = now
    # 多平台快照，便于以后同时维护两种成稿形态
    layouts = dict(content.layouts or {})
    layouts["gzh"] = {
        "theme": theme_id,
        "theme_name": THEMES[theme_id]["name"],
        "html": html,
        "warnings": warnings,
        "updated_at": now,
    }
    content.layouts = layouts

    # 排版完成 → 队列项从「待加工」转「待发布」（仅当没有致命校验错误）
    if not errors:
        item = (
            await db.execute(
                select(QueueItem).where(QueueItem.content_id == content.id)
            )
        ).scalars().first()
        if item and item.status == "draft":
            item.status = "ready"

    await db.flush()
    logger.info(
        "排版完成 content=%s theme=%s html=%d 字符 errors=%d warnings=%d",
        content.id, theme_id, len(html), len(errors), len(warnings),
    )
    return {
        "ok": not errors,
        "theme": theme_id,
        "theme_name": THEMES[theme_id]["name"],
        "html_len": len(html),
        "errors": errors,
        "warnings": warnings,
        "layout_status": content.layout_status,
    }


async def _layout_xiaohongshu(db: AsyncSession, content: Content) -> dict[str, Any]:
    """小红书不做 HTML 排版，只做文本规整（分点/emoji/标签归位）。

    小红书是纯文本 + emoji + 图，没有富文本概念。这里让轻量模型把
    Markdown 整理成可直接粘贴的笔记文本。
    """
    llm = get_llm()
    messages = [
        {
            "role": "system",
            "content": (
                "你是小红书文案排版助手。把下面的 Markdown 笔记整理成可直接粘贴到"
                "小红书 App 的纯文本：标题单独一行（不超过 20 字，可带 emoji），"
                "正文分点、每点用 emoji 开头、段间空行，话题标签放在最后一行、"
                "以 # 开头空格分隔。不要输出 Markdown 语法符号（#、*、>），"
                "不要输出任何解释说明，只输出整理后的纯文本。"
            ),
        },
        {"role": "user", "content": content.body or ""},
    ]
    result = await llm.chat(messages, task="lightweight")
    text = (result.content or "").strip()

    now = int(time.time())
    layouts = dict(content.layouts or {})
    layouts["xiaohongshu"] = {"text": text, "updated_at": now}
    content.layouts = layouts
    content.layout_status = "ready"
    content.updated_at = now
    await db.flush()
    return {
        "ok": True,
        "theme": "xiaohongshu",
        "theme_name": "小红书文本规整",
        "html_len": len(text),
        "errors": [],
        "warnings": [],
        "layout_status": "ready",
        "text": text,
    }


# ------------------------------------------------------------- 异步任务入口
def _prune_jobs() -> None:
    """清理过期任务，防内存泄漏（每次提交时顺手清一次，量极小）。"""
    now = time.time()
    stale = [
        k
        for k, v in _LAYOUT_JOBS.items()
        if v.get("status") in ("done", "error")
        and now - v.get("finished_at", now) > _JOB_TTL
    ]
    for k in stale:
        _LAYOUT_JOBS.pop(k, None)


def _set_job(task_id: str, **kw: Any) -> None:
    job = _LAYOUT_JOBS.get(task_id)
    if job is not None:
        job.update(kw)


def cancel_layout_job_for_content(content_id: str) -> str:
    """把某内容**在内存里仍标记为 running/queued** 的排版任务收尾成 error。

    背景：排版在 `_LAYOUT_LOCK` 里串行跑，无法从外部真正杀掉协程；用户「取消」
    要的是**解除互斥标记**（这样按钮恢复可点）并让仍开着的面板停止等待。这里只
    负责后者——把内存任务标记成「已取消」，前端轮询到 error 即 stopPoll。

    返回被取消的 task_id（没有则空串）。
    """
    for tid, job in _LAYOUT_JOBS.items():
        if job.get("content_id") == content_id and job.get("status") in ("queued", "running"):
            job.update(
                status="error",
                error="已取消",
                stage="已取消",
                finished_at=time.time(),
            )
            return tid
    return ""


async def recover_stuck_processing() -> int:
    """服务启动时清空残留的「加工中」标记（Content.processing）。

    为什么必须有：`processing` 是 DB 里的长任务互斥标记，正常路径在任务 finally 里清。
    但**进程重启/沙箱休眠**时正在跑的任务没了，标记就永久留在库里，队列页所有操作
    按钮会被永久禁用（用户实测踩过）。启动时统一清掉，重启即自愈。
    """
    async with SessionLocal() as db:
        res = await db.execute(
            select(Content.id).where(Content.processing != "")
        )
        stuck = res.scalars().all()
        for cid in stuck:
            c = await db.get(Content, cid)
            if c is not None:
                c.processing = ""
        await db.commit()
        n = len(stuck)
    if n:
        logger.warning("启动清理：%d 条内容残留的加工中标记已复位", n)
    return n


async def submit_layout_job(
    content_id: str,
    theme_id: str = DEFAULT_THEME,
    platform: str = "gzh",
    auto_cover: bool = True,
    author_name: str = "",
    author_intro: str = "",
    account_id: str = "",
    auto_tags: bool = True,
) -> dict[str, Any]:
    """登记排版任务并**立即返回**，真正排版交给后台协程。

    返回 {ok, task_id, status, poll}。前端拿 task_id 去 GET .../layout/{task_id} 轮询。

    auto_cover=True 时，排版前若还没有封面，自动按正文生成一张并置顶
    （封面通道不可用则静默跳过，不影响排版）。

    author_name / author_intro：排版完成后用于替换正文里的
    `{{作者名}}` / `{{一句话简介}}` 占位符；留空则不替换。

    account_id：关注卡用哪个公众号（留空 = 默认号），替换 `{{号名}}`/`{{slogan}}`
    并注入二维码。
    """
    _prune_jobs()
    task_id = uuid.uuid4().hex
    _LAYOUT_JOBS[task_id] = {
        "task_id": task_id,
        "content_id": content_id,
        "theme": theme_id,
        "platform": platform,
        "status": "queued",
        "progress": 0,
        "stage": "排队中…",
        "result": None,
        "error": "",
        "started_at": time.time(),
        "finished_at": None,
    }
    asyncio.create_task(
        _run_layout_job(
            task_id,
            content_id,
            theme_id,
            platform,
            auto_cover,
            author_name,
            author_intro,
            account_id,
            auto_tags,
        )
    )
    return {
        "ok": True,
        "task_id": task_id,
        "status": "queued",
        "message": "排版任务已提交",
        "poll": f"{settings.API_PREFIX}/content/items/{content_id}/layout/{task_id}",
    }


async def _ensure_cover(db: AsyncSession, content: Content) -> None:
    """排版前确保有封面：没有就按正文自动生成一张，插到配图首位。

    失败只记日志——封面是锦上添花，绝不能因为它把排版带崩。
    """
    from app.services import covers  # 延迟导入，避免循环依赖

    if not covers.available():
        return
    imgs = list(content.images or [])
    if any(im.get("source") == "cover" for im in imgs):
        return  # 用户已有封面（含手动上传后标记的），尊重之

    title, sub = covers.derive_cover_text(content.body or "")
    if not title:
        return
    try:
        out_dir = Path(settings.UPLOAD_DIR) / "gen" / content.id
        path = await covers.generate_cover(title, out_dir, sub=sub, theme=content.theme)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动生成封面失败（跳过，不影响排版）: %s", exc)
        return

    owner = content.owner or "default"
    url = link_url(f"{settings.API_PREFIX}/content/images/{content.id}/{path.name}", owner)
    imgs.insert(
        0,
        {
            "id": uuid.uuid4().hex[:12],
            "path": str(path.relative_to(Path(settings.UPLOAD_DIR))),
            "url": url,
            "alt": "封面",
            "source": "cover",
            "prompt": f"{title} / {sub}".strip(" /"),
            "position": 0,
        },
    )
    content.images = imgs
    await db.commit()
    logger.info("排版前自动生成封面 content=%s -> %s", content.id, path.name)


async def _run_layout_job(
    task_id: str,
    content_id: str,
    theme_id: str,
    platform: str,
    auto_cover: bool = True,
    author_name: str = "",
    author_intro: str = "",
    account_id: str = "",
    auto_tags: bool = True,
) -> None:
    """后台 worker：自建 Session（请求早已返回，不能复用请求期的 db）。"""
    def on_progress(p: int, s: str) -> None:
        _set_job(task_id, progress=p, stage=s, status="running")

    _set_job(task_id, status="running", progress=5, stage="开始排版…")
    try:
        async with _LAYOUT_LOCK:
            async with SessionLocal() as db:
                c = await db.get(Content, content_id)
                if c is None:
                    _set_job(
                        task_id, status="error", error="内容不存在", finished_at=time.time()
                    )
                    return
                # 标记「排版中」并先落库，队列页据此显示状态、禁用重复触发
                c.processing = "layout"
                await db.commit()
                try:
                    # 封面先于装配：这样 LLM 能在清单里看到「1. 封面」并据此决定
                    # 是否在文首留 <!--IMG:1--> 占位
                    if auto_cover:
                        _set_job(task_id, progress=8, stage="生成封面…")
                        await _ensure_cover(db, c)
                    result = await layout_content(
                        db,
                        c,
                        theme_id=theme_id,
                        platform=platform,
                        on_progress=on_progress,
                        author_name=author_name,
                        author_intro=author_intro,
                        account_id=account_id,
                        auto_tags=auto_tags,
                    )
                    if not result.get("ok") and result.get("error"):
                        raise RuntimeError(result["error"])
                finally:
                    # 无论成功失败都清掉加工中标记，否则队列页会一直卡在「排版中」
                    c.processing = ""
                await db.commit()
        _set_job(
            task_id,
            status="done",
            progress=100,
            stage="排版完成",
            result=result,
            finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("排版任务失败 task=%s", task_id)
        # 失败路径也要清标记（finally 已尽力清，这里兜底一次）
        try:
            async with SessionLocal() as db2:
                c2 = await db2.get(Content, content_id)
                if c2 is not None and c2.processing == "layout":
                    c2.processing = ""
                    await db2.commit()
        except Exception:  # noqa: BLE001
            pass
        _set_job(
            task_id,
            status="error",
            error=str(exc),
            stage="排版失败",
            finished_at=time.time(),
        )


def get_layout_job(task_id: str) -> dict[str, Any] | None:
    """轮询取任务状态；不存在（含进程重启后丢失）返回 None。"""
    job = _LAYOUT_JOBS.get(task_id)
    if job is None:
        return None
    return {
        "task_id": job["task_id"],
        "content_id": job["content_id"],
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "result": job["result"],
        "error": job["error"],
    }


__all__ = [
    "layout_content",
    "list_themes",
    "submit_layout_job",
    "get_layout_job",
    "cancel_layout_job_for_content",
    "recover_stuck_processing",
]
