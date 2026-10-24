"""内置工具集。

覆盖三类典型场景：
  - 只读查询：query_queue / check_compliance（模型可自由调用）
  - 写操作：save_content（仅落库为草稿，需前端显式触发，不入队）
  - 对外操作：push_wechat_draft / delete_wechat_draft / render_video（需人工确认）

说明：所有工具均已接入真实实现（WeChatClient / submit_render_job），
无 TODO 占位。
"""
from __future__ import annotations

import re
import uuid
import time
from typing import Any

from sqlalchemy import select

from app.core.logging import logger
from app.tools.registry import dynamic_tool, tool

# emoji 检测：知乎正文堆 emoji 显得不专业，合规自检要数一数。
# 只覆盖常见区段，够用就行 —— 精确判定 emoji 需要引入第三方库，不值当。
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u2190-\u21ff]"
)


def _zhihu_form(sub_type: str) -> str:
    """子形态的中文名，只用于提示文案。"""
    return {"answer": "回答", "article": "文章", "idea": "想法"}.get(sub_type, "正文")


async def _resolve_wechat_client(db, account_id: str = "", owner: str = "default"):
    """按 account_id / 默认号 / .env 的顺序解析出可用的 WeChatClient。

    优先级（2026-10-16 起支持多公众号）：
      1. 显式传的 account_id（队列页/排版页选了哪个号就推哪个）
      2. 数据库里的默认号（is_default=True，且属于当前 owner）
      3. 回退 .env 的 WECHAT_APPID / WECHAT_SECRET（兼容只有一套全局凭证的老用法）
    返回 (client, source)，source ∈ {"account", "env", "none"}。

    owner 过滤：别人的公众号账号（含 appid/secret）不能跨租户拿到。
    """
    from app.models.settings import WeChatAccount
    from app.services.wechat import WeChatClient

    if db is not None:
        try:
            acc = None
            if account_id:
                acc = await db.get(WeChatAccount, account_id)
                # 显式指定的号也必须属于当前 owner
                if acc is not None and (acc.owner or "default") != owner:
                    acc = None
            if acc is None:
                acc = (
                    await db.execute(
                        select(WeChatAccount)
                        .where(
                            WeChatAccount.owner == owner,
                            WeChatAccount.is_default.is_(True),
                        )
                        .limit(1)
                    )
                ).scalars().first()
            if acc is not None and acc.appid and acc.secret:
                return WeChatClient(appid=acc.appid, secret=acc.secret), "account"
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析公众号账号失败（回退 .env）: %s", exc)

    env_client = WeChatClient()
    if env_client.configured:
        return env_client, "env"
    return None, "none"


async def _resolve_wechat_account(db, account_id: str = "", owner: str = "default"):
    """解析出用于「关注引导」的公众号账号对象（可能为 None）。

    与 `_resolve_wechat_client` 同样遵守 owner 隔离与「显式 → 默认号」优先级，
    但返回的是 **WeChatAccount 对象**（不是 client）——推送时要用它拿二维码路径。
    .env 兼容路径没有账号记录，故返回 None（无二维码可转存）。
    """
    from app.models.settings import WeChatAccount

    if db is None:
        return None
    try:
        acc = None
        if account_id:
            acc = await db.get(WeChatAccount, account_id)
            if acc is not None and (acc.owner or "default") != owner:
                acc = None
        if acc is None:
            acc = (
                await db.execute(
                    select(WeChatAccount)
                    .where(
                        WeChatAccount.owner == owner,
                        WeChatAccount.is_default.is_(True),
                    )
                    .limit(1)
                )
            ).scalars().first()
        return acc
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析公众号账号对象失败: %s", exc)
        return None



# ---------------------------------------------------------------- Skill 激活
def _skill_description(names: list[str]) -> str:
    """activate_skill 的描述。

    names 为空时（/api/tools 展示）给一句通用说明；有可用集合时把名字列进去
    —— 描述和 enum 双管齐下，模型才不会去猜没注册的名字。
    """
    base = (
        "激活一套「技能包」，取回它的完整操作说明。"
        "【可用技能】清单里列出了每个技能的用途；当本轮任务确实匹配某个技能的"
        "description 时，先用本工具取回说明，再按说明里的步骤执行。"
        "不匹配的任务不要调用。"
    )
    if names:
        base += f" 当前可用：{', '.join(names)}。"
    return base


