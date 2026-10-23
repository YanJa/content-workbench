"""Agent 编排层。

这才是「AI Agent 嵌入框架」的核心。不是自主 Agent，而是
「带工具调用的工作流」——路径可控、可调试、可预测。

一轮对话的完整链路：
    用户消息
      → 写库
      → 构造上下文（system + 摘要 + 最近N轮）
      → 调模型（带 tools 定义）
      → 若模型要调工具：
           ├─ 安全工具 → 直接执行 → 结果喂回模型 → 继续
           └─ 危险工具 → 暂停，向前端发确认请求 → 用户确认后执行 → 继续
      → 流式返回文本
      → 落库
"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.services.llm import get_llm
from app.services.model_pool import resolve_for
from app.tools.registry import registry

# 单轮对话里最多允许的"模型→工具→模型"循环次数，防止失控
MAX_TOOL_ROUNDS = 5

# 工具结果回传给模型时的最大字符数。
# 工具结果可能是很长的正文，整段塞回去会迅速烧掉上下文和钱。
_TOOL_RESULT_LIMIT = 2000


def _call_fingerprint(name: str, arguments: dict[str, Any]) -> str:
    """给一次工具调用打指纹，用于识别「完全重复」的调用。

    sort_keys 保证参数顺序不同但内容相同的调用被视为同一个。
    """
    try:
        payload = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        payload = repr(arguments)
    return f"{name}::{payload}"


def _tool_result_for_llm(name: str, result: dict[str, Any]) -> str:
    """把工具结果裁剪成适合回传模型的紧凑 JSON。

    保留 ok / error / content_id / message 这些模型判断下一步真正需要的字段，
    冗长的正文和列表做截断与摘要。
    """
    keep = {"ok", "error", "message", "content_id", "note", "count", "job_id", "status"}
    compact: dict[str, Any] = {}
    for k, v in (result or {}).items():
        if k in keep:
            compact[k] = v
        elif isinstance(v, (list, tuple)):
            compact[k] = f"<{len(v)} 项，已省略>"
        elif isinstance(v, str):
            compact[k] = v[:_TOOL_RESULT_LIMIT] + ("…（已截断）" if len(v) > _TOOL_RESULT_LIMIT else "")
        else:
            compact[k] = v

    raw = json.dumps(compact, ensure_ascii=False)
    if len(raw) > _TOOL_RESULT_LIMIT:
        raw = raw[:_TOOL_RESULT_LIMIT] + "…（已截断）"
    logger.debug("工具 %s 结果回传模型 %d 字符", name, len(raw))
    return raw


def _strip_tool_traces(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剥掉消息列表里的工具痕迹，供不支持 function calling 的模型用。

    build_context 对「带 tool_calls 的 assistant 历史行」会展开成：
      assistant(content="", tool_calls=[...]) + role:"tool"(tool_call_id=...)
    OpenAI 系模型认这对结构；llama 等模型收到 role:tool 直接 400。
    处理规则：
      - role == "tool" 的消息：丢（纯工具结果，对无工具模型无意义）
      - assistant 带 tool_calls 且有正文：保留为普通 assistant 文本消息
      - assistant 带 tool_calls 但无正文：丢（那一轮整条都是工具动作）
      - 其余消息原样保留（system / user / 普通 assistant）
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant" and m.get("tool_calls"):
            if m.get("content"):
                out.append({"role": "assistant", "content": m["content"]})
            continue
        out.append(m)
    return out


async def run_chat(
    db: AsyncSession,
    ctx: dict[str, Any],
    messages: list[dict[str, Any]],
    task: str = "copywriting",
) -> AsyncGenerator[dict[str, Any], None]:
    """执行一轮对话，产出结构化事件流。

    事件类型（与前端约定，见 docs/API.md）：
      {"type":"delta",     "content":"..."}                  文本增量
      {"type":"tool_call", "id":..,"name":..,"arguments":{}}  工具调用开始
      {"type":"tool_result","id":..,"name":..,"result":{}}    工具执行结果
      {"type":"tool_confirm","id":..,"name":..,"arguments":{}} 需要人工确认
      {"type":"usage",     "usage":{...}}
      {"type":"done",      "finish_reason":"..."}
      {"type":"error",     "message":"..."}
    """
    from app.services.usage import make_sink

    llm = get_llm()
    # 模型池：整条回答只解析一次模型。
    # 每轮工具循环都重新解析的话，可能第 1 轮用模型 A、第 3 轮 A 的额度刚好
    # 耗尽换成 B —— 同一条回答里风格突变，用户会很困惑。
    spec = await resolve_for(ctx.get("feature") or "chat", ctx.get("owner"), ctx.get("db"))
    # schemas_for(ctx)：动态工具（activate_skill）的 enum 要按本次 owner 算，
    # 静态的 schemas() 只会给出一个 enum 为空的空壳。
    tools = await registry.schemas_for(ctx)
    # 对话供应商：deepseek（默认，全功能）/ cloudflare（Workers AI，见下）
    provider = ctx.get("provider") or "deepseek"
    model_override = ctx.get("model_override") or None
    convo = list(messages)
    if spec is not None and not spec.supports_tools or provider == "cloudflare":
        # CF 的 llama 系模型不支持 function calling：① 不带 tools（工具循环
        # 自然不会触发，pending_tools 恒为空）；② 历史消息里的工具痕迹必须剥掉
        # —— build_context 会把带 tool_calls 的 assistant 行展开成
        # 「assistant(tool_calls) + role:tool」消息对，llama 收到会 400。
        # 有正文的工具轮次保留正文（那是有效对话内容），纯工具轮次整条丢弃。
        tools = None
        convo = _strip_tool_traces(convo)
    sink = make_sink(
        db,
        conversation_id=ctx.get("conversation_id"),
        owner=ctx.get("owner") or "default",
        scene=ctx.get("scene") or "chat",
    )
    # 记录本轮已执行过的「工具+参数」指纹 → 结果，用于拦截完全重复的调用
    executed_calls: dict[str, dict[str, Any]] = {}

    for round_no in range(MAX_TOOL_ROUNDS):
        text_buffer = ""
        pending_tools: list[dict[str, Any]] = []
        finish_reason = ""

        async for evt in llm.stream_call(
            convo,
            task=task,
            tools=tools or None,
            usage_sink=sink,
            provider=provider,
            model_override=model_override,
            feature=ctx.get("feature") or "chat",
            owner=ctx.get("owner"),
            preferred=spec,
        ):
            etype = evt["type"]

            if etype == "delta":
                text_buffer += evt["content"]
                yield evt

            elif etype == "reasoning":
                # 思维链：透传给前端做「正在思考」的展示，不计入正文
                yield evt

            elif etype == "tool_calls":
                pending_tools = evt["tool_calls"]

            elif etype == "usage":
                yield evt

            elif etype == "finish":
                finish_reason = evt.get("finish_reason", "")

            elif etype == "error":
                yield evt
                return

            else:
                # 额度预警 / 模型切换这类展示型事件直接透传，别被这层吞掉
                yield evt

        # 没有工具调用 → 本轮结束
        if not pending_tools:
            yield {"type": "done", "finish_reason": finish_reason or "stop"}
            return

        # 把 assistant 的工具调用意向写进上下文
        convo.append(
            {
                "role": "assistant",
                "content": text_buffer or "",
                "tool_calls": [
                    {
                        "id": t["id"],
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "arguments": json.dumps(t["arguments"], ensure_ascii=False),
                        },
                    }
                    for t in pending_tools
                ],
            }
        )

        executed_any = False
        for t in pending_tools:
            yield {
                "type": "tool_call",
                "id": t["id"],
                "name": t["name"],
                "arguments": t["arguments"],
            }

            # 危险工具：不执行，交给前端确认
            if registry.needs_confirm(t["name"]):
                logger.info("工具 %s 需要人工确认，暂停执行", t["name"])
                yield {
                    "type": "tool_confirm",
                    "id": t["id"],
                    "name": t["name"],
                    "arguments": t["arguments"],
                    "message": f"该操作会影响外部系统（{t['name']}），请确认后执行",
                }
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": t["id"],
                        "content": json.dumps(
                            {"status": "pending_user_confirmation"}, ensure_ascii=False
                        ),
                    }
                )
                continue

            # 相同工具 + 相同参数已经执行过 → 直接回放上次结果，不再真的执行。
            # 模型在长上下文里容易忘记自己调过，会反复重试（实测：3 条标题触发
            # 10 次 check_compliance，撞上 MAX_TOOL_ROUNDS 被中止）。
            # 这里阻断的是「完全重复」的调用，参数有任何变化仍会正常执行。
            fingerprint = _call_fingerprint(t["name"], t["arguments"])
            if fingerprint in executed_calls:
                logger.info("工具 %s 参数与上次相同，回放缓存结果", t["name"])
                result = executed_calls[fingerprint]
                yield {
                    "type": "tool_result",
                    "id": t["id"],
                    "name": t["name"],
                    "result": {**result, "_cached": True},
                }
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": t["id"],
                        "content": _tool_result_for_llm(t["name"], result)
                        + "\n（该调用与上一次参数完全相同，已复用上次结果，请勿重复调用）",
                    }
                )
                continue

            result = await registry.execute(t["name"], t["arguments"], ctx)
            executed_calls[fingerprint] = result
            executed_any = True
            yield {
                "type": "tool_result",
                "id": t["id"],
                "name": t["name"],
                "result": result,
            }
            # 工具结果必须回传给模型。
            # 不回传的话，模型不知道上一轮干了什么，会反复调同一个工具
            # （实测：save_content 被连续调用 3 次，队列里出现 3 条重复内容）。
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": t["id"],
                    "content": _tool_result_for_llm(t["name"], result),
                }
            )

        # 若全部是待确认工具，本轮到此为止
        if not executed_any:
            yield {"type": "done", "finish_reason": "awaiting_confirmation"}
            return

    yield {
        "type": "error",
        "message": f"工具调用轮次超过上限（{MAX_TOOL_ROUNDS}），已中止",
    }


async def execute_confirmed_tool(
    db: AsyncSession, ctx: dict[str, Any], tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """执行用户已确认的工具。"""
    logger.info("执行已确认工具: %s", tool_name)
    result = await registry.execute(tool_name, arguments, ctx)

    # 推送草稿箱成功后，把 media_id 回填到「被推送的那条」队列项上。
    # 必须按 arguments.content_id 定位：前端（队列页 / 加工面板）推送时都带
    # content_id，那才是本次推送的记录。早期按「最新创建」回填会把 media_id
    # 写到别的记录上——被推送的记录仍显示可推送（会被二次推送），别的记录
    # 反而被误标「已推送」。没带 content_id 的老调用才兜底用最新创建。
    if tool_name == "push_wechat_draft" and result.get("ok") and result.get("media_id"):
        try:
            from sqlalchemy import select

            from app.models.content import QueueItem

            owner = ctx.get("owner", "default")
            content_id = (arguments or {}).get("content_id")
            items = []
            if content_id:
                stmt = select(QueueItem).where(
                    QueueItem.owner == owner,
                    QueueItem.content_id == content_id,
                )
                items = list((await db.execute(stmt)).scalars().all())
            else:
                stmt = (
                    select(QueueItem)
                    .where(QueueItem.owner == owner)
                    .order_by(QueueItem.created_at.desc())
                    .limit(1)
                )
                item = (await db.execute(stmt)).scalar_one_or_none()
                if item:
                    items = [item]
            for item in items:
                item.wechat_media_id = result["media_id"]
            if items:
                await db.flush()
                logger.info(
                    "回填 media_id：content_id=%s 命中 %d 条队列项", content_id, len(items)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("回填 media_id 失败: %s", exc)

    # 删除草稿成功后，清掉本地记录的推送标记（镜像上面的回填逻辑）。
    # 不清的后果：微信侧草稿已不存在，本地仍显示「已推送」——
    # 用户看不到推送按钮（会以为已群发），也不能重新推送修正。
    # 按 media_id 定位为主、content_id 兜底：两种参数前端至少带一种。
    #
    # 40007（media_id 无效）也清：微信明确说这个草稿不存在了（用户在后台
    # 删过 / 过期清理），本地标记已是陈旧信息，继续挂着只会挡住推送按钮。
    # 2026-10-21 实测遇到过：4 个旧草稿在微信后台被外部删除后，本地行全部
    # 卡在「已推送」且无解，只能来硬的。
    if tool_name == "delete_wechat_draft" and (
        result.get("ok") or result.get("errcode") == 40007
    ):
        try:
            from sqlalchemy import or_, select

            from app.models.content import QueueItem

            owner = ctx.get("owner", "default")
            args = arguments or {}
            media_id = args.get("media_id") or result.get("media_id")
            content_id = args.get("content_id")
            conditions = []
            if media_id:
                conditions.append(QueueItem.wechat_media_id == media_id)
            if content_id:
                conditions.append(QueueItem.content_id == content_id)
            items = []
            if conditions:
                stmt = select(QueueItem).where(
                    QueueItem.owner == owner,
                    or_(*conditions),
                )
                items = list((await db.execute(stmt)).scalars().all())
            for item in items:
                item.wechat_media_id = None
            if items:
                await db.flush()
                logger.info(
                    "清除推送标记：media_id=%s content_id=%s 命中 %d 条（ok=%s errcode=%s）",
                    media_id, content_id, len(items),
                    result.get("ok"), result.get("errcode"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("清除推送标记失败: %s", exc)

    return result
