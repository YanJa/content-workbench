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
from app.core.errorlog import record as record_error
from app.core.logging import logger
from app.services.model_pool import (
    ModelSpec,
    SYSTEM_OWNER,
    bump_quota,
    mark_failure,
    mark_success,
    resolve_candidates,
    resolve_model,
    take_warnings,
)

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


# Cloudflare Workers AI 对话通道的参数。
# 为什么单独一份而不是复用 MODEL_ROUTES：
#   1. CF 的 llama 系模型输出上限远低于 DeepSeek（16384 直接 400）；
#   2. CF 模型没有思维链，温度按普通对话模型给；
#   3. CF 端点不支持 stream_options.include_usage，流式 usage 常缺，
#      走「估价」路径（total_tokens=0 → Usage.estimated=True）。
_CF_MAX_TOKENS = 4096
_CF_TEMPERATURE = 0.7


def _cf_route(model: str) -> ModelRoute:
    return ModelRoute(
        model=model,
        temperature=_CF_TEMPERATURE,
        max_tokens=_CF_MAX_TOKENS,
        reason="Cloudflare Workers AI 对话（OpenAI 兼容端点 /ai/v1）",
    )


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


def _legacy_spec(provider: str, model_override: str | None) -> ModelSpec:
    """把老的 provider 字符串翻译成 ModelSpec。

    改造前 CF 的两个特例（不传 tools、不认 stream_options）是绑在
    `provider == "cloudflare"` 这个字符串上的；新接一家厂商就得再写一遍
    if 判断，漏一次就是一个 400。翻译成 flags 之后，新厂商只要在池里把
    supports_tools / supports_stream_usage 勾对就行。
    """
    if provider == "cloudflare":
        return ModelSpec(
            model=model_override or settings.CLOUDFLARE_CHAT_MODEL,
            provider_key="cloudflare",
            temperature=_CF_TEMPERATURE,
            max_tokens=_CF_MAX_TOKENS,
            supports_tools=False,
            supports_stream_usage=False,
        )
    return ModelSpec(model=model_override or "", provider_key="deepseek")


