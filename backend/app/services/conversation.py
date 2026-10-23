"""会话服务：上下文管理 + 消息持久化 + 摘要压缩。"""
from __future__ import annotations

import json
import uuid
import time
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.models.conversation import Conversation, Message

# 知识库素材的注入头。措辞是反复权衡过的：
#  - 「仅供本次写稿参考」划清边界，避免模型把素材当用户指令去执行；
#  - 「不要逐字照抄」防洗稿式复述，也防素材里的旧数据被当成事实硬写；
#  - 「不要提及本段存在」避免模型在成稿里冒出「根据你提供的素材1」这种话；
#  - 「与问题无关则忽略」防止素材反过来带偏本轮意图（召回是模糊匹配，
#    阈值之上不代表真的相关）。
KNOWLEDGE_HEADER = (
    "【知识库素材】以下片段来自用户的私有知识库，仅供本次写稿参考。"
    "相关时参考其中的事实、术语与行文风格；不要逐字照抄，不要在成稿里标注"
    "或向用户提及这些素材的存在。与本次问题无关则直接忽略。"
)


async def create_conversation(
    db: AsyncSession, owner: str, title: str = "新对话", scene: str = "chat"
) -> Conversation:
    conv = Conversation(id=str(uuid.uuid4()), owner=owner, title=title[:200], scene=scene)
    db.add(conv)
    await db.flush()
    return conv


async def get_conversation(db: AsyncSession, conv_id: str, owner: str) -> Conversation | None:
    stmt = select(Conversation).where(
        Conversation.id == conv_id, Conversation.owner == owner
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_conversations(db: AsyncSession, owner: str, limit: int = 50) -> list[Conversation]:
    stmt = (
        select(Conversation)
        .where(Conversation.owner == owner)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_messages(db: AsyncSession, conv_id: str) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def add_message(
    db: AsyncSession,
    conv_id: str,
    role: str,
    content: str = "",
    *,
    tool_calls: dict | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning: str = "",
    error: str = "",
    msg_id: str | None = None,
) -> Message:
    """落一条消息。

    `msg_id`：调用方指定主键。对话流让前端预生成的 assistant 消息 id 当主键，
    这样「保存到队列」上报的 msg_id 与历史消息 id 是同一个值，队列页才能
    反跳定位到那条回复（否则一个是客户端 uid、一个是库内 uuid，永远对不上）。

    `error`：模型调用失败的错误信息。失败轮次可能一个字都没吐出来，
    这时 content/reasoning/tool_calls 全空，只有 error —— 调用方靠
    「四者任一非空就落行」的判据把这种占位行也写进库，历史回显才看得到。
    """
    msg = Message(
        id=msg_id or str(uuid.uuid4()),
        conversation_id=conv_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning=reasoning,
        error=error,
    )
    db.add(msg)
    # 更新会话活跃时间
    conv = await db.get(Conversation, conv_id)
    if conv:
        conv.updated_at = int(time.time())
    await db.flush()
    return msg


async def find_assistant_with_tool_call(
    db: AsyncSession, conv_id: str, tool_call_id: str
) -> Message | None:
    """找会话里携带指定 tool_call_id 的那条 assistant 消息。

    用于 /chat/confirm 把「用户已确认执行」的结果回填进去（而不是另起一行
    role="tool" 消息）—— 工具痕迹要跟着所属的那条回复一起走，回显才连得上。
    """
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conv_id,
            Message.role == "assistant",
            Message.tool_calls.isnot(None),
        )
        .order_by(Message.created_at.desc())
    )
    for m in (await db.execute(stmt)).scalars().all():
        for c in (m.tool_calls or {}).get("calls") or []:
            if c.get("id") == tool_call_id:
                return m
    return None


async def rename_conversation(
    db: AsyncSession, conv_id: str, owner: str, title: str
) -> Conversation | None:
    """重命名会话。返回 None 表示会话不存在或不属于该 owner。

    空标题回落到「新对话」，避免出现标题为空白的会话项。
    updated_at 由模型的 onupdate 自动刷新，无需手动赋值。
    """
    conv = await get_conversation(db, conv_id, owner)
    if not conv:
        return None
    conv.title = title.strip() or "新对话"
    await db.flush()
    return conv


async def delete_conversation(db: AsyncSession, conv_id: str, owner: str) -> bool:
    conv = await get_conversation(db, conv_id, owner)
    if not conv:
        return False
    await db.delete(conv)
    await db.flush()
    return True


