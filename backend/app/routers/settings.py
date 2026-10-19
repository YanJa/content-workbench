"""应用设置路由：公众号管理 / 作者信息 / 对话供应商。

为什么单独一个 router/前缀：这是「应用级配置」而不是「内容加工」，
挂 /settings 语义清晰，也方便以后放别的设置（如生图脚本路径、代理等）。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.core.rbac import require_admin
from app.core.security import link_url
from app.core.tenant import current_owner_id, link_auth
from app.db.session import get_db
from app.models.settings import AppSetting, WeChatAccount
from app.models.user import User
from app.schemas.models import (
    AuthorSettingRequest,
    ChatProviderRequest,
    WeChatAccountCreate,
    WeChatAccountOut,
    WeChatAccountUpdate,
)
from app.services.wechat import WeChatClient, probe_egress_ip

router = APIRouter(prefix="/settings", tags=["设置"])

# 作者信息的设置键（存在 app_settings 表里）
_AUTHOR_NAME_KEY = "author_name"
_AUTHOR_INTRO_KEY = "author_intro"

# 对话供应商的设置键（chat_provider = deepseek / cloudflare）
_CHAT_PROVIDER_KEY = "chat_provider"
_CF_CHAT_MODEL_KEY = "cloudflare_chat_model"

# CF 模型清单缓存：拉一次 CF API 要 1-3 秒，不能每次打开设置页都拉。
# TTL 10 分钟；失败结果不进缓存（否则修好凭证还要干等一个 TTL）。
_CF_MODELS_CACHE: dict[str, object] = {}  # {account_id: (expire_at, [models])}
_CF_MODELS_TTL = 600

# 二维码允许的图片后缀
_QR_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


async def _get_setting(db: AsyncSession, owner: str, key: str) -> str:
    """取一条设置值，没有返回空串。"""
    row = (
        await db.execute(
            select(AppSetting).where(AppSetting.owner == owner, AppSetting.key == key)
        )
    ).scalar_one_or_none()
    return row.value if row else ""


async def _set_setting(db: AsyncSession, owner: str, key: str, value: str) -> None:
    """写一条设置值（upsert）。

    单条 INSERT ... ON CONFLICT DO NOTHING + UPDATE：select-then-write 在
    并发双写（同一用户两个标签页同时保存）时会撞唯一索引
    ix_app_settings_owner_key 抛 IntegrityError；一条 upsert 语句没这个窗口。
    """
    await db.execute(
        sqlite_insert(AppSetting)
        .values(id=uuid.uuid4().hex, owner=owner, key=key, value=value, updated_at=int(time.time()))
        .on_conflict_do_nothing(index_elements=["owner", "key"])
    )
    await db.execute(
        update(AppSetting)
        .where(AppSetting.owner == owner, AppSetting.key == key)
        .values(value=value, updated_at=int(time.time()))
    )
    await db.flush()


def _qrcode_url(owner: str, qrcode_path: str) -> str:
    """把二维码相对路径拼成可直连的 URL（HMAC 签名做租户隔离）。

    路径形如 `qrcodes/{account_id}/{filename}`，因此拆成三段。
    """
    if not qrcode_path:
        return ""
    parts = qrcode_path.split("/")
    if len(parts) != 3:
        return ""
    _, aid, fname = parts
    return link_url(f"{settings.API_PREFIX}/settings/wechat/qrcodes/{aid}/{fname}", owner)


def _out(a: WeChatAccount) -> WeChatAccountOut:
    """转响应：secret 一律不回传明文，只给布尔「有没有配」。"""
    return WeChatAccountOut(
        id=a.id,
        name=a.name,
        appid=a.appid,
        secret_set=bool(a.secret),
        homepage=a.homepage,
        gzh_name=a.gzh_name or "",
        slogan=a.slogan or "",
        qrcode_url=_qrcode_url(a.owner or "default", a.qrcode_path or ""),
        qrcode_set=bool(a.qrcode_path),
        is_default=a.is_default,
        verified_at=a.verified_at,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


async def _get(db: AsyncSession, account_id: str, owner: str = "default") -> WeChatAccount:
    a = await db.get(WeChatAccount, account_id)
    if not a or (a.owner or "default") != owner:
        raise HTTPException(404, "公众号不存在")
    return a


@router.get("/wechat", response_model=list[WeChatAccountOut])
async def list_accounts(db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)):
    """公众号列表（默认号排最前）。"""
    stmt = (
        select(WeChatAccount)
        .where(WeChatAccount.owner == owner)
        .order_by(WeChatAccount.is_default.desc(), WeChatAccount.created_at.desc())
    )
    return [_out(a) for a in (await db.execute(stmt)).scalars().all()]


@router.post("/wechat", response_model=WeChatAccountOut)
async def create_account(
    req: WeChatAccountCreate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """新增公众号。若这是第一个号，或显式指定 is_default，则设为默认。"""
    existing = (
        await db.execute(select(WeChatAccount).where(WeChatAccount.owner == owner))
    ).scalars().all()
    is_default = req.is_default or len(existing) == 0
    if is_default:
        # 默认号唯一：先把其它的清掉
        await db.execute(
            update(WeChatAccount).where(WeChatAccount.owner == owner).values(is_default=False)
        )
    a = WeChatAccount(
        owner=owner,
        name=req.name.strip() or f"公众号 {req.appid[:6]}",
        appid=req.appid.strip(),
        secret=req.secret.strip(),
        homepage=req.homepage.strip(),
        gzh_name=req.gzh_name.strip(),
        slogan=req.slogan.strip(),
        is_default=is_default,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return _out(a)


@router.put("/wechat/{account_id}", response_model=WeChatAccountOut)
async def update_account(
    account_id: str,
    req: WeChatAccountUpdate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    a = await _get(db, account_id, owner)
    if req.name is not None:
        a.name = req.name.strip()
    if req.appid is not None and req.appid.strip():
        a.appid = req.appid.strip()
    # secret 留空 = 不改（用户改名称时不必回填密钥）
    if req.secret:
        a.secret = req.secret.strip()
        a.verified_at = None  # 换了密钥，之前的验证作废
    if req.homepage is not None:
        a.homepage = req.homepage.strip()
    if req.gzh_name is not None:
        a.gzh_name = req.gzh_name.strip()
    if req.slogan is not None:
        a.slogan = req.slogan.strip()
    a.updated_at = int(time.time())
    await db.commit()
    await db.refresh(a)
    return _out(a)


@router.delete("/wechat/{account_id}")
async def delete_account(
    account_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    a = await _get(db, account_id, owner)
    was_default = a.is_default
    await db.delete(a)
    await db.flush()
    # 删掉的是默认号 → 把剩下的第一个提为默认
    if was_default:
        rest = (
            await db.execute(select(WeChatAccount).where(WeChatAccount.owner == owner))
        ).scalars().all()
        if rest:
            rest[0].is_default = True
    await db.commit()
    return {"ok": True}


@router.post("/wechat/{account_id}/check")
async def check_account(
    account_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    """测试连通性：拉 access_token。成功则回写 verified_at。

    同时附带 `egress` 字段（实际出口 IP + 是否走固定代理），
    方便在「IP 不在白名单」时直接看到该加哪个 IP。
    """
    a = await _get(db, account_id, owner)
    client = WeChatClient(appid=a.appid, secret=a.secret)
    result = await client.check()
    # 附带出口信息，辅助排查白名单（失败时尤其有用）
    try:
        result["egress"] = await probe_egress_ip()
    except Exception as exc:  # noqa: BLE001
        result["egress"] = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        a.verified_at = int(time.time())
        await db.commit()
        logger.info("公众号连通性验证成功 id=%s appid=%s", a.id, a.appid)
    else:
        logger.warning("公众号连通性验证失败 id=%s: %s", a.id, result.get("error"))
    return result


@router.post("/wechat/{account_id}/default", response_model=WeChatAccountOut)
async def set_default(
    account_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    a = await _get(db, account_id, owner)
    await db.execute(
        update(WeChatAccount).where(WeChatAccount.owner == owner).values(is_default=False)
    )
    a.is_default = True
    await db.commit()
    await db.refresh(a)
    return _out(a)


# ---------------------------------------------------------------- 二维码
@router.post("/wechat/{account_id}/qrcode", response_model=WeChatAccountOut)
async def upload_qrcode(
    account_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """上传公众号二维码图（文末关注卡「扫码 / 长按关注」用）。

    独立端点而非复用内容图片上传：二维码属于**账号**、跨文章复用，
    塞进 Content.images 会污染内容图片体系（被列进提示词、被 <!--IMG:n--> 索引）。
    """
    a = await _get(db, account_id, owner)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _QR_SUFFIXES:
        raise HTTPException(400, "二维码仅支持 png / jpg / webp 格式")

    upload_root = Path(settings.UPLOAD_DIR)
    out_dir = upload_root / "qrcodes" / a.id
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex[:12]}{suffix}"
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传文件为空")
    (out_dir / fname).write_bytes(data)

    # 换图：删掉旧文件
    if a.qrcode_path:
        try:
            (upload_root / a.qrcode_path).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除旧二维码失败 %s: %s", a.qrcode_path, exc)

    a.qrcode_path = str((out_dir / fname).relative_to(upload_root))
    a.updated_at = int(time.time())
    await db.commit()
    await db.refresh(a)
    return _out(a)


@router.get("/wechat/qrcodes/{account_id}/{filename}")
async def serve_qrcode(
    account_id: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(link_auth),
):
    """二维码静态服务（HMAC 签名直链隔离，见 core/security.link_url）。

    注意：本路由必须在 `/wechat/{account_id}` 之类的**动态路由之前**语义清晰，
    但因为它带三段路径（qrcodes/{id}/{file}），不会和 `PUT /wechat/{id}` 冲突。
    """
    # 防路径穿越
    if not account_id or not filename or "/" in account_id or "/" in filename or ".." in filename:
        raise HTTPException(400, "非法路径")
    a = await _get(db, account_id, owner)
    f = Path(settings.UPLOAD_DIR) / "qrcodes" / a.id / filename
    if not f.exists() or not f.is_file():
        raise HTTPException(404, "二维码不存在")
    return FileResponse(f)


# ---------------------------------------------------------------- 作者信息
@router.get("/author")
async def get_author(db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)):
    """取作者默认信息（未配置返回空串）。

    排版时用它替换正文里的 `{{作者名}}` / `{{一句话简介}}` 占位符。
    """
    return {
        "author_name": await _get_setting(db, owner, _AUTHOR_NAME_KEY),
        "author_intro": await _get_setting(db, owner, _AUTHOR_INTRO_KEY),
    }


@router.put("/author")
async def put_author(
    req: AuthorSettingRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """保存作者默认信息。"""
    await _set_setting(db, owner, _AUTHOR_NAME_KEY, req.author_name.strip())
    await _set_setting(db, owner, _AUTHOR_INTRO_KEY, req.author_intro.strip())
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 对话供应商
@router.get("/chat-provider")
async def get_chat_provider(
    db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    """取对话供应商设置（provider + 已选 CF 模型）。

    未配置过返回 {provider: "deepseek", model: ""} —— 老用户零感知。
    """
    return {
        "provider": await _get_setting(db, owner, _CHAT_PROVIDER_KEY) or "deepseek",
        "model": await _get_setting(db, owner, _CF_CHAT_MODEL_KEY),
        # CF 凭证是否就位：前端据此决定模型下拉是可点还是禁用+引导
        "cloudflare_ready": bool(
            settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID
        ),
    }


@router.put("/chat-provider")
async def put_chat_provider(
    req: ChatProviderRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    # 供应商切换收归管理员：Cloudflare 通道不支持对话内工具、计费按估价，
    # 让每个普通成员随手切，出问题（对话里调不了工具、用量对不上账）时
    # 定位成本高。GET 仍然全员开放 —— 谁都需要知道自己当前在哪个通道上。
    _admin: User = Depends(require_admin),
):
    """保存对话供应商。

    model 的校验放宽处理：CF 模型清单接口可用时校验「在清单内」，
    清单拉取失败（网络抖动）时不堵死保存，只要非空即可 —— 设置存错模型
    的后果是下一次对话报错（llm 层会转成前端可见的 error 事件），可恢复。
    """
    model = req.model.strip()
    if req.provider == "cloudflare":
        if not (settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID):
            raise HTTPException(400, "未配置 Cloudflare 凭证（.env 的 CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID）")
        if model:
            try:
                known = {m["name"] for m in await _fetch_cf_models()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("CF 模型清单拉取失败，跳过 model 校验: %s", exc)
                known = set()
            if known and model not in known:
                raise HTTPException(400, f"模型 {model} 不在当前账号的可用清单里")
    else:
        model = ""  # 切回 DeepSeek 时清掉 CF 模型选择，防止串味
    await _set_setting(db, owner, _CHAT_PROVIDER_KEY, req.provider)
    await _set_setting(db, owner, _CF_CHAT_MODEL_KEY, model)
    await db.commit()
    return {"ok": True, "provider": req.provider, "model": model}


async def _fetch_cf_models() -> list[dict[str, str]]:
    """拉 Cloudflare 账号下可用于对话的模型清单（带 TTL 缓存）。

    返回 [{name, label}]。task 过滤参数以实测为准：先试带 task=text-generation，
    失败则退化为拉全量本地过滤。deprecated 的模型剔除（还能调，但别让人选）。
    """
    import httpx

    if not (settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID):
        raise HTTPException(400, "未配置 Cloudflare 凭证（.env 的 CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID）")

    account_id = settings.CLOUDFLARE_ACCOUNT_ID
    now = time.time()
    cached = _CF_MODELS_CACHE.get(account_id)
    if cached and cached[0] > now:
        return cached[1]  # type: ignore[return-value]

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search"
    headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare 返回失败: {data.get('errors')}")

    models: list[dict[str, str]] = []
    for m in data.get("result") or []:
        name = m.get("name") or ""
        if not name or not name.startswith("@cf/"):
            continue
        props = m.get("properties") or []
        if any(p.get("name") == "deprecated" for p in props if isinstance(p, dict)):
            continue
        # 只留文本生成类：task 属性在 properties 里（没有该字段的保留，宁多勿漏）
        task = next(
            (p.get("value") for p in props if isinstance(p, dict) and p.get("name") == "task"),
            "",
        )
        if task and task not in ("Text Generation", "Text-to-Text"):
            continue
        desc = (m.get("description") or "").split("\n")[0][:60]
        models.append({"name": name, "label": desc or name})
    if not models:
        raise RuntimeError("账号下没有可用的对话模型")
    # 按名字排一下，@cf/meta/llama 系列在前（默认模型优先可见）
    models.sort(key=lambda x: (0 if "llama" in x["name"] else 1, x["name"]))
    _CF_MODELS_CACHE[account_id] = (now + _CF_MODELS_TTL, models)
    return models


@router.get("/cloudflare-models")
async def get_cloudflare_models(owner: str = Depends(current_owner_id)):
    """Cloudflare 可用对话模型清单（设置页模型下拉用）。

    CF 未配凭证 → 400（前端据此禁用下拉并引导去配 .env）；
    拉取失败 → 502 + 具体原因。失败结果不进缓存。
    """
    try:
        models = await _fetch_cf_models()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("CF 模型清单拉取失败: %s", exc)
        raise HTTPException(502, f"模型清单拉取失败：{exc}") from exc
    return {"models": models}
