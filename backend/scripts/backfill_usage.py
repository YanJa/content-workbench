"""历史用量回填：把 messages 表里已有的 token 数据搬进 llm_usage。

用法：
    python -m scripts.backfill_usage                 # 预演，只打印不写库
    python -m scripts.backfill_usage --apply          # 真正写入
    python -m scripts.backfill_usage --apply --force  # 忽略"已回填过"的保护，重跑

为什么需要它：
    埋点是新加的，之前跑出来的对话只在 messages 上留了 prompt/completion 两个数字，
    没有模型名、没有缓存命中、没有峰谷标记，算不出钱。这个脚本把它们补成
    可计价的记录，并打上 estimated=True —— 意思是「这些数字是回填的估算，
    不是实时埋点采到的」，前端要显式标注，别拿它对账。

回填口径（重要，别乱改）：
  - 模型：统一按 deepseek-flash（历史数据没有模型字段，而绝大多数调用走的是
    copywriting/lightweight 路由，本来就是 Flash）
  - cached_tokens = 0：没采到就当全部未命中，这是**保守偏高**的估法，
    宁可估多一点也别低估成本
  - is_peak：按消息 created_at 当时的时段算，不是按"现在"
  - scene：取会话的 scene
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.pricing import estimate_cost  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.usage import LlmUsage  # noqa: E402
from app.services import usage as usage_svc  # noqa: E402

BACKFILL_MODEL = "deepseek-flash"


async def main(apply: bool, force: bool) -> int:
    await init_db()

    async with SessionLocal() as db:
        # 已经回填过就别重复跑，否则同一条消息会记两遍钱
        existing = await db.execute(select(func.count()).select_from(LlmUsage))
        already = existing.scalar() or 0
        if already and not force:
            print(f"llm_usage 表里已有 {already} 行，默认跳过。")
            print("确认要重跑请先清空该表，或加 --force。")
            return 1

        rows = (
            await db.execute(
                select(Message, Conversation.scene, Conversation.owner).join(
                    Conversation, Conversation.id == Message.conversation_id
                ).where(Message.role == "assistant")
            )
        ).all()

        todo = [
            (m, scene, owner)
            for (m, scene, owner) in rows
            if (m.prompt_tokens or 0) + (m.completion_tokens or 0) > 0
        ]

        print(f"扫描到 assistant 消息 {len(rows)} 条，其中有 token 数据 {len(todo)} 条。")

        total_cost = 0.0
        for msg, scene, owner in todo:
            cost = estimate_cost(
                BACKFILL_MODEL,
                msg.prompt_tokens,
                msg.completion_tokens,
                0,
                ts=msg.created_at,
            )
            total_cost += cost
            if apply:
                await usage_svc.record(
                    db,
                    model=BACKFILL_MODEL,
                    prompt_tokens=msg.prompt_tokens or 0,
                    completion_tokens=msg.completion_tokens or 0,
                    cached_tokens=0,
                    task_type="backfill",
                    conversation_id=msg.conversation_id,
                    message_id=msg.id,
                    owner=owner or "default",
                    scene=scene or "chat",
                    is_summary=False,
                    estimated=True,
                    created_at=msg.created_at,
                )

        if apply:
            await db.commit()

        verb = "已写入" if apply else "预演（未写入）"
        print(f"{verb} {len(todo)} 行，合计估算费用 ¥{total_cost:.4f}")
        if not apply:
            print("确认无误后加 --apply 真正写入。")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="回填历史 AI 用量")
    parser.add_argument("--apply", action="store_true", help="真正写入数据库")
    parser.add_argument("--force", action="store_true", help="已有数据时也强制执行")
    args = parser.parse_args()
    print(f"数据库: {settings.DATABASE_URL}")
    raise SystemExit(asyncio.run(main(args.apply, args.force)))
