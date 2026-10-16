"""对话路由：SSE 流式 + 工具确认。"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.prompts.platforms import build_system_prompt
from app.schemas.models import (
    ChatRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationRename,
    MessageOut,
    ToolConfirmRequest,
)
from app.services import conversation as conv_svc
from app.services.agent import execute_confirmed_tool, run_chat
from app.services.llm import get_llm
from app.tools.registry import registry

router = APIRouter(prefix="/chat", tags=["对话"])


def _sse(event: dict) -> str:
    """把事件包装成 SSE 格式。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """流式对话（SSE）。

    前端用 fetch + ReadableStream 接收。事件格式见 docs/API.md。
    """

    # 取或建会话
    if req.conversation_id:
        conv = await conv_svc.get_conversation(db, req.conversation_id, owner)
        if not conv:
            raise HTTPException(404, "会话不存在")
    else:
        conv = await conv_svc.create_conversation(
            db, owner=owner, title=req.message[:30] or "新对话", scene=req.platform or "chat"
        )

    # 用户消息落库
    await conv_svc.add_message(db, conv.id, "user", req.message)
    await db.commit()

    async def gen() -> AsyncGenerator[str, None]:
        # 先告诉前端会话 ID（新建会话时前端需要）
        yield _sse({"type": "conversation", "conversation_id": conv.id})

        llm = get_llm()
        # 长对话先压缩历史
        await conv_svc.maybe_summarize(db, conv, llm)

        # 构造上下文
        system_prompt = build_system_prompt(
            req.platform, topic=req.topic, audience=req.audience
        )
        messages = await conv_svc.build_context(db, conv, system_prompt)

        ctx = {
            "db": db,
            "owner": owner,
            "conversation_id": conv.id,
            "scene": req.platform or conv.scene or "chat",
        }

        # 本轮 assistant 消息的累积器。全部攒着，流结束后一次性落库 ——
        # 思维链（reasoning）和工具痕迹（tool_trace）都要跟着这条回复一起存，
        # 否则历史回显只剩干巴巴的正文。
        collected_text = ""
        collected_reasoning = ""
        # 展示型工具痕迹：[{id, name, arguments, result, status}]，按发生顺序
        tool_trace: list[dict] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        tools_used: list[str] = []

        async for event in run_chat(db, ctx, messages):
            etype = event["type"]

            if etype == "delta":
                collected_text += event["content"]
            elif etype == "reasoning":
                # 思维链：单独累积，不计入正文，也不回灌模型上下文
                collected_reasoning += event["content"]
            elif etype == "usage":
                # usage 是 llm.Usage dataclass，不是 dict。两种都兼容一下，
                # 免得以后改回 dict 又炸一遍。
                u = event["usage"]
                if isinstance(u, dict):
                    usage["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
                    usage["completion_tokens"] += u.get("completion_tokens", 0) or 0
                else:
                    usage["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                    usage["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
                # 对外仍以 dict 形式下发，前端格式不变
                event = {
                    "type": "usage",
                    "usage": {
                        "prompt_tokens": usage["prompt_tokens"],
                        "completion_tokens": usage["completion_tokens"],
                        "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"],
                    },
                }
            elif etype == "tool_call":
                tools_used.append(event["name"])
                tool_trace.append(
                    {
                        "id": event["id"],
                        "name": event["name"],
                        "arguments": event.get("arguments") or {},
                        "status": "running",
                    }
                )
            elif etype == "tool_result":
                rec = next((t for t in tool_trace if t["id"] == event["id"]), None)
                if rec:
                    rec["result"] = event.get("result") or {}
                    rec["status"] = "error" if event["result"].get("error") else "done"
            elif etype == "tool_confirm":
                # 危险工具：先把「到目前为止」的这条回复落库，等用户确认后由
                # /chat/confirm 把结果回填到同一行的 tool_calls.calls 里。
                # 落完必须重置累积器，否则流尾会再插一条内容相同的 assistant。
                await conv_svc.add_message(
                    db,
                    conv.id,
                    "assistant",
                    collected_text,
                    reasoning=collected_reasoning,
                    tool_calls={
                        "calls": tool_trace
                        + [
                            {
                                "id": event["id"],
                                "name": event["name"],
                                "arguments": event.get("arguments") or {},
                                "status": "pending",
                            }
                        ]
                    },
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    msg_id=req.assistant_msg_id,
                )
                await db.commit()
                collected_text = ""
                collected_reasoning = ""
                tool_trace = []

            yield _sse(event)

        # 正文落库（含思维链与工具痕迹）。
        # 注意：没有正文但有工具调用时也要落行 —— 那种回复整条都是工具动作。
        if collected_text or collected_reasoning or tool_trace:
            await conv_svc.add_message(
                db,
                conv.id,
                "assistant",
                collected_text,
                reasoning=collected_reasoning,
                tool_calls={"calls": tool_trace} if tool_trace else None,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                msg_id=req.assistant_msg_id,
            )
            await db.commit()

        logger.info(
            "对话完成 conv=%s tokens=%s/%s tools=%s",
            conv.id,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            tools_used,
        )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 下关闭缓冲，否则流式会被攒批
        },
    )


