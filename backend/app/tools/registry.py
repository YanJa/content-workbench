"""工具注册中心（Function Calling）。

设计要点：
1. 工具用装饰器注册，加一个工具只写一个函数
2. 危险操作（发布、渲染）标记 requires_confirm，模型不能直接触发，
   必须走「模型提议 → 前端确认 → 后端执行」的流程
3. 每个工具有超时与异常隔离，单个工具失败不影响对话主流程
4. **动态工具**：定义随请求上下文变化（典型是 activate_skill 的 name 枚举），
   用 `dynamic_tool()` 注册，`schemas_for(ctx)` 生成；没有可用项时整个工具
   不提供给模型 —— 空 enum 的工具会让模型反复试错。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.config import settings
from app.core.logging import logger


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    requires_confirm: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的 tools 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class DynamicTool:
    """定义随请求变化的工具。

    为什么需要它：`activate_skill` 必须把 `name` 参数用 **enum 约束到当前用户
    实际拥有的技能集合**（官方对非 Claude 模型的推荐做法）。而注册表是模块
    导入期建好的进程级单例，装不下「随用户变化」的定义。

    - `description_for(names)` / `parameters_for(names)`：用本次可用集合生成定义；
    - `list_fn(db, owner)`：算可用集合，**为空时整个工具不提供给模型**；
    - `func`：真实执行逻辑（和普通工具一样，ctx 按签名注入）。
    """

    name: str
    func: Callable[..., Any]
    list_fn: Callable[..., Any]
    description_for: Callable[[list[str]], str]
    parameters_for: Callable[[list[str]], dict[str, Any]]
    requires_confirm: bool = False
    # 无上下文时的展示定义（/api/tools 用，枚举留空）
    base_description: str = ""
    base_parameters: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._dynamic: dict[str, DynamicTool] = {}

    # ---------------------------------------------------------------- 注册
    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        requires_confirm: bool | None = None,
    ):
        """注册静态工具。requires_confirm 默认读配置里的危险操作名单。"""

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            rc = (
                requires_confirm
                if requires_confirm is not None
                else name in settings.confirm_tools
            )
            self._tools[name] = Tool(
                name=name,
                description=description,
                parameters=parameters,
                func=func,
                requires_confirm=rc,
            )
            return func

        return deco

    def register_dynamic(self, dt: DynamicTool) -> None:
        self._dynamic[dt.name] = dt

    # ---------------------------------------------------------------- 查询
    def get(self, name: str) -> Tool | DynamicTool | None:
        return self._tools.get(name) or self._dynamic.get(name)

    def all(self) -> list[Tool | DynamicTool]:
        return [*self._tools.values(), *self._dynamic.values()]

    def schemas(self) -> list[dict[str, Any]]:
        """无上下文场景的完整定义（/api/tools 展示、日志）。

        动态工具在这里用「无可用集合」的基础定义占位。**发模型前必须走
        `schemas_for(ctx)`**，否则会发出一个 enum 为空的 activate_skill。
        """
        if not settings.ENABLE_TOOLS:
            return []
        out = [t.to_openai_schema() for t in self._tools.values()]
        for dt in self._dynamic.values():
            out.append(
                Tool(
                    name=dt.name,
                    description=dt.base_description,
                    parameters=dt.base_parameters,
                    func=dt.func,
                    requires_confirm=dt.requires_confirm,
                ).to_openai_schema()
            )
        return out

    async def schemas_for(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """按请求上下文生成工具定义（含动态工具）。

        动态工具失败/无可用项 → **直接不出现**在列表里。一个技能表的读失败
        不能把整轮对话的工具能力拖下水。
        """
        if not settings.ENABLE_TOOLS:
            return []

        out = [t.to_openai_schema() for t in self._tools.values()]
        for dt in self._dynamic.values():
            try:
                names = list(await _call_list(dt.list_fn, ctx))
            except Exception as exc:  # noqa: BLE001 - 动态工具降级
                logger.warning("动态工具 %s 取可用集合失败（本次不提供）: %s", dt.name, exc)
                continue
            if not names:
                continue
            out.append(
                Tool(
                    name=dt.name,
                    description=dt.description_for(names),
                    parameters=dt.parameters_for(names),
                    func=dt.func,
                    requires_confirm=dt.requires_confirm,
                ).to_openai_schema()
            )
        return out

    def needs_confirm(self, name: str) -> bool:
        t = self.get(name)
        return bool(t and t.requires_confirm)

    # ---------------------------------------------------------------- 执行
    async def execute(self, name: str, arguments: dict[str, Any], ctx: dict[str, Any]) -> Any:
        """执行工具。ctx 会注入给需要上下文的工具（如 db session、owner）。"""
        tool = self.get(name)
        if tool is None:
            return {"error": f"未知工具: {name}"}

        try:
            sig = inspect.signature(tool.func)
            kwargs = {k: v for k, v in arguments.items() if k in sig.parameters}
            # 显式声明了 ctx 参数的才注入
            if "ctx" in sig.parameters:
                kwargs["ctx"] = ctx

            result = tool.func(**kwargs)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=300)
            return result
        except asyncio.TimeoutError:
            logger.error("工具执行超时: %s", name)
            return {"error": f"工具 {name} 执行超时"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具执行失败: %s", name)
            return {"error": f"工具 {name} 执行失败: {exc}"}


async def _call_list(list_fn: Callable[..., Any], ctx: dict[str, Any]) -> Any:
    """调 list_fn。同步/异步、要不要 ctx 都兼容。"""
    sig = inspect.signature(list_fn)
    if "ctx" in sig.parameters:
        res = list_fn(ctx)
    else:
        db = ctx.get("db")
        owner = ctx.get("owner") or "default"
        res = list_fn(db, owner)
    if inspect.isawaitable(res):
        res = await res
    return res


registry = ToolRegistry()


def tool(name: str, description: str, parameters: dict[str, Any], requires_confirm: bool | None = None):
    """静态工具注册装饰器。用法见 app/tools/builtin.py。"""
    return registry.register(name, description, parameters, requires_confirm)


def dynamic_tool(
    name: str,
    list_fn: Callable[..., Any],
    description_for: Callable[[list[str]], str],
    parameters_for: Callable[[list[str]], dict[str, Any]],
    requires_confirm: bool | None = None,
):
    """动态工具注册装饰器：定义由 `list_fn` 算出的可用集合决定。"""

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        registry.register_dynamic(
            DynamicTool(
                name=name,
                func=func,
                list_fn=list_fn,
                description_for=description_for,
                parameters_for=parameters_for,
                requires_confirm=(
                    requires_confirm
                    if requires_confirm is not None
                    else name in settings.confirm_tools
                ),
                base_description=description_for([]),
                base_parameters=parameters_for([]),
            )
        )
        return func

    return deco
