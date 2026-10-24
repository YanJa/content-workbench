"""系统级只读信息（服务健康卡片的「详细版」数据源）。

`/api/health` 是给探活用的最小载荷；这张卡片要的是「系统过得怎么样」：
运行时（版本/运行时长/库大小）、外部通道（公众号/知乎/Cloudflare 凭证与水位）、
模型池（8 个功能槽的主模型与额度）、最近异常。

设计约束：
- **只读、不拨号**：外部通道只查「凭证就位 + 本地计数」，不真实调用微信/知乎
  接口 —— 健康页刷新一次就发几个外部请求，既慢又烧配额。真实连通性测试留在
  各自卡片（公众号有「检测」按钮）。
- **模型池按平台 Key 口径**统计额度（cred_owner=system）：个人 Key 的用量在
  用量页看，这里回答「平台这套池子还行不行」。
- 登录即可看：没有密钥明文，没有他人数据。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errorlog import recent as recent_errors
from app.core.rbac import User, current_user
from app.db.session import get_db
from app.models.settings import WeChatAccount
from app.services import model_pool, zhihu

router = APIRouter(prefix="/system", tags=["系统"])

#: 进程启动时刻（模块首次被导入 ≈ uvicorn 拉起进程，偏差毫秒级，够用）
STARTED_AT = time.time()


def _db_size_bytes(db: AsyncSession) -> int | None:
    """SQLite 主库 + WAL 的磁盘占用。非 SQLite（以后换 PostgreSQL）返回 None。

    用**当前连接的引擎**而不是 settings 里的 URL：测试把 get_db 覆盖成内存库
    时，settings 仍指向真实文件 —— 按连接走才能反映「这次请求实际在查哪个库」。
    """
    url = str(db.bind.url) if db.bind is not None else settings.DATABASE_URL
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///")[-1]
    if raw == ":memory:" or not raw:
        return None
    path = Path(raw)
    total = 0
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total or None


async def _channel_wechat(db: AsyncSession) -> dict:
    n = int(
        (
            await db.execute(select(func.count()).select_from(WeChatAccount))
        ).scalar_one()
    )
    return {
        "ok": n > 0,
        "detail": f"{n} 个公众号已配置" if n else "未配置公众号凭证（设置页「公众号管理」可添加）",
    }


async def _channel_zhihu() -> dict:
    info = await zhihu.usage_today()
    sec = zhihu.secret_info()
    configured = bool(sec.get("configured"))
    if not configured:
        return {"ok": False, "detail": "未配置知乎 Access Secret（设置页可填）", "used": 0, "quota": 0}
    used, quota = int(info.get("used") or 0), int(info.get("quota") or 0)
    return {
        "ok": True,
        "detail": f"今日热榜调用 {used}/{quota} 次",
        "used": used,
        "quota": quota,
    }


def _channel_cloudflare() -> dict:
    ok = bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID)
    return {
        "ok": ok,
        "detail": "凭证就位（生图 / 对话 / 向量通道可用）"
        if ok
        else "未配置 CF Token（文生图与 Cloudflare 对话通道不可用）",
    }


def _pool_payload() -> dict:
    """模型池概览：服务商/模型/槽位绑定 + 有额度限制的模型水位。"""
    snap = model_pool.snapshot()
    providers = [
        {"key": p.get("key"), "name": p.get("name") or p.get("key"), "kind": p.get("kind")}
        for p in snap.providers.values()
    ]

    bindings = []
    for feature, label in model_pool.FEATURES.items():
        b = snap.bindings.get(feature) or {}
        entry = snap.catalog.get(b.get("primary") or "")
        if not entry:
            bindings.append({"feature": feature, "label": label, "primary": None})
            continue
        prov = snap.providers.get(entry.get("provider_key") or "") or {}
        spec = model_pool._spec_of(entry, prov, "", "system")
        qs = model_pool.quota_status(spec)
        # 只把「设了上限」的槽报出水位；没设限制的槽不需要进度条
        limit_key = next(
            (k for k, v in qs["limits"].items() if isinstance(v, int) and v > 0), None
        )
        quota = None
        if limit_key:
            limit = int(qs["limits"][limit_key])
            used = int(qs["used"].get(limit_key) or 0)
            quota = {"metric": limit_key, "used": used, "limit": limit, "ratio": round(used / limit, 3)}
        bindings.append(
            {
                "feature": feature,
                "label": label,
                "primary": spec.display,
                "fallbacks": len(b.get("fallbacks") or []),
                "quota": quota,
            }
        )

    alerts = [
        {
            "level": "danger" if (b["quota"]["ratio"] or 0) >= 1 else "warning",
            "model": b["primary"],
            "ratio": b["quota"]["ratio"],
            "message": (
                f"{b['primary']} 额度已用尽，调用会自动切到备选"
                if (b["quota"]["ratio"] or 0) >= 1
                else f"{b['primary']} 额度已用 {round(b['quota']['ratio'] * 100)}%（80% 预警线）"
            ),
        }
        for b in bindings
        if b.get("quota") and (b["quota"]["ratio"] or 0) >= 0.8
    ]
    return {
        "providers": providers,
        "model_count": len(snap.catalog),
        "bindings": bindings,
        "alerts": alerts,
    }


@router.get("/health-detail")
async def health_detail(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict:
    up = int(time.time() - STARTED_AT)
    return {
        "runtime": {
            "version": settings.APP_VERSION,
            "env": settings.APP_ENV,
            "started_at": int(STARTED_AT),
            "uptime_secs": up,
            "uptime": f"{up // 86400}天{up % 86400 // 3600}小时" if up >= 3600 else f"{up // 60}分钟",
            "db_size_bytes": _db_size_bytes(db),
            "pid": os.getpid(),
        },
        "channels": {
            "wechat": await _channel_wechat(db),
            "zhihu": await _channel_zhihu(),
            "cloudflare": _channel_cloudflare(),
        },
        "pool": _pool_payload(),
        "recent_errors": recent_errors(20),
    }