@router.post("/confirm", tags=["对话"])
async def confirm_tool(
    req: ToolConfirmRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """用户确认后执行危险工具。

    `conversation_id` 可选：对话页确认时传真实会话 id（结果回写消息流）；
    队列页 / 加工面板的「推送草稿」不来自任何对话，不传即可——不能塞占位
    字符串（如旧前端的 `"queue"`），否则会在这里被当成真实会话查 → 404。
    """
    conv = None
    if req.conversation_id:
        conv = await conv_svc.get_conversation(db, req.conversation_id, owner)
        if not conv:
            raise HTTPException(404, "会话不存在")

    if not registry.needs_confirm(req.tool_name):
        raise HTTPException(400, "该工具无需确认，请直接通过对话触发")

    # 无会话时 conversation_id 置 None：下游（usage 统计 / Content 落库）都用
    # `.get()` 取值且列可空，无会话的推送不计入任何会话，属预期。
    ctx = {"db": db, "owner": owner, "conversation_id": conv.id if conv else None}
    result = await execute_confirmed_tool(db, ctx, req.tool_name, req.arguments)

    if conv:
        # 结果回填到「发起这次调用」的那条 assistant 消息里，而不是另起一行
        # role="tool" —— 工具痕迹要跟着所属回复走，历史回显才连得上。
        target = await conv_svc.find_assistant_with_tool_call(
            db, conv.id, req.tool_call_id
        )
        if target and target.tool_calls:
            calls = [dict(c) for c in target.tool_calls.get("calls") or []]
            for c in calls:
                if c.get("id") == req.tool_call_id:
                    c["result"] = result
                    c["status"] = "error" if result.get("error") else "done"
            # JSON 列原地改嵌套 dict 不会标记 dirty，必须整体重新赋值
            target.tool_calls = {"calls": calls}
            await db.flush()
    await db.commit()
    return {"ok": True, "result": result}


# ---------------------------------------------------------------- 会话管理
@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    return await conv_svc.list_conversations(db, owner)


@router.post("/conversations", response_model=ConversationOut)
async def create_conversation(
    req: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    conv = await conv_svc.create_conversation(db, owner, req.title, req.scene)
    await db.commit()
    return conv


@router.get("/conversations/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    conv = await conv_svc.get_conversation(db, conv_id, owner)
    if not conv:
        raise HTTPException(404, "会话不存在")
    msgs = await conv_svc.list_messages(db, conv_id)
    return ConversationDetail(
        **ConversationOut.model_validate(conv).model_dump(),
        messages=[MessageOut.model_validate(m) for m in msgs],
    )


@router.patch("/conversations/{conv_id}", response_model=ConversationOut, tags=["会话"])
async def rename_conversation(
    conv_id: str,
    req: ConversationRename,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """重命名会话标题。"""
    conv = await conv_svc.rename_conversation(db, conv_id, owner, req.title)
    if not conv:
        raise HTTPException(404, "会话不存在")
    await db.commit()
    return conv


@router.delete("/conversations/{conv_id}", tags=["会话"])
async def delete_conversation(
    conv_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    ok = await conv_svc.delete_conversation(db, conv_id, owner)
    if not ok:
        raise HTTPException(404, "会话不存在")
    await db.commit()
    return {"ok": True}
