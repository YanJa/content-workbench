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
    tools = registry.schemas()
    convo = list(messages)
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

        async for evt in llm.stream_call(convo, task=task, tools=tools or None, usage_sink=sink):
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

    # 推送草稿箱成功后，回填 media_id
    if tool_name == "push_wechat_draft" and result.get("ok") and result.get("media_id"):
        try:
            from sqlalchemy import select

            from app.models.content import QueueItem

            stmt = (
                select(QueueItem)
                .where(QueueItem.owner == ctx.get("owner", "default"))
                .order_by(QueueItem.created_at.desc())
                .limit(1)
            )
            item = (await db.execute(stmt)).scalar_one_or_none()
            if item:
                item.wechat_media_id = result["media_id"]
                await db.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("回填 media_id 失败: %s", exc)

    return result
