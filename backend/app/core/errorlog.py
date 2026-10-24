"""进程内「最近异常」环形缓冲（给系统设置页的「服务健康」卡片用）。

为什么不用日志文件：健康卡片要的是「最近出了什么岔子」，日志文件在服务器上、
用户（运营者）未必看得到；而这里只需要**最近一小段**，没必要为一个展示功能
引入文件轮转或数据库表。进程重启即清空也是可接受语义 —— 它回答的是「现在
这个进程过得好不好」，不是审计需求。

记什么（两个入口，刻意收窄）：
- `record()`：代码里显式调用的关键失败点（目前：LLM 模型调用失败）。这类是
  用户能感知的「AI 坏了」，最值得浮出来。
- `record_http()`：FastAPI 全局兜底 handler 里记未处理异常（500）。这类是
  「后端某处炸了」，同样该被看见。

不记什么：4xx（用户输入问题，不是服务健康问题）、排版/生图任务的业务失败
（那些在队列页和加工面板有自己的状态呈现，重复上报只会制造噪音）。

线程/协程安全：asyncio 单线程事件循环内 append 是原子的，deque 自带线程安全
的 append/popleft，无需加锁。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

_MAX = 50

_buf: deque[dict[str, Any]] = deque(maxlen=_MAX)


def record(source: str, message: str, detail: str = "", ts: int | None = None) -> None:
    """记一条异常。`source` 是出错子系统（llm / http / queue …），给卡片分组用。"""
    _buf.append(
        {
            "ts": int(ts if ts is not None else time.time()),
            "source": (source or "unknown")[:32],
            "message": (message or "")[:500],
            "detail": (detail or "")[:500],
        }
    )


def record_http(path: str, method: str, exc: BaseException) -> None:
    """未处理异常的统一入口（main.py 的全局 handler 调）。"""
    record("http", f"{method} {path} -> {type(exc).__name__}: {exc}")


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """最近的异常，**新的在前**。"""
    items = list(_buf)[-limit:]
    items.reverse()
    return items


def clear() -> None:
    """清空（管理员「已处理完，别再显示」时用）。"""
    _buf.clear()
