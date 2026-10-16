"""工具注册中心（Function Calling）。

设计要点：
1. 工具用装饰器注册，加一个工具只写一个函数
2. 危险操作（发布、渲染）标记 requires_confirm，模型不能直接触发，
   必须走「模型提议 → 前端确认 → 后端执行」的流程
3. 每个工具有超时与异常隔离，单个工具失败不影响对话主流程
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
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


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        requires_confirm: bool | None = None,
    ):
        """注册工具。requires_confirm 默认读配置里的危险操作名单。"""

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

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        if not settings.ENABLE_TOOLS:
            return []
        return [t.to_openai_schema() for t in self._tools.values()]

    def needs_confirm(self, name: str) -> bool:
        tool = self.get(name)
        return bool(tool and tool.requires_confirm)

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


registry = ToolRegistry()


def tool(name: str, description: str, parameters: dict[str, Any], requires_confirm: bool | None = None):
    """工具注册装饰器。用法见 app/tools/builtin.py。"""
    return registry.register(name, description, parameters, requires_confirm)
