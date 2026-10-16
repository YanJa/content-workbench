"""应用设置路由：目前只有「公众号管理」。

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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.core.tenant import current_owner
from app.db.session import get_db
from app.models.settings import AppSetting, WeChatAccount
from app.schemas.models import (
    AuthorSettingRequest,
    WeChatAccountCreate,
    WeChatAccountOut,
    WeChatAccountUpdate,
)
from app.services.wechat import WeChatClient, probe_egress_ip

router = APIRouter(prefix="/settings", tags=["设置"])

# 作者信息的设置键（存在 app_settings 表里）
_AUTHOR_NAME_KEY = "author_name"
_AUTHOR_INTRO_KEY = "author_intro"

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
    """写一条设置值（upsert）。"""
    row = (
        await db.execute(
            select(AppSetting).where(AppSetting.owner == owner, AppSetting.key == key)
        )
    ).scalar_one_or_none()
    if row:
        row.value = value
        row.updated_at = int(time.time())
    else:
        db.add(AppSetting(owner=owner, key=key, value=value))
    await db.flush()


def _qrcode_url(owner: str, qrcode_path: str) -> str:
    """把二维码相对路径拼成可直连的 URL（带 ?owner= 做租户隔离）。

    路径形如 `qrcodes/{account_id}/{filename}`，因此拆成三段。
    """
    if not qrcode_path:
        return ""
    parts = qrcode_path.split("/")
    if len(parts) != 3:
        return ""
    _, aid, fname = parts
    return f"{settings.API_PREFIX}/settings/wechat/qrcodes/{aid}/{fname}?owner={owner}"


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
async def list_accounts(db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)):
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
    owner: str = Depends(current_owner),
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
    owner: str = Depends(current_owner),
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
    account_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)
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
    account_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)
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
    account_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)
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
    owner: str = Depends(current_owner),
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
    owner: str = Depends(current_owner),
):
    """二维码静态服务（按 owner 隔离）。

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
async def get_author(db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)):
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
    owner: str = Depends(current_owner),
):
    """保存作者默认信息。"""
    await _set_setting(db, owner, _AUTHOR_NAME_KEY, req.author_name.strip())
    await _set_setting(db, owner, _AUTHOR_INTRO_KEY, req.author_intro.strip())
    await db.commit()
    return {"ok": True}
