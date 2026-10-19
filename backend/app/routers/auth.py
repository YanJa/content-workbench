"""认证路由：注册 / 登录 / 当前用户。

注册策略（settings.AUTH_REGISTER_MODE）
------------------------------------
- **第一个用户（users 表为空）永远放行**，并在同一事务里「认领」存量数据
  （owner='default' → 该用户 id）—— 这是单租户 Demo 期平滑过渡的关键一步，
  否则老数据会挂在已废弃的 default 租户下，新用户什么都看不到。
- 之后按模式：`invite` = 校验邀请码（invite_codes 表，管理员生成，见
  core/invite.py）；`open` = 开放；`closed` = 禁止。

登录限流：按用户名计数，5 次 / 15 分钟。内存实现、重启清零 —— 对这个
量级的工作台够用；真被打爆了再挂 Redis（项目已有 cache 层）。

角色：**注册进来的一律是 member**，管理员由系统在启动时预置
（`core.rbac.ensure_admin_account()`，见 main.py 的 lifespan）。
刻意不做「第一个注册的人自动当管理员」—— 那是把权限交给一个时间顺序
而不是一个明确的账号：谁先打开页面谁就是管理员，路人甲也一样。

停用账号登录会被拒，且**文案与「密码错误」区分开** —— 否则用户会一直
以为是自己记错了密码。
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core import invite as invite_mod
from app.core.logging import logger
from app.core.rbac import ROLE_MEMBER
from app.core.security import create_token, hash_password, link_url, verify_password
from app.core.tenant import current_user, link_auth
from app.db.session import get_db
from app.models.content import Content, QueueItem
from app.models.conversation import Conversation
from app.models.renderjob import RenderJob
from app.models.settings import AppSetting, WeChatAccount
from app.models.usage import LlmUsage
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["认证"])


# ---------------------------------------------------------------- 模型
class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    invite_code: str = ""


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    created_at: int
    # 展示资料（2026-10-19 用户中心上线补的）。都可空 = 用户没填过，
    # 前端一律 nickname || username 兜底。email 不用于登录。
    nickname: str | None = None
    email: str | None = None
    # 头像签名直链（HMAC 隔离，见 core/security.link_url）。NULL = 未设置，
    # 前端渲染「用户名首字母」圆徽。路径恒定 avatars/<uid>.jpg —— 上传时
    # 后端统一裁剪转码，原格式原尺寸不落地。
    avatar_url: str | None = None
    # 角色与停用状态：前端据此决定菜单显隐（role）与是否踢回登录页
    role: str = ROLE_MEMBER
    is_active: bool = True
    # 细粒度权限与用量快照（按钮级权限上线后新增）。三个都带默认空值：
    # 老调用方（内部构造 UserOut 的地方）不必改，缺了就只是前端少渲染一点。
    # - permissions：生效的权限点 key 列表，前端按钮显隐/置灰的唯一依据；
    # - quotas：生效的配额配置 {feature: {period, limit}}，limit=None 表示不限；
    # - usage：本周期已用量，left=0 就是「今天/本月用完」。
    permissions: list[str] = Field(default_factory=list)
    quotas: dict = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    # 注册时用的邀请码（渠道统计）。空 = 首用户 / 代建 / 预置 admin。
    invite_code: str | None = None


class TokenOut(BaseModel):
    token: str
    user: UserOut
    # 仅首用户注册时非空：告知用户本次接管了多少存量数据
    claimed: dict | None = None


def _avatar_url(user: User) -> str | None:
    """头像签名直链。没设置返回 None（前端回落首字母圆徽）。"""
    if not user.avatar:
        return None
    path = f"{settings.API_PREFIX}/auth/avatar/{user.id}.jpg"
    return link_url(path, user.id)


async def _user_out(user: User, db: AsyncSession | None = None) -> UserOut:
    """用户信息出参。

    `db` 传了才附带权限/配额/用量快照 —— 注册、登录、me 三个端点都有会话，
    一律传上，让前端拿到 token 的同时就知道「能用什么、还剩多少」，
    省掉一次额外的 /me 往返（也避免首屏按钮先全亮再灰掉的闪烁）。
    """
    out = UserOut(
        id=user.id,
        username=user.username,
        created_at=user.created_at,
        nickname=user.nickname,
        email=user.email,
        avatar_url=_avatar_url(user),
        role=user.role,
        is_active=user.is_active,
        invite_code=user.invite_code,
    )
    if db is not None:
        from app.core import quota as quota_mod
        from app.core.rbac import load_permissions

        perms, quotas = await load_permissions(db, user)
        out.permissions = perms
        out.quotas = quotas
        # 只读快照，不写库：复用请求内的会话，不额外开连接
        out.usage = await quota_mod.usage_of(user.id, quotas, db=db)
    return out


# ---------------------------------------------------------------- 校验与限流
_USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")

_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_WINDOW_SEC = 15 * 60
# 单用户名：5 次就锁（挡「盯着一个账号撞库」）
_MAX_PER_USER = 5
# 单 IP：20 次（挡「换着用户名遍历」。nginx 那层 basic auth 摘掉后，
# 登录接口是直接暴露在公网的，这一档是补位；定 20 是因为同一出口 IP
# 后面可能是好几个人，太紧会误伤）
_MAX_PER_IP = 20

# 内网/回源地址不算「客户端」：cloudflared 从本机连 nginx，XFF 链里会带 127.0.0.1
_PRIVATE_PREFIX = ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                   "172.2", "172.30.", "172.31.", "127.", "::1", "fc00:", "fd00:")


def validate_username(raw: str) -> str:
    v = (raw or "").strip()
    if not _USERNAME_RE.fullmatch(v):
        raise HTTPException(422, "用户名只能包含字母、数字、_ 或 -，长度 3~32 位")
    return v


def _client_ip(request: Request) -> str:
    """真实客户端 IP：取 X-Forwarded-For 里第一个非内网地址。

    链路是 浏览器 → Cloudflare → cloudflared(本机) → nginx → 18080 → 后端，
    所以 $remote_addr 恒为 127.0.0.1，只有 XFF 里有真东西。XFF 可被伪造，
    但它只用来兜「按用户名限流」的缺口，伪造了也仍受用户名那档约束。
    """
    for part in (request.headers.get("x-forwarded-for") or "").split(","):
        ip = part.strip()
        if ip and not ip.startswith(_PRIVATE_PREFIX):
            return ip
    return request.client.host if request.client else "unknown"


def _check_rate(key: str, limit: int) -> None:
    now = time.time()
    attempts = [t for t in _ATTEMPTS[key] if now - t < _WINDOW_SEC]
    _ATTEMPTS[key] = attempts
    if len(attempts) >= limit:
        raise HTTPException(429, "尝试次数过多，请 15 分钟后再试")


def _record_fail(key: str) -> None:
    _ATTEMPTS[key].append(time.time())


# ---------------------------------------------------------------- 存量认领
# 所有带 owner 列的表（messages 无 owner，随 conversation 一起归属）
_OWNER_MODELS = [Content, QueueItem, Conversation, RenderJob, AppSetting, WeChatAccount, LlmUsage]

# 存量直链形态：<path>?owner=default（owner 是唯一 query 参数）。
# 捕获组只吃 URL 本体，不碰引号 / 尖括号 / 空白，JSON 与 HTML 里都安全。
_LEGACY_URL_RE = re.compile(r"([^\s\"'<>]+?)\?owner=default\b")


def _rewrite_urls_in_text(text: str, uid: str) -> tuple[str, int]:
    """把文本里所有 `?owner=default` 直链重签成 `?o=<uid>&sig=...`。"""

    def _repl(m: re.Match) -> str:
        return link_url(m.group(1), uid)

    new_text, n = _LEGACY_URL_RE.subn(_repl, text)
    return new_text, n


async def _claim_legacy_data(db: AsyncSession, uid: str) -> dict:
    """首用户注册时，把单租户期的存量数据判给该用户（同事务）。

    三件事：
    1. 所有 `owner='default'`（含 NULL）的行改判为 uid；
    2. 持久化 URL（contents.images / layouts JSON、body_html）重签 ——
       不重签的话存量直链会在认领后全部失效；
    3. 音色目录 `data/voices/profiles` → `data/voices/owners/<uid>/profiles`
       （services/voices.py 的 profiles_dir 早有 owners/<owner> 分支，
       所以这里是纯数据搬迁，代码零改）。
    """
    stats = {"rows": 0, "urls": 0, "voice_dir_moved": False}

    for model in _OWNER_MODELS:
        result = await db.execute(
            update(model)
            .where((model.owner == "default") | (model.owner.is_(None)))
            .values(owner=uid)
        )
        stats["rows"] += result.rowcount or 0

    # URL 重写：JSON 列 dumps 成字符串统一做正则，解析失败就原样跳过
    contents = (await db.execute(select(Content).where(Content.owner == uid))).scalars().all()
    for c in contents:
        changed = False
        for col in ("images", "layouts"):
            raw = getattr(c, col, None)
            if not raw:
                continue
            text = json.dumps(raw, ensure_ascii=False)
            new_text, n = _rewrite_urls_in_text(text, uid)
            if not n:
                continue
            try:
                setattr(c, col, json.loads(new_text))
            except (ValueError, TypeError):
                logger.warning("URL 重写后 JSON 解析失败，跳过该列 content=%s col=%s", c.id, col)
                continue
            stats["urls"] += n
            changed = True
        if c.body_html and "?owner=default" in c.body_html:
            new_html, n = _rewrite_urls_in_text(c.body_html, uid)
            if n:
                c.body_html = new_html
                stats["urls"] += n
                changed = True
        if changed:
            c.updated_at = int(time.time())

    base = Path(settings.VOICE_DIR)
    legacy = base / "profiles"
    target = base / "owners" / uid / "profiles"
    if legacy.is_dir() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        stats["voice_dir_moved"] = True

    return stats


# ---------------------------------------------------------------- 端点
@router.post("/register", response_model=TokenOut)
async def register(
    req: RegisterIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """注册。

    - 首用户永远放行（并触发存量认领）；
    - 之后按 AUTH_REGISTER_MODE：invite 校验邀请码 / open 放行 / closed 拒绝；
    - 按客户端 IP 限流，防「撞邀请码」（邀请码填错也计数）。
      首用户注册豁免 —— 那是把系统交给第一个人的一次性动作，不能被限流挡在门外。
    """
    username = validate_username(req.username)
    if len(req.password) < 8:
        raise HTTPException(422, "密码至少 8 位")

    # 注：系统启动时会预置管理员账号，所以 users 表通常不为空 —— 存量认领
    # 这条「单租户过渡」路径只在「一个用户都没有」的库上才可能触发，
    # 实际上已经退役（老数据都认领完了）。留着是为了空库起服时兜底。
    first_user = (await db.execute(select(User.id).limit(1))).first() is None
    ip_key = f"reg:{_client_ip(request)}"
    if not first_user:
        _check_rate(ip_key, _MAX_PER_IP)

    taken = (await db.execute(select(User.id).where(User.username == username))).first()
    if taken:
        raise HTTPException(409, "用户名已被占用")

    if not first_user:
        mode = settings.AUTH_REGISTER_MODE
        if mode == "closed":
            raise HTTPException(403, "注册通道已关闭")
        if mode == "invite":
            # 新邀请码体系：注册只认 invite_codes 表里的码（管理员在用户管理页
            # 生成）。旧的 .env 全局码在启动时已被 import_legacy_code() 导入为
            # 一条不限次的记录，这里不再单独读 settings.INVITE_CODE。
            try:
                inv = await invite_mod.consume(req.invite_code)
            except HTTPException:
                # 撞码也算一次失败：防「遍历邀请码」（邀请码比密码熵低，
                # 且这里没有用户名维度兜底，只能靠 IP 限流挡撞码）。
                _record_fail(ip_key)
                raise
            invite_code = inv.code
        else:
            invite_code = None
    else:
        invite_code = None

    # 注册进来的一律 member：管理员是系统预置的账号，不靠注册顺序决定
    user = User(
        username=username,
        password_hash=hash_password(req.password),
        role=ROLE_MEMBER,
        invite_code=invite_code,
    )
    db.add(user)
    await db.flush()  # 拿到 user.id，先不提交（认领要和注册同事务）

    claimed: dict | None = None
    if first_user:
        claimed = await _claim_legacy_data(db, user.id)

    await db.commit()
    logger.info("新用户注册: %s (id=%s, 首用户=%s)", username, user.id, first_user)
    if claimed and (claimed["rows"] or claimed["urls"] or claimed["voice_dir_moved"]):
        logger.info("首用户认领存量数据: %s", claimed)
    return TokenOut(
        token=create_token(user.id),
        user=await _user_out(user, db),
        claimed=claimed,
    )


@router.post("/login", response_model=TokenOut)
async def login(req: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    """登录。**双维度限流**：

    - 按用户名 5 次 / 15 分钟 —— 挡盯着一个账号撞库；
    - 按客户端 IP 20 次 / 15 分钟 —— 挡换着用户名遍历。

    只按用户名限流是不够的：攻击者每试一个新名字就重置配额，等于没限。
    """
    username = (req.username or "").strip()
    user_key = f"login:{username}"
    ip_key = f"login-ip:{_client_ip(request)}"
    _check_rate(user_key, _MAX_PER_USER)
    _check_rate(ip_key, _MAX_PER_IP)

    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not verify_password(req.password, user.password_hash):
        _record_fail(user_key)
        _record_fail(ip_key)
        raise HTTPException(401, "用户名或密码错误")

    # 密码对了才判停用，且文案必须和上面区分开：共用一句的话，被停用的人
    # 会一直以为是自己记错了密码，反复重试（还白白吃掉限流配额）。
    if not user.is_active:
        raise HTTPException(401, "账号已被停用，请联系管理员")

    # 登录成功只清当前用户名这一档：IP 那档要留着，
    # 否则「撞库成功一次」就能把整个 IP 的配额洗白
    _ATTEMPTS.pop(user_key, None)
    user.last_login_at = int(time.time())
    await db.commit()
    return TokenOut(token=create_token(user.id), user=await _user_out(user, db))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """当前登录用户。前端启动时用它验证本地 token 是否还有效。

    同时是**权限与用量的权威快照**：管理员刚改了某个人的权限或配额，
    前端在下一个 /me（页面加载、路由切换、60 秒静默刷新）就能拿到新值 ——
    权限点不进 JWT，本来就得每次现算（见 core/rbac.py 的设计口径）。

    `db` 与 `current_user` 里那个是同一个会话（FastAPI 按请求缓存依赖），
    不会多开一条连接。
    """
    return await _user_out(user, db)


# ---------------------------------------------------------------- 个人资料
# 邮箱格式：宽松版（本地 @ 域名，域名至少一个点）。刻意不引 email-validator
# 之类的库 —— 这里的邮箱只是联系方式，不用于登录、不发验证信，
# 格式离谱到收不到信的程度由用户自己承担。
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class ProfileUpdate(BaseModel):
    """个人中心保存的资料。两个字段都可空：

    - 不传（None）= 不改这一项；
    - 传空串 = 清空（回到「未设置」，前端显示名回落 username）；
    - 用户名（username）**不可改** —— 它是登录凭据，改了引用它的地方
      （别人分享的直链不带用户名，但管理员后台按用户名定位人）都会断。
    """

    nickname: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)


@router.put("/profile", response_model=UserOut)
async def update_profile(
    req: ProfileUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """改自己的昵称 / 邮箱。只动当前登录用户（`current_user` 就是凭据），
    不接受 id 参数 —— 改别人资料是用户管理（admin）的职责。
    """
    nickname = req.nickname.strip() if req.nickname is not None else None
    email = req.email.strip() if req.email is not None else None

    if nickname is not None:
        user.nickname = nickname or None
    if email is not None:
        if email and not _EMAIL_RE.fullmatch(email):
            raise HTTPException(422, "邮箱格式不正确")
        if email:
            # 查重排除自己：没改邮箱重复保存不该被 409 挡住
            taken = (
                await db.execute(select(User.id).where(User.email == email, User.id != user.id))
            ).first()
            if taken:
                raise HTTPException(409, "该邮箱已被其他账号使用")
        user.email = email or None

    await db.commit()
    logger.info("用户更新个人资料: %s (id=%s)", user.username, user.id)
    return await _user_out(user, db)


# ---------------------------------------------------------------- 头像
# 为什么上传后要重做：用户会传手机相机直出的 4MB 照片（还带 EXIF 方向），
# 直接落地既占存储又拖慢每一个渲染头像的页面。统一裁成 256×256 JPEG：
# 尺寸恒定（文件名就能用 uid，换图天然覆盖）、体积 ~10-20KB、
# 没有 EXIF/透明度这些跨端幺蛾子。
_AVATAR_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2MB：原图上限，裁完只会更小
_AVATAR_SIZE = 256
_AVATAR_JPEG_Q = 85


def _process_avatar(data: bytes) -> bytes:
    """任意受支持格式的图片字节 → 256×256 居中裁剪 JPEG 字节。

    透明通道先贴白底再转 RGB —— JPEG 没有 alpha，直接转会把透明像素
    变成黑色（naive-ui 顶栏是深色主题的话尤其明显）。
    """
    import io

    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # 手机照片不带这步会躺倒
        img.load()
    except Exception as exc:  # noqa: BLE001 —— Pillow 的异常类型太杂，统一挡
        raise HTTPException(400, "图片解析失败，请换一张试试") from exc

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")

    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if side > _AVATAR_SIZE:
        img = img.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=_AVATAR_JPEG_Q)
    return buf.getvalue()


@router.post("/avatar", response_model=UserOut)
async def upload_avatar(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传/更换自己的头像。裁成 256×256 JPEG 存 `avatars/<uid>.jpg`。

    文件名固定用 uid：换图直接覆盖，**不需要先删旧的**（也不会攒下一堆
    历史文件）。只动当前登录用户 —— 改别人头像是用户管理的事，这里没有
    id 参数可传。
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _AVATAR_SUFFIXES:
        raise HTTPException(400, "头像仅支持 png / jpg / webp 格式")

    data = await file.read()
    if not data:
        raise HTTPException(400, "上传文件为空")
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(400, "图片超过 2MB，请压缩后再传")

    out_dir = Path(settings.UPLOAD_DIR) / "avatars"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{user.id}.jpg").write_bytes(_process_avatar(data))

    user.avatar = f"avatars/{user.id}.jpg"
    await db.commit()
    logger.info("用户更新头像: %s (id=%s)", user.username, user.id)
    return await _user_out(user, db)


@router.delete("/avatar", response_model=UserOut)
async def delete_avatar(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """删除头像，回到「用户名首字母」圆徽。文件删不删无所谓紧要
    （uid 固定名，下次上传直接覆盖），但还是清一下，省得磁盘里躺着
    没人引用的图。"""
    if user.avatar:
        try:
            (Path(settings.UPLOAD_DIR) / user.avatar).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除头像文件失败 %s: %s", user.avatar, exc)
        user.avatar = None
        await db.commit()
        logger.info("用户删除头像: %s (id=%s)", user.username, user.id)
    return await _user_out(user, db)


@router.get("/avatar/{user_id}.jpg")
async def serve_avatar(user_id: str, owner: str = Depends(link_auth)):
    """头像静态服务（HMAC 签名直链隔离，同二维码那套，见 core/security.link_url）。

    路径带 uid 文件名而不是任意文件名：用户只能签自己的头像 URL
    （link_url 绑 uid），拿到别人的链接也过不了签名。
    """
    if not re.fullmatch(r"[0-9a-f]{32}", user_id):
        raise HTTPException(404, "头像不存在")
    f = Path(settings.UPLOAD_DIR) / "avatars" / f"{user_id}.jpg"
    if not f.exists() or not f.is_file():
        raise HTTPException(404, "头像不存在")
    return FileResponse(f, media_type="image/jpeg")