def _tool_calls_to_context(tool_calls: dict | None) -> list[dict[str, Any]]:
    """把 assistant 行里的展示型 tool_calls 展开成模型可用的上下文消息。

    库里存的是展示结构 `{"calls": [{id, name, arguments, result, status}]}`
    （给人看的）；发给模型必须转成 OpenAI 的 tool_calls + 逐条 tool 结果，
    两者结构不同，**绝不能把展示结构直接喂模型**。

    pending（等用户确认、还没执行）的调用用占位结果，保证
    「assistant 带 tool_calls → tool 结果」成对出现，否则部分 provider 会报错。
    """
    calls = (tool_calls or {}).get("calls") or []
    out: list[dict[str, Any]] = []
    for c in calls:
        cid = c.get("id") or ""
        name = c.get("name") or ""
        args = c.get("arguments") or {}
        try:
            args_text = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            args_text = "{}"
        out.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": name, "arguments": args_text},
                    }
                ],
            }
        )
        if c.get("status") == "pending":
            result_text = json.dumps(
                {"status": "pending_user_confirmation"}, ensure_ascii=False
            )
        else:
            try:
                result_text = json.dumps(c.get("result") or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                result_text = "{}"
        out.append({"role": "tool", "tool_call_id": cid, "content": result_text})
    return out


async def build_context(
    db: AsyncSession,
    conv: Conversation,
    system_prompt: str,
    knowledge: str | None = None,
    skills: str | None = None,
) -> list[dict[str, Any]]:
    """构造发给模型的消息列表。

    上下文策略：
      [system] + [历史摘要（若有）] + [技能清单（若有）] + [知识库素材（若有）] + [最近 N 轮原文]

    为什么不无脑全塞：虽然有 1M 上下文，但输入 token 是要花钱的。
    保留最近 N 轮原文 + 更早内容压缩成摘要，是成本与效果的平衡点。

    注意 reasoning（思维链）**不进上下文**：那是给人看的推理过程，
    回灌既白烧 token，又有 provider 兼容风险。

    `knowledge` 是已拼好的素材文本（拼装见 `services/knowledge.build_snippets`），
    `skills` 是已拼好的技能清单（拼装见 `services/skill.build_catalog`），
    这里只负责塞进去。**两者都必须是独立 system 消息，不能拼进 system_prompt**：
    `prompts.platforms.strip_tool_discipline()` 会从「工具使用纪律」标记一刀切，
    拼在 system_prompt 尾部的素材在 CF 通道会被整段丢弃（2026-10-21 实测确认）。

    技能清单排在知识库之前：它是「怎么做」的方法论，优先级高于「参考什么」；
    而且越靠前越容易被当成背景设定，而不是刚说过的话。
    """
    msgs = await list_messages(db, conv.id)

    history = [m for m in msgs if m.role in ("user", "assistant")]
    recent = history[-settings.CONTEXT_WINDOW_TURNS * 2 :] if history else []

    result: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    # 更早的内容用摘要代替
    if conv.summary:
        result.append(
            {"role": "system", "content": f"【此前对话摘要】\n{conv.summary}"}
        )

    # 技能清单：独立 system 消息，理由同上（见 build_context 文档串）
    if skills:
        result.append({"role": "system", "content": skills})

    # 知识库素材：独立一条 system 消息，紧跟摘要、在最近 N 轮之前 ——
    # 越靠前越容易被模型当作「背景设定」而不是「刚说过的话」。
    if knowledge:
        result.append(
            {"role": "system", "content": f"{KNOWLEDGE_HEADER}\n\n{knowledge}"}
        )

    for m in recent:
        if m.role == "assistant" and m.tool_calls:
            # 一条 assistant 行可能带多次工具调用：正文（若有）+ 展开的调用/结果
            if m.content:
                result.append({"role": "assistant", "content": m.content})
            result.extend(_tool_calls_to_context(m.tool_calls))
        elif m.role == "assistant" and not m.content:
            # 纯错误占位行（模型调用失败、一个字没吐出来）不进上下文：
            # 空正文的 assistant 消息对模型没有信息量，部分 provider 还会
            # 因为空 content 报错。给人看的错误提示走 Message.error 回显。
            continue
        else:
            result.append({"role": m.role, "content": m.content})

    return result


async def maybe_summarize(db: AsyncSession, conv: Conversation, llm: Any) -> None:
    """对话过长时压缩历史，控制 token 消耗。"""
    msgs = await list_messages(db, conv.id)
    if len(msgs) <= settings.CONTEXT_SUMMARY_THRESHOLD * 2:
        return

    # 把最老的一半压成摘要
    older = msgs[: len(msgs) // 2]
    text = "\n".join(f"{m.role}: {m.content[:500]}" for m in older if m.content)

    from app.prompts.platforms import SUMMARY_PROMPT
    from app.services.usage import make_sink

    try:
        result = await llm.chat(
            [{"role": "user", "content": SUMMARY_PROMPT.format(content=text)}],
            task="lightweight",
            feature="summary",
            owner=conv.owner or "default",
            # 这一笔之前完全没记账：压缩历史也是真金白银的 LLM 调用
            usage_sink=make_sink(
                db,
                conversation_id=conv.id,
                owner=conv.owner or "default",
                scene="summary",
                is_summary=True,
            ),
        )
        conv.summary = (conv.summary + "\n" + result.content).strip()[-1500:]
        await db.flush()
        logger.info("会话 %s 已压缩历史摘要", conv.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("摘要生成失败，跳过: %s", exc)
