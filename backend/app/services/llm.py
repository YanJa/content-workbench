"""大模型客户端。

设计要点：
1. 统一走 OpenAI 兼容接口 → 换模型只需改 base_url / model name
2. 模型路由：按任务类型选模型，不硬编码在业务代码里
3. Prompt 缓存友好：system prompt 固定放最前，命中缓存价格降至 1/50
4. 峰谷感知：可选地把批量任务推迟到低谷时段
5. 容错：主模型失败自动切兜底模型
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import logger

TaskType = Literal["copywriting", "reasoning", "lightweight", "vision"]

# 用量埋点：LLM 层在每次调用结束时把用量推给这个回调。
#
# 为什么埋点放在这一层，而不是各个业务函数里各写一遍：
#   业务侧有 chat 流式、chat 非流式、会话摘要压缩三条路径，只要漏一个就会出现
#   「用量对不上账单」。放在 LLM 层是唯一收敛点 —— 只要调了模型就一定记账。
# sink 由调用方（持有 db session 的那层）注入，LLM 层不碰数据库。
UsageSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ModelRoute:
    model: str
    temperature: float = 0.8
    max_tokens: int = 4096
    reason: str = ""


# 模型路由表：改这里就能全局切换，业务代码不用动。
#
# ⚠️ max_tokens 为什么给这么大：
#   这两个模型都是推理模型，思维链（reasoning_content）和正式回答（content）
#   共用同一个 max_tokens 预算。写一篇 800 字公众号文章，思维链实测要烧
#   1100~2000 tokens。如果 max_tokens 只给 4096，很容易出现
#   「思维链吐完了、预算没了、正文一个字没出」的情况。
#   宁可给宽，模型不会用满；给窄了是硬伤。
MODEL_ROUTES: dict[TaskType, ModelRoute] = {
    "copywriting": ModelRoute(
        model=settings.DEEPSEEK_MODEL_FLASH,
        # 0.85 时约有两成概率跑偏成「写作指南/待填模板」。
        # 0.75 仍保留创意多样性，但稳定性明显提升。
        temperature=0.75,
        max_tokens=16384,
        reason="文案生成：V4.1-Flash 便宜、中文强、1M 上下文；预算需覆盖思维链",
    ),
    "reasoning": ModelRoute(
        model=settings.DEEPSEEK_MODEL_PRO,
        temperature=0.6,
        max_tokens=32768,
        reason="复杂策划：V4-Pro 推理更强，思维链更长",
    ),
    "lightweight": ModelRoute(
        model=settings.DEEPSEEK_MODEL_FLASH,
        temperature=0.3,
        max_tokens=4096,
        reason="轻任务：标题、标签、分类（思维链短）",
    ),
    "vision": ModelRoute(
        model=settings.DEEPSEEK_MODEL_FLASH,
        temperature=0.7,
        max_tokens=16384,
        reason="图片理解：V4.1-Flash 原生多模态",
    ),
}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # DeepSeek 推理模型会把思维链算进 completion_tokens，单独记录便于排查"正文被挤掉"
    reasoning_tokens: int = 0
    # 命中上下文缓存的输入 token：价格只有未命中缓存的 1/50，
    # 是成本优化的主要抓手，必须单独存。
    cached_tokens: int = 0

    @property
    def estimated(self) -> bool:
        return self.total_tokens == 0

    @property
    def miss_tokens(self) -> int:
        """未命中缓存的输入 token（缓存命中部分不算这个价）。"""
        return max(self.prompt_tokens - self.cached_tokens, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "estimated": self.estimated,
        }


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""


def is_peak_hour() -> bool:
    """判断当前是否处于 DeepSeek 高峰计费时段。

    高峰 = 工作日 UTC 01:00-04:00 与 06:00-10:00（北京时间 09-12 点、14-18 点）。
    其余时段（含周末）为低谷，价格减半。
    """
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:  # 周六周日
        return False
    h = now.hour
    return (1 <= h < 4) or (6 <= h < 10)


class LLMClient:
    """大模型客户端。"""

    def __init__(self) -> None:
        self._primary = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY or "sk-placeholder",
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=settings.LLM_TIMEOUT,
        )
        self._fallback: AsyncOpenAI | None = None
        if settings.FALLBACK_API_KEY and settings.FALLBACK_BASE_URL:
            self._fallback = AsyncOpenAI(
                api_key=settings.FALLBACK_API_KEY,
                base_url=settings.FALLBACK_BASE_URL,
                timeout=settings.LLM_TIMEOUT,
            )

    # ---------------- 非流式 ----------------
    async def chat(
        self,
        messages: list[dict[str, Any]],
        task: TaskType = "copywriting",
        tools: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
        usage_sink: UsageSink | None = None,
    ) -> ChatResult:
        route = MODEL_ROUTES[task]
        model = model_override or route.model

        if settings.ENABLE_PEAK_GUARD and is_peak_hour():
            logger.warning("当前为高峰计费时段，成本翻倍。建议把批量任务移到低谷。")

        started = time.perf_counter()
        try:
            result = await self._call(self._primary, model, messages, route, tools)
        except Exception as exc:  # noqa: BLE001
            logger.error("主模型调用失败(%s): %s", model, exc)
            if self._fallback:
                logger.info("切换到兜底模型: %s", settings.FALLBACK_MODEL)
                try:
                    result = await self._call(
                        self._fallback, settings.FALLBACK_MODEL, messages, route, tools
                    )
                except Exception as exc2:  # noqa: BLE001
                    await self._emit(usage_sink, task, model, Usage(), started, failed=True)
                    raise exc2 from exc
            else:
                # 失败也要记一笔（耗时 + 0 token），否则「重试了几次」在用量页上看不出来
                await self._emit(usage_sink, task, model, Usage(), started, failed=True)
                raise
        await self._emit(usage_sink, task, result.model or model, result.usage, started)
        return result

    async def _emit(
        self,
        sink: UsageSink | None,
        task: TaskType,
        model: str,
        usage: Usage,
        started: float,
        *,
        failed: bool = False,
    ) -> None:
        """把一次调用的用量推给埋点回调。

        这里吞掉所有异常：记账失败绝不能把对话打断。
        """
        if sink is None:
            return
        # 完全失败（没拿到任何 token）的调用不计费，也不该污染「调用次数」
        if failed and usage.total_tokens == 0:
            return
        payload = {
            "model": model,
            "task": task,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "failed": failed,
            **usage.to_dict(),
        }
        try:
            await sink(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("用量埋点写入失败（不影响对话）: %s", exc)

    async def _call(
        self,
        client: AsyncOpenAI,
        model: str,
        messages: list[dict[str, Any]],
        route: ModelRoute,
        tools: list[dict[str, Any]] | None,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": route.temperature,
            "max_tokens": route.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = await client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[dict[str, Any]] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": _safe_json(tc.function.arguments),
                        "raw_arguments": tc.function.arguments,
                    }
                )

        u = resp.usage
        prompt_details = getattr(u, "prompt_tokens_details", None)
        completion_details = getattr(u, "completion_tokens_details", None)
        usage = Usage(
            prompt_tokens=u.prompt_tokens if u else 0,
            completion_tokens=u.completion_tokens if u else 0,
            total_tokens=u.total_tokens if u else 0,
            reasoning_tokens=getattr(completion_details, "reasoning_tokens", 0) or 0,
            cached_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
        )

        return ChatResult(
            content=msg.content or "",
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=choice.finish_reason or "",
        )

    # ------------------------------------------------------------------ 流式
    async def stream_call(
        self,
        messages: list[dict[str, Any]],
        task: TaskType = "copywriting",
        tools: list[dict[str, Any]] | None = None,
        usage_sink: UsageSink | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式调用，逐块产出事件。

        产出的事件（dict）：
            {"type": "reasoning", "content": "..."}  思维链增量（可选展示，不计入正文）
            {"type": "delta", "content": "..."}      正文增量
            {"type": "tool_calls", "tool_calls": [...]} 本轮的完整工具调用（收尾时一次性给出）
            {"type": "usage", "usage": Usage}
            {"type": "finish", "finish_reason": "..."}

        为什么要流式：
            非流式要等模型把整篇写完（实测 30 秒以上），前端会一直空白。
            流式后首字延迟能压到 1-2 秒，体感完全不同。

        ⚠️ 关于 reasoning_content：
            deepseek-flash / deepseek-v4-pro 都是推理模型，会先流式吐
            `reasoning_content`（思维链），吐完之后才吐 `content`（正式回答）。
            两者必须分开处理：
              - 思维链不能当正文（否则用户看到一堆「我们需要回答用户…」）
              - 思维链也占用 max_tokens（实测 800 字文章能烧掉 1100+ reasoning tokens），
                所以 max_tokens 必须留足，否则正文会被截断成空。

        工具调用的增量拼接：
            OpenAI 兼容协议里，tool_calls 是按 index 分片下发的，
            arguments 会被切成很多小段。必须按 index 累积再解析。
        """
        route = MODEL_ROUTES.get(task, MODEL_ROUTES["copywriting"])
        model = route.model

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": route.temperature,
            "max_tokens": route.max_tokens,
            "stream": True,
            # 带 stream_options 才能拿到流式下的 token 用量
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if settings.ENABLE_PEAK_GUARD and is_peak_hour():
            logger.warning("当前为高峰计费时段，成本翻倍。建议把批量任务移到低谷。")

        # 按 index 累积工具调用分片
        acc: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage = Usage()
        started = time.perf_counter()

        stream = await self._primary.chat.completions.create(**kwargs)
        async for chunk in stream:
            # 注意：带 usage 的收尾 chunk 通常仍有 1 条 choices（finish_reason=stop），
            # 所以必须在 choices 判空之前先查 usage，否则会整段丢掉。
            if getattr(chunk, "usage", None):
                p_details = getattr(chunk.usage, "prompt_tokens_details", None)
                c_details = getattr(chunk.usage, "completion_tokens_details", None)
                usage = Usage(
                    prompt_tokens=chunk.usage.prompt_tokens or 0,
                    completion_tokens=chunk.usage.completion_tokens or 0,
                    total_tokens=chunk.usage.total_tokens or 0,
                    reasoning_tokens=getattr(c_details, "reasoning_tokens", 0) or 0,
                    cached_tokens=getattr(p_details, "cached_tokens", 0) or 0,
                )

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                finish_reason = choice.finish_reason

            if delta is None:
                continue

            # 思维链增量：单独发事件，不和正文混在一起
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield {"type": "reasoning", "content": reasoning}

            # 正文增量
            if delta.content:
                yield {"type": "delta", "content": delta.content}

            # 工具调用增量
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index if tc.index is not None else 0
                    slot = acc.setdefault(
                        idx, {"id": "", "name": "", "arguments": "", "index": idx}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

        # 收尾：把累积的分片解析成完整工具调用
        tool_calls: list[dict[str, Any]] = []
        for idx in sorted(acc):
            slot = acc[idx]
            if not slot["name"]:
                continue
            tool_calls.append(
                {
                    "id": slot["id"] or f"call_{idx}",
                    "name": slot["name"],
                    "arguments": _safe_json(slot["arguments"] or "{}"),
                    "raw_arguments": slot["arguments"],
                }
            )

        # 埋点在收尾处统一发一次（流式只有最后一个 chunk 带 usage）
        await self._emit(usage_sink, task, model, usage, started)

        yield {"type": "tool_calls", "tool_calls": tool_calls}
        yield {"type": "usage", "usage": usage}
        yield {"type": "finish", "finish_reason": finish_reason}


def _safe_json(raw: str) -> dict[str, Any]:
    """工具参数是流式拼接的 JSON 字符串，解析失败时返回空 dict 并记录。"""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("工具参数 JSON 解析失败: %s", raw[:200])
        return {}


_llm: LLMClient | None = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm
