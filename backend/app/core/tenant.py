"""租户（数据归属）解析 —— 用户模块落地前的「接缝」。

背景
----
所有表早有 `owner` 列，但用户模块还没做。这之前系统是「单租户」运行：
所有请求都归属 `default` 这个虚拟租户。等真有了用户体系（登录/Token），
**只需要改本文件**：把 `current_owner` 的取值从「读请求头」换成「从会话
解析用户 ID」，路由 / 服务 / 模型一行都不用动。

约定
----
1. 前端普通请求带 `X-Owner-Id` 请求头。
2. `<img>` / `<audio>` / `<video>` 这类浏览器直链没法带头，允许
   `?owner=` 查询参数（见各静态服务端点）。
3. 都不传 = `default`：兼容老前端、curl、以及存量数据。
4. 格式限 `[A-Za-z0-9._-]{1,64}`：owner 会进文件路径（音色目录按 owner
   隔离），必须防路径穿越。

注意：这是**隔离接缝**，不是安全边界。真正的权限校验要等用户模块
（登录态 + 服务端会话），届时 `current_owner` 只认服务端解析出的身份，
不再信任客户端传的 header。
"""
from __future__ import annotations

import re

from fastapi import Depends, Header, HTTPException, Query

DEFAULT_OWNER = "default"

# owner 会用于拼文件路径，只放行安全字符
_OWNER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def normalize_owner(raw: str | None) -> str:
    """把外部传入的 owner 规范成合法值；不合法直接抛 ValueError。"""
    v = (raw or "").strip()
    if not v:
        return DEFAULT_OWNER
    if not _OWNER_RE.fullmatch(v):
        raise ValueError(f"非法的 owner 标识（仅限 1~64 位字母、数字、._-）：{raw!r}")
    return v


def current_owner(
    x_owner_id: str | None = Header(
        default=None, alias="X-Owner-Id", description="数据归属（用户模块上线前的隔离口径）"
    ),
    owner: str | None = Query(
        default=None, description="直链场景（img/audio 的 src）没法带请求头，用查询参数"
    ),
) -> str:
    """FastAPI 依赖：解析本次请求的数据归属。

    头优先于查询参数。用户模块落地时，这个函数是唯一需要改的地方
    （换成从登录态/Token 取用户 ID，并不再信任客户端传入值）。
    """
    try:
        return normalize_owner(x_owner_id or owner)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# 便于 `Depends(current_owner)` 复用，也方便测试直接 import
OwnerDep = Depends(current_owner)