def _skill_parameters(names: list[str]) -> dict[str, Any]:
    """name 参数用 enum 约束到可用集合。

    官方对非 Claude 模型的推荐做法：把 enum 收紧到实际可用的技能，模型就不会
    编造不存在的名字；再配合「没有可用技能就不注册这个工具」。
    """
    prop: dict[str, Any] = {
        "type": "string",
        "description": "要激活的技能名（与【可用技能】清单里的名字完全一致）",
    }
    if names:
        prop["enum"] = names
    return {
        "type": "object",
        "properties": {"name": prop},
        "required": ["name"],
    }


async def _list_skill_names(db, owner: str, ctx: dict | None = None) -> list[str]:
    """本次请求可激活的技能名。空列表 → 工具整体不提供给模型。

    `ctx` 是可选的：`dynamic_tool` 优先把 ctx 传进来（这样才读得到
    `use_skills` 这个本轮开关）；老调用只给 (db, owner) 也能工作。
    """
    from app.core.config import settings

    if not settings.ENABLE_SKILLS or db is None:
        return []
    # 用户在输入框旁关掉了技能：本轮连工具都不给，否则模型会凭 description
    # 去猜名字调用（catalog 不在，但工具还在 = 只关了一半）。
    if ctx is not None and ctx.get("use_skills") is False:
        return []
    from app.services import skill as skill_svc

    return [s.name for s in await skill_svc.list_active(db, owner)]


@dynamic_tool(
    name="activate_skill",
    list_fn=_list_skill_names,
    description_for=_skill_description,
    parameters_for=_skill_parameters,
)
async def activate_skill(name: str, ctx: dict | None = None):
    """取回某个技能包的完整说明。"""
    ctx = ctx or {}
    db = ctx.get("db")
    owner = ctx.get("owner", "default")
    if db is None:
        return {"ok": False, "error": "数据库未就绪，暂时无法读取技能"}
    from app.services import skill as skill_svc

    return await skill_svc.activate(db, owner, name)


# ------------------------------------------------------------ 知乎直答
@tool(
    name="zhihu_zhida",
    description=(
        "调用知乎直答（Zhida），就某个问题获取知乎上的真实回答。"
        "当用户想查询知乎、就某话题找知乎上的权威回答或不同观点、需要引用知乎内容时使用。"
        "传入 query（用户的原问题），工具返回知乎生成的回答正文，据此组织你的回复。"
        "会消耗知乎开放平台额度；未配置凭证时会返回明确错误提示，不要假装已有回答。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要问知乎直答的问题，用用户的原话或改写后的清晰提问",
            },
            "model": {
                "type": "string",
                "enum": ["zhida-fast-1p5", "zhida-thinking-1p5", "zhida-agent"],
                "description": "直答模型：快答/带思考/智能体，默认 zhida-fast-1p5",
            },
        },
        "required": ["query"],
    },
)
async def zhihu_zhida(query: str, model: str = "zhida-fast-1p5", ctx: dict | None = None):
    """调用知乎直答获取回答（底层实现见 app/services/zhihu.zhida_query）。"""
    from app.services import zhihu as zhihu_svc

    if not (query or "").strip():
        return {"ok": False, "error": "query 不能为空"}
    result = await zhihu_svc.zhida_query(query.strip(), model=model)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error", "知乎直答调用失败"),
            "hint": result.get("hint", "请确认知乎 Access Secret 是否已配置"),
        }
    return {
        "ok": True,
        "query": query,
        "answer": result["answer"],
        "model": result.get("model", model),
        "note": "以上是知乎直答的回答，据此组织你的回复并注明来源是知乎。",
    }


