"""对话路由：SSE 流式 + 工具确认。"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.core import quota as quota_mod
from app.core.rbac import TOOL_PERM, load_permissions, require_use
from app.core.tenant import current_owner_id, current_user
from app.db.session import SessionLocal, get_db
from app.models.settings import AppSetting
from app.models.user import User
from app.prompts.platforms import build_system_prompt, strip_tool_discipline
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
from app.services import knowledge as kb
from app.services import skill as skill_svc
from app.services.agent import execute_confirmed_tool, run_chat
from app.services.llm import get_llm
from app.tools.registry import registry

router = APIRouter(prefix="/chat", tags=["对话"])


def _sse(event: dict) -> str:
    """把事件包装成 SSE 格式。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _load_chat_provider(db: AsyncSession, owner: str) -> tuple[str, str]:
    """读用户的对话供应商设置。

    返回 (provider, model_override)。provider ∈ deepseek / cloudflare；
    model_override 是用户在设置页选的 CF 模型 id（空 = 用 env 默认）。
    设置从未写过时默认 deepseek —— 老用户零感知。
    """
    rows = (
        await db.execute(
            select(AppSetting).where(
                AppSetting.owner == owner,
                AppSetting.key.in_(("chat_provider", "cloudflare_chat_model")),
            )
        )
    ).scalars().all()
    kv = {r.key: (r.value or "") for r in rows}
    provider = kv.get("chat_provider") or "deepseek"
    if provider not in ("deepseek", "cloudflare"):
        provider = "deepseek"
    return provider, kv.get("cloudflare_chat_model") or ""


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("chat.send", "chat")),
):
    """流式对话（SSE）。

    前端用 fetch + ReadableStream 接收。事件格式见 docs/API.md。

    权限与配额挂成依赖而不是写在函数体里：**SSE 一旦返回 200 就改不了状态
    码了**，配额超限必须在 StreamingResponse 之前拦下来。放在依赖里由
    FastAPI 保证它在函数体执行前跑完。
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
        provider, cf_model = await _load_chat_provider(db, owner)
        system_prompt = build_system_prompt(
            req.platform, topic=req.topic, audience=req.audience
        )
        if provider == "cloudflare":
            # CF 模型没有工具可用：system 里的「工具使用纪律」段只会干扰它
            system_prompt = strip_tool_discipline(system_prompt)

        # 知识库召回：放在知道 provider 之后，好按通道挑 token 预算
        # （CF 的 8b 模型上下文小得多，给一样的预算会挤爆）。
        # 整体 try/except —— 召回是旁路，任何异常都只能让本轮没素材，
        # 不能让对话直接 500。
        snippets = ""
        kb_hits: list[dict] = []
        if req.use_knowledge is not False:
            budget = (
                settings.KB_MAX_CHARS_CF if provider == "cloudflare" else settings.KB_MAX_CHARS
            )
            t0 = time.time()
            try:
                snippets, kb_hits = await kb.recall_for_prompt(
                    db, owner, req.message, max_chars=budget
                )
                if kb_hits:
                    logger.info(
                        "知识库召回 %d 片 / %.0f 字（%.2fs, %s）",
                        len(kb_hits),
                        len(snippets),
                        time.time() - t0,
                        kb_hits[0].get("match"),
                    )
            except Exception as exc:  # noqa: BLE001 - 旁路降级
                logger.warning("知识库召回失败（本轮无素材继续）：%s", exc)

        # 技能清单（catalog）：和知识库一样是旁路，读失败就本轮无技能。
        # CF 通道不给 —— 它没有 function calling，清单里「调用 activate_skill」
        # 的指令只会让它凭空编造工具调用；而且 8b 模型上下文小，省点预算。
        # use_skills=False 时本轮连工具都不提供，前后端一致。
        skill_catalog = ""
        use_skills = req.use_skills is not False
        if use_skills and provider != "cloudflare":
            skill_catalog = await skill_svc.build_catalog_for_owner(db, owner)

        messages = await conv_svc.build_context(
            db, conv, system_prompt, knowledge=snippets, skills=skill_catalog
        )

        ctx = {
            "db": db,
            "owner": owner,
            "conversation_id": conv.id,
            "scene": req.platform or conv.scene or "chat",
            # 对话供应商（deepseek / cloudflare）+ 用户选的 CF 模型（空=env 默认）。
            # 只作用于对话轮次；摘要压缩（maybe_summarize）固定走 DeepSeek。
            "provider": provider,
            "model_override": cf_model or None,
            # 本轮是否挂技能。run_chat 靠它决定要不要把 activate_skill 放进
            # tools 列表 —— 只靠不注入 catalog 是不够的：catalog 不在，但工具
            # 还在的话，模型仍可能凭 description 去猜一个名字调用。
            "use_skills": use_skills,
        }

        # 本轮 assistant 消息的累积器。全部攒着，流结束后一次性落库 ——
        # 思维链（reasoning）和工具痕迹（tool_trace）都要跟着这条回复一起存，
        # 否则历史回显只剩干巴巴的正文。
        collected_text = ""
        collected_reasoning = ""
        # 模型调用失败的错误信息（SSE 已 200，错误只能以事件推给前端）。
        # 随这条回复落库：失败轮次常一个字都没吐出来，不记就彻底没法回溯。
        collected_error = ""
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
            elif etype == "error":
                # 模型调用失败（凭证失效 / 模型不存在 / 超长 / CF 未配置 …）。
                # 事件照常推给前端（下面的 yield），同时攒下来随这条回复落库。
                # run_chat 收到 error 后立即 return，流到此结束 —— 走到下面的
                # 落库块时四类累积器可能全空，所以判据里必须带上 error。
                collected_error = (event.get("message") or "")[:2000]
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
                collected_error = ""
                tool_trace = []

            yield _sse(event)

        # 正文落库（含思维链与工具痕迹）。
        # 注意：没有正文但有工具调用时也要落行 —— 那种回复整条都是工具动作；
        # 同理，模型调用失败（一个字没吐出来）也要落一行，只带 error 字段，
        # 历史回显时这条失败才看得见（2026-10-23 用户要求：错误要可回溯）。
        if collected_text or collected_reasoning or tool_trace or collected_error:
            await conv_svc.add_message(
                db,
                conv.id,
                "assistant",
                collected_text,
                reasoning=collected_reasoning,
                tool_calls={"calls": tool_trace} if tool_trace else None,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                error=collected_error,
                msg_id=req.assistant_msg_id,
            )
            await db.commit()

        logger.info(
            "对话完成 conv=%s tokens=%s/%s tools=%s error=%s",
            conv.id,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            tools_used,
            collected_error[:120] or None,
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
    user: User = Depends(current_user),
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

    # 权限与配额**按工具名映射**，而不是给这个端点设一个「确认工具」权限点：
    # 一个端点承载 save_content / render_video / push_wechat_draft 三个工具，
    # 设统一的点会造成「有 video.render 却点不了确认」的死锁。
    perm, feature = TOOL_PERM.get(req.tool_name, (None, None))
    if perm:
        await require_use(perm, feature)(user)  # 复用同一个校验+扣减逻辑

    # 无会话时 conversation_id 置 None：下游（usage 统计 / Content 落库）都用
    # `.get()` 取值且列可空，无会话的推送不计入任何会话，属预期。
    ctx = {"db": db, "owner": owner, "conversation_id": conv.id if conv else None}
    result = await execute_confirmed_tool(db, ctx, req.tool_name, req.arguments)

    # 推送失败退还配额：这是唯一「同步可判成败」的受控动作，其余（渲染/排版/
    # 复刻）都是异步任务，资源已真实消耗，不退还。
    if feature == "wechat_push" and result.get("error"):
        async with SessionLocal() as qdb:
            _, quotas = await load_permissions(qdb, user)
        await quota_mod.refund(user.id, "wechat_push", quotas.get("wechat_push"))

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
