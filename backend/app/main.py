"""内容运营工作台 · 后端入口。

启动：
    uvicorn app.main:app --reload --port 8000

接口文档：http://localhost:8000/docs
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.cache import close_cache, get_cache
from app.core.config import settings
from app.core.logging import logger
from app.core.tenant import current_owner_id
from app.db.session import SessionLocal, init_db
from app.routers import (
    admin,
    auth,
    chat,
    humanize,
    knowledge,
    layout,
    model_pool,
    queue,
    revenue,
    settings as settings_router,
    skills,
    system,
    tools,
    usage,
    voice_clone,
    weibo,
    zhihu,
)

# 导入内置工具以完成注册（必须有这一句）
from app.tools import builtin  # noqa: F401

from app.services.zhihu import load_runtime_secret, loop_hot_fetch  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 56)
    logger.info("%s 启动中 (env=%s)", settings.APP_NAME, settings.APP_ENV)
    logger.info("=" * 56)

    os.makedirs(os.path.dirname(settings.DATABASE_URL.split("///")[-1]) or "./data", exist_ok=True)
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
    os.makedirs(settings.VOICE_DIR, exist_ok=True)
    # 配图目录（生图/上传）
    os.makedirs(os.path.join(settings.UPLOAD_DIR, "gen"), exist_ok=True)

    await init_db()
    # 预置管理员账号：保证 ADMIN_USERNAME 存在且是 admin（不存在就建，
    # 已存在只摆正角色、不动密码）。放在 init_db 之后 —— 要先有表才能查。
    from app.core.rbac import ensure_admin_account

    await ensure_admin_account()
    get_cache()

    # 把 .env 里的旧全局邀请码导入为新体系记录（幂等）。放在 init_db 之后 ——
    # 要先有 invite_codes 表才能写。
    from app.core.invite import import_legacy_code

    await import_legacy_code()

    # 上一次进程退出时正在渲染的任务，统一标成失败，不让它挂着假装在跑
    from app.services.render import recover_orphans

    await recover_orphans()

    # 清理残留的「加工中」标记（排版/生图）：进程重启后这些长任务没了，
    # 但 Content.processing 若留在库里会把队列页对应行的按钮永久禁用。
    from app.services.layout import recover_stuck_processing

    await recover_stuck_processing()

    # 知乎热点：后台定时抓取（对话页「不知道写啥」灵感卡片的数据源）。
    # 先加载 DB 里存过的 secret（管理后台可在线设置，DB 优先于 .env），
    # 再起循环 —— 顺序反了的话，启动时用的还是 .env 的旧值。
    # 任务内部自己判断有没有配凭证，没配会进入待命而不是退出。注意它没有
    # sparsely 依赖上面任何一步，但 create_task 必须在 init_db 之后 —— 循环里
    # 第一件事是查 HotSnapshot 表决定要不要补抓。
    await load_runtime_secret()
    asyncio.create_task(loop_hot_fetch())

    # 模型池：按 .env 播种服务商与模型、迁移旧的 chat_provider 设置、
    # 并把配置读进内存快照。放在 init_db 之后 —— 要先有表才能查。
    # 失败只告警、不影响启动：模型池解析不到会回落环境变量通道。
    from app.services.model_pool import bootstrap_pool, loop_pool_refresh

    async with SessionLocal() as seed_db:
        await bootstrap_pool(seed_db)
        # 老账单补记「这笔是免费模型」：加 billing 列之前的行没有这个标记，
        # 免费模型会一直挂着兜底价算出来的钱（与「标着免费」自相矛盾）。
        try:
            from app.services.usage import backfill_free_billing

            n = await backfill_free_billing(seed_db)
            if n:
                await seed_db.commit()
                logger.info("已为 %d 条历史用量补记「免费模型」标记", n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("历史用量 billing 回填失败（不影响启动）: %s", exc)
    asyncio.create_task(loop_pool_refresh())

    # 知识库：把上次进程中断时没跑完的向量化重新排队（只捞 pending，
    # failed 是明确失败，等用户在列表页决定要不要重试）。
    from app.services.knowledge import reembed_failed_on_startup

    asyncio.create_task(reembed_failed_on_startup())

    if not settings.DEEPSEEK_API_KEY:
        logger.warning("未配置 DEEPSEEK_API_KEY，AI 功能将不可用。请在 .env 中填写。")

    logger.info("已注册工具: %s", [t.name for t in tools.registry.all()])
    yield
    await close_cache()
    logger.info("服务已停止")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="内容运营工作台后端 API。含多平台文案生成、会话管理、工具调用、视频渲染。",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    # 额外放行本机任意端口：静态文件通道打开前端时端口是随机分配的，
    # 写不进上面的白名单。正则只匹配 loopback，不放行公网。
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(chat.router, prefix=settings.API_PREFIX)
app.include_router(queue.router, prefix=settings.API_PREFIX)
app.include_router(queue.content_router, prefix=settings.API_PREFIX)
app.include_router(layout.router, prefix=settings.API_PREFIX)
app.include_router(settings_router.router, prefix=settings.API_PREFIX)
app.include_router(tools.router, prefix=settings.API_PREFIX)
app.include_router(tools.video_router, prefix=settings.API_PREFIX)
app.include_router(voice_clone.router, prefix=settings.API_PREFIX)
app.include_router(usage.router, prefix=settings.API_PREFIX)
app.include_router(revenue.router, prefix=settings.API_PREFIX)
app.include_router(admin.router, prefix=settings.API_PREFIX)
app.include_router(zhihu.router, prefix=settings.API_PREFIX)
app.include_router(weibo.router, prefix=settings.API_PREFIX)
app.include_router(knowledge.router, prefix=settings.API_PREFIX)
app.include_router(model_pool.router, prefix=settings.API_PREFIX)
app.include_router(skills.router, prefix=settings.API_PREFIX)
app.include_router(humanize.router, prefix=settings.API_PREFIX)
app.include_router(system.router, prefix=settings.API_PREFIX)


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    """全局兜底：任何未处理异常都进「最近异常」环形缓冲。

    没有这个钩子，500 只能翻服务器日志才知道；健康卡片要显示「最近出了什么
    岔子」，就得在进程内留一份。只记不拦 —— 返回值 None 交给 Starlette 的
    默认 500 流程，行为不变，多了一份记录。
    """
    from app.core.errorlog import record_http

    record_http(request.url.path, request.method, exc)
    # 与 FastAPI 默认 500 行为一致（handler 必须返回 Response，不能返回 None）
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


@app.get("/", tags=["系统"])
async def root():
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "api_prefix": settings.API_PREFIX,
    }


def _health_payload() -> dict:
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "env": settings.APP_ENV,
        "llm_configured": bool(settings.DEEPSEEK_API_KEY),
        "redis": settings.use_redis,
        "tools_enabled": settings.ENABLE_TOOLS,
        # 模型明细：只读 .env 配置、不查库，探活保持轻量。
        # 「服务健康」卡片要展示用户实际在用的模型，光一个 flash 名不够
        # 全面——Flash/Pro 双模型 + CF 通道默认值都得能看见。
        # per-user 的当前通道（用户在个人资料页改的 chat_provider /
        # cloudflare_chat_model）走 GET /settings/chat-provider，前端拼合。
        "model_flash": settings.DEEPSEEK_MODEL_FLASH,
        "model_pro": settings.DEEPSEEK_MODEL_PRO,
        "fallback_model": settings.FALLBACK_MODEL,
        "cf_chat_model": settings.CLOUDFLARE_CHAT_MODEL,
        "cf_configured": bool(
            settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID
        ),
    }


@app.get("/health", tags=["系统"])
async def health():
    """裸路径探活，给 K8s / 负载均衡用。"""
    return _health_payload()


@app.get(f"{settings.API_PREFIX}/health", tags=["系统"])
async def health_under_prefix():
    """带 /api 前缀的版本。

    前端的 axios baseURL 是 /api，走 Vite 代理时只会把 /api 打过来，
    所以必须同时提供这个路径，否则前端探活永远 404。
    """
    return _health_payload()


@app.get(f"{settings.API_PREFIX}/whoami", tags=["系统"])
async def whoami(request: Request, owner: str = Depends(current_owner_id)):
    """链路自检探针：把网关转过来的请求原样回声。

    用于排查反向代理链路（本机隧道 → 云端网关 → 沙箱）到底改写了什么：
    原始 Host、真实来源 IP、有没有转发头、请求路径有没有被剥前缀。

    要求登录：公网暴露等于送人一个「请求回声器」，能看到转发头与来源 IP。
    """
    forwarded = {
        k: v
        for k, v in request.headers.items()
        if k.lower().startswith(("x-forwarded", "x-real", "via", "x-orig"))
    }
    return {
        "client_host": request.client.host if request.client else None,
        "client_port": request.client.port if request.client else None,
        "url_path": request.url.path,
        "host_header": request.headers.get("host"),
        "scheme": request.url.scheme,
        "forwarded_headers": forwarded or None,
        "query_string": request.url.query or None,
    }