# ---------------------------------------------------------------- 只读工具
@tool(
    name="query_queue",
    description="查询当前发布队列里的内容及其状态。当用户问『队列里有什么』『哪些还没发』时使用。",
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["draft", "ready", "published", "archived", "all"],
                "description": "按状态筛选，默认 all",
            },
            "platform": {
                "type": "string",
                "enum": ["gzh", "xiaohongshu", "douyin", "zhihu", "all"],
                "description": "按平台筛选，默认 all",
            },
        },
    },
)
async def query_queue(status: str = "all", platform: str = "all", ctx: dict | None = None):
    """查询发布队列。"""
    ctx = ctx or {}
    db = ctx.get("db")
    owner = ctx.get("owner", "default")
    if db is None:
        return {"items": [], "note": "数据库未就绪"}

    from sqlalchemy import select

    from app.models.content import QueueItem

    stmt = select(QueueItem).where(QueueItem.owner == owner)
    if status != "all":
        stmt = stmt.where(QueueItem.status == status)
    if platform != "all":
        stmt = stmt.where(QueueItem.platform == platform)
    stmt = stmt.order_by(QueueItem.created_at.desc()).limit(20)

    rows = (await db.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "platform": r.platform,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@tool(
    name="check_compliance",
    description="检查一段文案是否符合目标平台的规范（字数、标签数、语气、标点等）。生成内容后建议调用此工具自检。",
    parameters={
        "type": "object",
        "properties": {
            "platform": {
                "type": "string",
                "enum": ["gzh", "xiaohongshu", "douyin", "zhihu"],
                "description": "目标平台",
            },
            "sub_type": {
                "type": "string",
                "enum": ["answer", "article", "idea"],
                "description": "子形态：知乎（回答/文章/想法）、公众号（图文/贴图）；其他平台不传",
            },
            "content": {"type": "string", "description": "待检查的文案内容"},
        },
        "required": ["platform", "content"],
    },
)
async def check_compliance(platform: str, content: str, sub_type: str = ""):
    """平台合规自检。"""
    text = content.replace("\n", "")
    length = len(text)
    issues: list[dict[str, str]] = []

    rules = {
        # 公众号两形态篇幅差一个数量级（图文 1200-3500 / 贴图 100-400），
        # 和知乎三形态同理，必须按子形态分档，混一档必然误报。
        # 标签按 # 字符计（微信一个 #话题# 是 2 个 #）：贴图 0-2 = 最多 1 个标签，
        # 微信没有 #话题# 聚合文化，贴图堆一排反而违和。
        "gzh_article": {"min": 1200, "max": 3500, "tags": (0, 0)},
        "gzh_tietu": {"min": 100, "max": 400, "tags": (0, 2)},
        "xiaohongshu": {"min": 300, "max": 800, "tags": (5, 10)},
        "douyin": {"min": 100, "max": 400, "tags": (3, 6)},
        # 知乎三形态篇幅差一个数量级，混在一档里必然误报，所以按子形态分档
        "zhihu_answer": {"min": 600, "max": 2000, "tags": (0, 0)},
        "zhihu_article": {"min": 1500, "max": 6000, "tags": (0, 0)},
        "zhihu_idea": {"min": 50, "max": 300, "tags": (1, 3)},
    }
    key = (
        f"{platform}_{sub_type}"
        if platform in ("zhihu", "gzh")
        and sub_type in ("answer", "article", "idea", "tietu")
        else platform
    )
    r = rules.get(key, {"min": 100, "max": 5000, "tags": (0, 99)})

    if length < r["min"]:
        issues.append({"level": "warn", "msg": f"正文 {length} 字，低于建议下限 {r['min']} 字"})
    if length > r["max"]:
        issues.append({"level": "warn", "msg": f"正文 {length} 字，超过建议上限 {r['max']} 字"})

    tag_count = content.count("#")
    lo, hi = r["tags"]
    if hi > 0 and (tag_count < lo or tag_count > hi):
        issues.append({"level": "warn", "msg": f"话题标签 {tag_count} 个，建议 {lo}-{hi} 个"})

    if platform == "gzh" and any(c in content for c in [",", ".", "!", "?", ":"]):
        issues.append({"level": "warn", "msg": "检测到半角标点，公众号正文建议用全角"})

    if platform == "zhihu":
        # 这两样一出现在知乎，评论区会替你回答
        for bad in ("希望对你有帮助", "点赞收藏", "三连", "觉得有用就"):
            if bad in text:
                issues.append({"level": "warn", "msg": f"出现求互动套话「{bad}」，知乎读者反感"})
                break
        emoji_n = len(_EMOJI_RE.findall(content))
        # 想法允许少量 emoji 做节奏，回答/文章里堆 emoji 显得不专业
        cap = 2 if sub_type == "idea" else 0
        if emoji_n > cap:
            issues.append({"level": "warn", "msg": f"正文有 {emoji_n} 个 emoji，知乎{_zhihu_form(sub_type)}建议不超过 {cap} 个"})
        if "首先" in text and "其次" in text:
            issues.append({"level": "warn", "msg": "出现「首先/其次」八股连接词，改用小标题切分"})
        if "作为AI" in text or "作为人工智能" in text:
            issues.append({"level": "warn", "msg": "出现「作为AI」开场，删掉直接给内容"})

    return {
        "platform": platform,
        "sub_type": sub_type if platform in ("zhihu", "gzh") else "",
        "length": length,
        "tag_count": tag_count,
        "passed": not issues,
        "issues": issues or [{"level": "ok", "msg": "未发现明显问题"}],
    }


# ---------------------------------------------------------------- 写操作
@tool(
    name="save_content",
    description=(
        "把内容保存为草稿（**不加入发布队列**）。"
        "仅由用户在界面上点击「保存到队列」触发，模型不得主动调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "platform": {
                "type": "string",
                "enum": ["gzh", "xiaohongshu", "douyin", "zhihu"],
                "description": "目标平台",
            },
            "title": {"type": "string", "description": "标题"},
            "body": {"type": "string", "description": "正文内容（结构化 Markdown）"},
            "kind": {
                "type": "string",
                "enum": ["text", "video"],
                "description": "内容类型，默认 text",
            },
            "sub_type": {
                "type": "string",
                "enum": ["answer", "article", "idea"],
                "description": "知乎子形态（仅 platform=zhihu 时有效）",
            },
        },
        "required": ["platform", "title", "body"],
    },
    # 2026-10-16：从「模型可自由调用」改为需确认 —— 之前模型会自作主张入队，
    # 用户根本不知道队列里怎么就多了东西。现在只允许前端「保存到队列」显式触发。
    requires_confirm=True,
)
async def save_content(
    platform: str, title: str, body: str, kind: str = "text", sub_type: str = "",
    conversation_id: str | None = None, ctx: dict | None = None,
):
    """保存内容为草稿（不入队）。

    注意：这里**不再创建 QueueItem**。入队是用户的显式动作，
    由 POST /api/content/items 承载（见 routers/queue.py）。
    """
    ctx = ctx or {}
    db = ctx.get("db")
    owner = ctx.get("owner", "default")
    if db is None:
        return {"ok": False, "error": "数据库未就绪"}

    from app.models.content import Content
    from app.prompts.platforms import normalize_sub_type

    content_id = str(uuid.uuid4())
    db.add(
        Content(
            id=content_id,
            platform=platform,
            kind=kind,
            sub_type=normalize_sub_type(platform, sub_type),
            title=title[:300],
            body=body,
            owner=owner,
            conversation_id=conversation_id or ctx.get("conversation_id"),
            meta={"source": "agent"},
        )
    )
    await db.flush()
    return {"ok": True, "content_id": content_id, "message": "已保存为草稿（尚未入队）"}