class LLMClient:
    """大模型客户端。

    选模型的两条路：
      1. **老路**：provider="deepseek" / "cloudflare" 两个字符串 + model_override。
         行为与改造前完全一致（`_legacy_spec` 保证）。
      2. **模型池**：传 `feature="chat"` 这样的功能槽，从池里按健康度挑一个。
         池里没有可用条目时**自动退回老路** —— 模型池是增强，不是必经之路。

    非对话任务（排版/摘要/标签）以前固定走 DeepSeek，现在可以各自绑定槽位。
    """

    def __init__(self) -> None:
        self._pool_clients: dict[tuple[str, str], AsyncOpenAI] = {}
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
        # Cloudflare Workers AI：凭证齐才建（与生图通道共用同一对凭证）
        self._cloudflare: AsyncOpenAI | None = None
        if settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID:
            self._cloudflare = AsyncOpenAI(
                api_key=settings.CLOUDFLARE_API_TOKEN,
                base_url=(
                    f"https://api.cloudflare.com/client/v4/accounts/"
                    f"{settings.CLOUDFLARE_ACCOUNT_ID}/ai/v1"
                ),
                timeout=settings.LLM_TIMEOUT,
            )

    def _client_for(self, spec: ModelSpec) -> AsyncOpenAI:
        """按 spec 取客户端。

        老的三个实例（primary / fallback / cloudflare）继续复用；池里新加的厂商
        按 (provider_key, cred_owner) 缓存 —— 同一个服务商的全局 Key 和个人 Key
        是两个不同的 client，额度也要各算各的。
        """
        if spec.provider_key == "deepseek":
            return self._primary
        if spec.provider_key == "cloudflare":
            if self._cloudflare is None:
                raise RuntimeError(
                    "未配置 Cloudflare（CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID）"
                )
            return self._cloudflare
        key = (spec.provider_key, spec.cred_owner)
        client = self._pool_clients.get(key)
        if client is None:
            client = AsyncOpenAI(
                api_key=spec.api_key or "sk-placeholder",
                base_url=spec.base_url or None,
                timeout=settings.LLM_TIMEOUT,
            )
            self._pool_clients[key] = client
        return client

    def _pick(
        self,
        provider: str,
        model_override: str | None,
        feature: str | None,
        owner: str | None,
    ) -> ModelSpec:
        """决定这次用哪个模型：feature 命中池就用池，否则退回老路径。"""
        if feature:
            spec = resolve_model(feature, owner)
            if spec is not None:
                return spec
            logger.info("模型池没有可用于 %s 的条目，回落环境变量通道", feature)
        return _legacy_spec(provider, model_override)

    # ---------------- 非流式 ----------------
    async def chat(
        self,
        messages: list[dict[str, Any]],
        task: TaskType = "copywriting",
        tools: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
        usage_sink: UsageSink | None = None,
        provider: str = "deepseek",
        *,
        feature: str | None = None,
        owner: str | None = None,
    ) -> ChatResult:
        spec = self._pick(provider, model_override, feature, owner)
        route = MODEL_ROUTES.get(task, MODEL_ROUTES["copywriting"])
        model = spec.model or route.model
        client = self._client_for(spec)

        if tools and not spec.supports_tools:
            logger.warning("%s 不支持工具调用，本次已忽略 tools", spec.provider_key)
            tools = None

        if settings.ENABLE_PEAK_GUARD and is_peak_hour():
            logger.warning("当前为高峰计费时段，成本翻倍。建议把批量任务移到低谷。")

        started = time.perf_counter()
        try:
            result = await self._call(client, model, messages, spec, route, tools)
        except Exception as exc:  # noqa: BLE001
            logger.error("模型调用失败(%s): %s", model, exc)
            record_error("llm", f"模型调用失败({model}): {exc}")
            # 兜底只给 DeepSeek 主通道（CF 与新厂商失败即抛，语义同改造前）
            if spec.provider_key == "deepseek" and self._fallback:
                logger.info("切换到兜底模型: %s", settings.FALLBACK_MODEL)
                try:
                    result = await self._call(
                        self._fallback,
                        settings.FALLBACK_MODEL,
                        messages,
                        _legacy_spec("deepseek", None),
                        route,
                        tools,
                    )
                except Exception as exc2:  # noqa: BLE001
                    await self._emit(usage_sink, task, model, Usage(), started, failed=True, spec=spec)
                    await mark_failure(spec, str(exc2))
                    raise exc2 from exc
            else:
                # 失败也要记一笔（耗时 + 0 token），否则「重试了几次」在用量页上看不出来
                await self._emit(usage_sink, task, model, Usage(), started, failed=True, spec=spec)
                await mark_failure(spec, str(exc))
                raise
        await self._emit(usage_sink, task, result.model or model, result.usage, started, spec=spec)
        await mark_success(spec)
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
        spec: ModelSpec | None = None,
    ) -> None:
        """把一次调用的用量推给埋点回调，并累加模型池额度。

        这里吞掉所有异常：记账失败绝不能把对话打断。
        """
        if sink is None and spec is None:
            return
        # 完全失败（没拿到任何 token）的调用不计费，也不该污染「调用次数」
        if failed and usage.total_tokens == 0:
            return
        payload = {
            "model": model,
            "task": task,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "failed": failed,
            "cred_owner": spec.cred_owner if spec else SYSTEM_OWNER,
            "billing": spec.billing if spec else "",
            "provider_key": spec.provider_key if spec else "",
            **usage.to_dict(),
        }
        try:
            if sink is not None:
                await sink(payload)
            if spec is not None and spec.catalog_id:
                await bump_quota(spec, tokens=usage.total_tokens or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("用量埋点写入失败（不影响对话）: %s", exc)

    async def _call(
        self,
        client: AsyncOpenAI,
        model: str,
        messages: list[dict[str, Any]],
        spec: ModelSpec,
        route: ModelRoute,
        tools: list[dict[str, Any]] | None,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            # 池条目没配参数时沿用 MODEL_ROUTES 里调过的值 —— 新加的模型不该
            # 退化成 SDK 默认参数（那会和既有模型的表现对不上）
            "temperature": spec.temperature if spec.temperature is not None else route.temperature,
            "max_tokens": spec.max_tokens if spec.max_tokens is not None else route.max_tokens,
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
        provider: str = "deepseek",
        model_override: str | None = None,
        *,
        feature: str | None = None,
        owner: str | None = None,
        preferred: ModelSpec | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式调用，逐块产出事件。

        preferred：调用方已经解析好的首选模型（多轮工具循环里必须传 ——
        每轮都重新解析的话，可能第 1 轮用 A、第 3 轮 A 的额度刚好耗尽换成 B，
        同一条回答里风格突变）。传了它，候选链就是 [preferred, 其余候选]。

        产出的事件（dict）：
            {"type": "reasoning", "content": "..."}  思维链增量（可选展示，不计入正文）
            {"type": "delta", "content": "..."}      正文增量
            {"type": "tool_calls", "tool_calls": [...]} 本轮的完整工具调用（收尾时一次性给出）
            {"type": "usage", "usage": Usage}
            {"type": "finish", "finish_reason": "..."}
            {"type": "error", "message": "..."}      调用失败（SSE 已 200，只能这样告诉前端）
            {"type": "quota_warning", ...}           额度预警（80% / 已用尽，非阻塞）
            {"type": "model_switched", ...}          已自动切换到备用模型

        ------------------------------------------------------------------
        关于「自动切换到备用模型」的一条硬语义
        ------------------------------------------------------------------
        一旦已经开始吐正文，就**不再换模型** —— 否则用户会看到两段风格迥异的
        正文拼接在同一条回答里。切换只发生在「开流阶段就失败、一个字都没吐」
        的时候。这条不写进代码，上线后一定会出现拼接回答，而且极难复现。
        """
        route = MODEL_ROUTES.get(task, MODEL_ROUTES["copywriting"])

        # 候选链：feature 模式下把池里所有健康候选排好队；老路径只有一个
        if feature:
            candidates = resolve_candidates(feature, owner)
            if not candidates:
                logger.info("模型池没有可用于 %s 的条目，回落环境变量通道", feature)
                candidates = [_legacy_spec(provider, model_override)]
            elif preferred is not None:
                # 首选固定为调用方解析好的那个，其余候选排后面当退路
                rest = [c for c in candidates if c.catalog_id != preferred.catalog_id]
                candidates = [preferred] + rest
        else:
            candidates = [preferred or _legacy_spec(provider, model_override)]

        if settings.ENABLE_PEAK_GUARD and is_peak_hour():
            logger.warning("当前为高峰计费时段，成本翻倍。建议把批量任务移到低谷。")

        for idx, spec in enumerate(candidates):
            is_last = idx == len(candidates) - 1
            model = spec.model or route.model
            try:
                client = self._client_for(spec)
            except RuntimeError as exc:
                yield {"type": "error", "message": str(exc)}
                yield {"type": "finish", "finish_reason": "error"}
                return

            if tools and not spec.supports_tools:
                logger.warning("%s 不支持工具调用，本次已忽略 tools", spec.provider_key)
                tools = None

            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": spec.temperature if spec.temperature is not None else route.temperature,
                "max_tokens": spec.max_tokens if spec.max_tokens is not None else route.max_tokens,
                "stream": True,
            }
            # CF 的 /ai/v1 兼容端点不认识 stream_options，带了直接 400
            if spec.supports_stream_usage:
                kwargs["stream_options"] = {"include_usage": True}
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            # 额度预警：在开流之前推给前端，不打断正文
            for warn in take_warnings(spec, feature or ""):
                yield warn

            state: dict[str, bool] = {"failed": False, "produced": False}
            async for evt in self._run_stream(
                client, kwargs, task, model, usage_sink,
                spec=spec, emit_error=is_last, state=state,
            ):
                yield evt

            if not state["failed"]:
                await mark_success(spec)
                return
            if state["produced"]:
                # 已经吐过正文，不能再换模型了 —— 但错误必须告诉用户，
                # 否则流就这么静悄悄断了（emit_error 当初按「还有候选」给成了 False）
                logger.error("模型 %s 中途失败，已产出正文，不再切换", model)
                yield {"type": "error", "message": f"模型 {spec.display or model} 输出中断"}
                yield {"type": "finish", "finish_reason": "error"}
                return
            if is_last:
                return
            nxt = candidates[idx + 1]
            logger.warning("模型 %s 开流即失败，切换到候选 %s", model, nxt.model)
            yield {
                "type": "model_switched",
                "from": model,
                "to": nxt.model,
                "message": f"{spec.display or model} 调用失败，已切换到 {nxt.display or nxt.model}",
            }

    async def _run_stream(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
        task: TaskType,
        model: str,
        usage_sink: UsageSink | None,
        spec: ModelSpec | None = None,
        emit_error: bool = True,
        state: dict[str, bool] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """开流 → 逐 chunk 产出事件 → 收尾。CF 与 DeepSeek 共用。

        异常处理：SSE 一旦返回 200 就改不了状态码，模型侧报错（凭证失效、
        模型名写错、超长）如果直接冒泡，前端只看到「流断了」。这里统一转成
        {"type": "error"} 事件 + finish，前端 NAlert 能展示具体原因。

        ⚠️ 关于 reasoning_content：
            deepseek-flash / deepseek-v4-pro 都是推理模型，会先流式吐
            `reasoning_content`（思维链），吐完之后才吐 `content`（正式回答）。
            CF 模型没有这个字段（getattr 兜底 None），行为自动降级。

        工具调用的增量拼接：
            OpenAI 兼容协议里，tool_calls 是按 index 分片下发的，
            arguments 会被切成很多小段。必须按 index 累积再解析。
        """
        # 按 index 累积工具调用分片
        acc: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage = Usage()
        started = time.perf_counter()
        emitted = False

        try:
            stream = await client.chat.completions.create(**kwargs)
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
                    if state is not None:
                        state["produced"] = True
                    yield {"type": "reasoning", "content": reasoning}

                # 正文增量
                if delta.content:
                    if state is not None:
                        state["produced"] = True
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
        except Exception as exc:  # noqa: BLE001
            logger.error("流式调用失败(%s): %s", model, exc)
            record_error("llm", f"流式调用失败({model}): {exc}")
            await self._emit(usage_sink, task, model, usage, started, failed=True, spec=spec)
            emitted = True
            if state is not None:
                state["failed"] = True
            if spec is not None and spec.catalog_id:
                await mark_failure(spec, str(exc))
            # 后面还有候选时不发 error —— 发了前端就当这次对话结束了，
            # 切换过去的模型白跑一趟
            if emit_error:
                yield {"type": "error", "message": f"模型调用失败：{exc}"}
                yield {"type": "finish", "finish_reason": "error"}
            return
        finally:
            # 兜底记账：SSE 客户端断开（切页面/关标签页）时，取消以 CancelledError
            # （BaseException，上面的 except Exception 抓不到）冒泡，正常收尾的
            # 埋点会被整段跳过 —— 模型 token 已经烧了，账不能丢。
            # 断开时 usage chunk（流尾才到）通常还没读到 → total=0 记一笔
            # estimated 兜底：调用事实至少不丢，金额本来就是估价
            # （CF 流式无 include_usage，完整消费时也是 estimated，语义一致）。
            # 2026-10-19 实测：UI 端到端测试一切换页面就走到了这条路径。
            if not emitted:
                await self._emit(usage_sink, task, model, usage, started, spec=spec)
                emitted = True

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
        # （正常路径下 finally 的兜底已经发过一次，这里跳过防双记）
        if not emitted:
            await self._emit(usage_sink, task, model, usage, started, spec=spec)
            emitted = True

        yield {"type": "tool_calls", "tool_calls": tool_calls}
        # 带上模型名，前端气泡角标能显示「由 X 生成」—— 用户能自己发现被换过模型
        yield {"type": "usage", "usage": usage, "model": model}
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