# ------------------------------------------------------------ 需确认的工具
@tool(
    name="push_wechat_draft",
    description=(
        "把公众号文章推送到微信公众号草稿箱。"
        "注意：本操作会真实写入微信后台，必须经用户确认后才能执行。"
        "仅支持公众号平台。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "文章标题"},
            "html": {"type": "string", "description": "公众号兼容的 HTML 正文（内联样式）"},
            "digest": {"type": "string", "description": "摘要，120 字内"},
            "content_id": {
                "type": "string",
                "description": "内容 ID（可选）。带上会把正文里的本地配图转存到微信素材库，"
                "并用首图作封面。不带则原样推送。",
            },
            "author": {
                "type": "string",
                "description": "作者名（可选）。写入草稿的 author 字段；留空则回退系统「作者信息」设置里的 author_name。",
            },
            "need_open_comment": {
                "type": "boolean",
                "description": "是否打开留言（可选，默认 true）。留空即打开；显式传 false 才关闭。",
            },
            "account_id": {
                "type": "string",
                "description": "公众号账号 ID（可选）。留空则用默认公众号；一个都没配时回退 .env。",
            },
        },
        "required": ["title", "html"],
    },
    requires_confirm=True,
)
async def push_wechat_draft(
    title: str,
    html: str,
    digest: str = "",
    content_id: str = "",
    author: str = "",
    need_open_comment: bool = True,
    account_id: str = "",
    ctx: dict | None = None,
):
    """推送公众号草稿箱。

    真实实现需要的准备工作：
      1. AppID + AppSecret（公众平台 → 设置与开发 → 基本配置）
         —— 可在「系统状态」页添加公众号，无需改 .env；也兼容 .env 全局凭证
      2. 服务器出网 IP 加入公众号后台的 API IP 白名单（**填沙箱出网 IP，不是 SSH 隧道 IP**）
      3. 正文里的外链图片必须先调 material/uploadimg 上传到微信素材库，
         否则发布后图片会失效

    带 content_id 时：自动把本地配图转存到微信 + 用首图做封面。
    account_id 留空则用默认公众号；一个号都没配时回退 .env。
    """
    ctx = ctx or {}
    db = ctx.get("db")

    client, source = await _resolve_wechat_client(db, account_id, ctx.get("owner", "default"))
    if client is None:
        return {
            "ok": False,
            "error": "未配置微信凭证",
            "hint": "到「系统状态」页添加公众号，或在 .env 里填 WECHAT_APPID / WECHAT_SECRET",
        }

    # 带 content_id：处理图片（转存正文图 + 首图当封面）
    thumb_id = ""
    if content_id and db is not None:
        try:
            from pathlib import Path

            from app.core.config import settings
            from app.models.content import Content

            content = await db.get(Content, content_id)
            if content:
                images = content.images or []
                if content.summary and not digest:
                    digest = content.summary[:120]
                # 1) 正文图片转存
                html = await client.transfer_inline_images(html, images)
                # 2) 首图上传为永久素材，做封面
                if images and images[0].get("path"):
                    first = Path(settings.UPLOAD_DIR) / images[0]["path"]
                    if first.exists():
                        try:
                            thumb_id = await client.upload_thumb(str(first))
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("封面上传失败（草稿仍会创建，可后台补封面）: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("推送前的图片处理失败（继续推送纯文本）: %s", exc)

    # 关注卡二维码转存：二维码属账号级资源，不在 content.images 里，
    # transfer_inline_images 不会处理它 —— 不转存则发布后外链失效。
    if db is not None:
        try:
            from pathlib import Path as _Path

            from app.core.config import settings as _settings
            from app.routers.settings import _qrcode_url as _qr_url

            owner = ctx.get("owner", "default")
            acc = await _resolve_wechat_account(db, account_id, owner)
            if acc is not None and getattr(acc, "qrcode_path", ""):
                local_qr = _Path(_settings.UPLOAD_DIR) / acc.qrcode_path
                qrfname = _Path(acc.qrcode_path).name
                if local_qr.exists() and qrfname:
                    # 二维码 url 同样带 &amp;/ 签名查询串，必须整体替换 src
                    # （复用正文图转存的同一个函数，曾因正则失配静默不转存）
                    from app.services.wechat import _replace_src_by_filename

                    _, hits = _replace_src_by_filename(html, qrfname)
                    if hits:
                        try:
                            wx_url = await client.upload_image(str(local_qr))
                            html, _ = _replace_src_by_filename(html, qrfname, wx_url)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("二维码转存失败（保留原地址）: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("二维码转存流程异常（继续推送）: %s", exc)

    # 微信 draft/add 强制要求封面（thumb_media_id 必填，空串/省略都 40007）。
    # 没有配图时在这里拦下来，给可操作提示；否则用户看到的是微信的
    # 「invalid media_id」，完全不知道问题出在没封面上。
    if not thumb_id:
        return {
            "ok": False,
            "error": "这篇内容还没有封面图，微信要求每篇草稿必须有封面",
            "hint": "先到队列里点「加工排版」生成配图（首图自动用作封面）后再推送",
        }

    # author 字段：前端显式传了就用（排版面板里「作者名」），否则回退系统
    # 「作者信息」设置里的 author_name。微信草稿的 author 为空也能发，所以缺省不报错，
    # 只是草稿不显示作者而已。
    from app.routers.settings import _get_setting

    owner = ctx.get("owner", "default")
    final_author = (author or "").strip()
    if not final_author and db is not None:
        try:
            final_author = (await _get_setting(db, owner, "author_name") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取作者信息失败（草稿 author 留空）: %s", exc)

    result = await client.add_draft(
        title=title,
        html=html,
        digest=digest,
        thumb_media_id=thumb_id,
        author=final_author,
        need_open_comment=need_open_comment,
    )
    logger.info(
        "推送草稿箱: title=%s ok=%s 凭证来源=%s 封面=%s",
        title, result.get("ok"), source, bool(thumb_id),
    )
    return result


@tool(
    name="delete_wechat_draft",
    description=(
        "删除微信公众号草稿箱里的指定草稿。"
        "注意：微信侧删除不可恢复（草稿箱没有回收站），必须经用户确认后才能执行；"
        "删除成功后本地记录会自动恢复为「可推送」状态。仅支持公众号平台。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "media_id": {
                "type": "string",
                "description": "草稿的 media_id（推送成功时返回，队列页「已推送」记录上带有）",
            },
            "content_id": {
                "type": "string",
                "description": "内容 ID（可选）。带上后删除成功会同步清掉该记录的推送状态",
            },
            "account_id": {
                "type": "string",
                "description": "公众号账号 ID（可选）。留空则用默认公众号；一个都没配时回退 .env。",
            },
        },
        "required": ["media_id"],
    },
    requires_confirm=True,
)
async def delete_wechat_draft(
    media_id: str,
    content_id: str = "",
    account_id: str = "",
    ctx: dict | None = None,
):
    """删除公众号草稿箱里的草稿（draft/delete）。

    不可恢复操作，必须前端二次确认。成功后由 execute_confirmed_tool
    按 media_id / content_id 清空队列记录的 wechat_media_id。
    """
    ctx = ctx or {}
    db = ctx.get("db")

    client, source = await _resolve_wechat_client(db, account_id, ctx.get("owner", "default"))
    if client is None:
        return {
            "ok": False,
            "error": "未配置微信凭证",
            "hint": "到「系统状态」页添加公众号，或在 .env 里填 WECHAT_APPID / WECHAT_SECRET",
        }

    result = await client.delete_draft(media_id)
    logger.info(
        "删除草稿: media_id=%s ok=%s 凭证来源=%s",
        media_id, result.get("ok"), source,
    )
    return result


@tool(
    name="render_video",
    description=(
        "把口播脚本渲染成视频（异步任务，返回任务 ID 用于查询进度）。"
        "注意：渲染耗时较长且消耗资源，必须经用户确认后才能执行。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "口播脚本内容"},
            "ratio": {
                "type": "string",
                "enum": ["9:16", "16:9", "1:1"],
                "description": "画幅，默认 9:16 竖屏",
            },
            "voice": {"type": "string", "description": "配音音色，留空则不配音"},
            "subtitle": {"type": "boolean", "description": "是否烧录字幕，默认 true"},
        },
        "required": ["script"],
    },
    requires_confirm=True,
)
async def render_video(
    script: str, ratio: str = "9:16", voice: str = "", subtitle: bool = True, ctx: dict | None = None
):
    """提交视频渲染任务。

    真实实现在 app/services/render.py：
      edge-tts 配音（SentenceBoundary 句级时间轴）+ PIL 字幕卡片 + FFmpeg 运镜合成
    """
    ctx = ctx or {}
    from app.services.render import submit_render_job

    job = await submit_render_job(
        script=script, ratio=ratio, voice=voice, subtitle=subtitle, owner=ctx.get("owner", "default")
    )
    return job
