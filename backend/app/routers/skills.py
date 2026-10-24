"""Skill 管理路由。

四种录入方式，覆盖「从哪来」的全部现实情况：
  - 上传 SKILL.md 或技能 zip 包（最常见：从 Claude / 别人的技能包拷过来）
  - 从链接导入（知乎开放平台等会直接给下载链接，省一次「先下载再上传」）
  - 粘贴正文（懒得拼文件，直接贴）
  - 手表单（填 name + description + 正文）

zip 的规则见 parse_zip：SKILL.md 为准，文本附件进 files，防炸弹 + 挡路径穿越。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rbac import require_use
from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.models.skill import Skill
from app.models.user import User
from app.services import skill as skill_svc

router = APIRouter(prefix="/skills", tags=["技能"])

# 技能包文本上限（字符）。一个 SKILL.md 通常 2~10KB，1MB 是远高于合理的兜底。
SKILL_TEXT_MAX = 1_000_000
# zip 是压缩传输，原始字节上限放宽到 5MB（解压后的量在 parse_zip 里另有限流）
SKILL_ZIP_MAX = 5_000_000


class SkillOut(BaseModel):
    id: str
    name: str
    description: str
    body: str
    enabled: bool
    status: str
    error: str
    use_count: int
    created_at: int
    updated_at: int
    # zip 里的附件（[{path, content}]）。列表只回 path 不回 content ——
    # 附件正文可能上百 KB，列表页/详情页只需要知道有哪些文件；
    # 真要读内容是阶段三（activate 时按需展开）的事。
    files: list[str] = []


class SkillCreate(BaseModel):
    text: str = Field(..., description="完整的 SKILL.md 内容（含 frontmatter）")


class SkillPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = None
    # 完整的 SKILL.md 内容（含 frontmatter）。name 必须与现有一致——
    # 改名走 name 字段，两边查重/规范校验不重复。
    text: str | None = None


def _out(s: Skill) -> SkillOut:
    return SkillOut(
        id=s.id,
        name=s.name,
        description=s.description,
        body=s.body,
        enabled=bool(s.enabled),
        status=s.status,
        error=s.error or "",
        use_count=s.use_count or 0,
        created_at=s.created_at,
        updated_at=s.updated_at,
        files=[f.get("path", "") for f in (s.files or [])],
    )


@router.get("", response_model=list[SkillOut])
async def list_skills(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """当前用户的全部技能（含解析失败的，前端要能看到原因）。"""
    return [_out(s) for s in await skill_svc.list_skills(db, owner)]


@router.get("/builtins")
async def list_builtins(_owner: str = Depends(current_owner_id)):
    """随代码发布的内置技能（只读清单，不含正文）。

    单独一个端点而不是并进列表：内置技能**没有库行**，没有 id、不能删、不能
    停用 —— 混进列表里前端会给出一堆点了就失败的操作按钮。这里只给名字和
    description，让用户知道「本来就有这些能力」，想改就传一个同名版本覆盖。
    """
    return {
        "items": [
            {"name": s.name, "description": s.description}
            for s in skill_svc._builtin_skills()
        ]
    }


@router.post("", response_model=SkillOut)
async def create_skill(
    req: SkillCreate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("skill.manage", "skill")),
):
    """粘贴 / 手填的方式录入。同名覆盖。

    注意：解析失败**不是** 4xx —— 落库为 status=invalid 并把原因放进 error，
    让用户在自己的技能列表里看到「这条为什么没生效」。这一点和上传一致。
    """
    if not (req.text or "").strip():
        raise HTTPException(400, "内容不能为空")
    if len(req.text) > SKILL_TEXT_MAX:
        raise HTTPException(400, "内容过大")
    try:
        skill, _created = await skill_svc.upsert_skill(db, owner, req.text, "SKILL.md")
    except skill_svc.SkillParseError as exc:
        # 同名已有可用版本时不覆盖，直接把原因回给用户（400 而不是落成 invalid）
        raise HTTPException(400, f"格式有误，原技能未改动：{exc}") from exc
    await db.commit()
    return _out(skill)


@router.post("/upload", response_model=SkillOut)
async def upload_skill(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("skill.manage", "skill")),
):
    """上传 SKILL.md 或技能 zip 包。文件名参与 name 校验（等价于「name 必须等于父目录名」）。

    zip 是官方「一个 skill 一个目录」的传输形态：解包取 SKILL.md 作主文件，
    其余文本文件（references/scripts 里的 md/json/py 等）存进 files 字段。
    """
    filename = file.filename or "SKILL.md"
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件是空的")
    if filename.lower().endswith(".zip"):
        if len(raw) > SKILL_ZIP_MAX:
            raise HTTPException(400, "zip 包过大")
        try:
            parsed_zip = skill_svc.parse_zip(raw)
        except skill_svc.SkillParseError as exc:
            raise HTTPException(400, f"zip 无法导入：{exc}") from exc
        text, name_for_parse, files = (
            parsed_zip["text"],
            parsed_zip["filename"],
            parsed_zip["files"],
        )
    elif filename.lower().endswith((".md", ".markdown", ".txt")):
        if len(raw) > SKILL_TEXT_MAX:
            raise HTTPException(400, "文件过大")
        files = None
        name_for_parse = filename
        # 编码容错顺序照抄 services/doc_extract 的惯例：Windows 用户导出的
        # SKILL.md 常是 GBK，硬要 utf-8 会直接乱码进正文。
        text = ""
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text.strip():
            raise HTTPException(400, "无法识别文件编码，请转成 UTF-8 后重试")
    else:
        raise HTTPException(400, "请上传 .md 文件（SKILL.md）或技能 zip 包")

    try:
        skill, _created = await skill_svc.upsert_skill(db, owner, text, name_for_parse, files)
    except skill_svc.SkillParseError as exc:
        raise HTTPException(400, f"格式有误，原技能未改动：{exc}") from exc
    await db.commit()
    return _out(skill)


class SkillImportUrl(BaseModel):
    url: str = Field(..., description="技能下载链接，.md / .zip 后缀")


# URL 下载的字节上限：zip 官方技能包通常几十 KB~ 几 MB，20MB 是很宽的兜底
SKILL_URL_MAX = 20_000_000


def _check_url_public(url: str) -> None:
    """挡 SSRF：只放行公网地址。

    这个端点会用服务器身份去请求用户填的任意 URL —— 不挡的话，
    填 http://169.254.169.254/ 就能摸到云元数据、填内网地址能探测内网。
    解析域名后逐个 IP 检查，私网 / 环回 / 链路本地 / 保留段全部拒绝。
    重定向后的地址不在检查范围（httpx 会跟），这是已知取舍：
    本平台是单租户自用、下载源是白名单性质的开发站，挡住「直接填内网地址」
    这条最常见的路已经够了。
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise HTTPException(400, "只支持 http/https 链接")
    host = u.hostname or ""
    if not host:
        raise HTTPException(400, "链接格式不对")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise HTTPException(400, f"域名解析失败：{host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(400, "不允许访问内网/保留地址")


async def _fetch_skill_url(url: str) -> tuple[bytes, str]:
    """下载 URL 内容，返回 (原始字节, 从路径截出的文件名)。

    抽成独立函数是为了测试能 monkeypatch 掉网络。
    """
    import httpx

    _check_url_public(url)
    from urllib.parse import urlparse, unquote

    path = urlparse(url).path
    name = unquote(path.rstrip("/").rsplit("/", 1)[-1] or "skill.md")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise HTTPException(400, f"下载失败：HTTP {resp.status_code}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes(65536):
                    size += len(chunk)
                    if size > SKILL_URL_MAX:
                        raise HTTPException(400, "文件过大（上限 20MB）")
                    chunks.append(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"下载失败：{exc}") from exc
    return b"".join(chunks), name


@router.post("/import_url", response_model=SkillOut)
async def import_skill_url(
    req: SkillImportUrl,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("skill.manage", "skill")),
):
    """从链接导入技能：下载 .md / .zip 后走与上传完全相同的解析入库。

    知乎开放平台等站点会直接给一个技能包下载链接（如 hot_list_skills.zip），
    让用户先下载再上传是多余的往返。
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "链接不能为空")
    raw, name = await _fetch_skill_url(url)

    # 分支逻辑与 upload_skill 相同；后缀判断优先看链接路径，
    # 没有 recognizable 后缀时再看 content-type —— 但下载器没回传 header，
    # 简化：靠路径后缀 + zip 魔数嗅探（PK\x03\x04）
    if name.lower().endswith(".zip") or raw[:4] == b"PK\x03\x04":
        if len(raw) > SKILL_ZIP_MAX:
            raise HTTPException(400, "zip 包过大")
        try:
            parsed_zip = skill_svc.parse_zip(raw)
        except skill_svc.SkillParseError as exc:
            raise HTTPException(400, f"zip 无法导入：{exc}") from exc
        text, name_for_parse, files = (
            parsed_zip["text"],
            parsed_zip["filename"],
            parsed_zip["files"],
        )
    elif name.lower().endswith((".md", ".markdown", ".txt")):
        if len(raw) > SKILL_TEXT_MAX:
            raise HTTPException(400, "文件过大")
        files = None
        name_for_parse = name
        text = ""
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text.strip():
            raise HTTPException(400, "无法识别文件编码")
    else:
        raise HTTPException(400, f"链接文件名 {name} 不是 .md / .zip，无法识别技能格式")

    try:
        skill, _created = await skill_svc.upsert_skill(db, owner, text, name_for_parse, files)
    except skill_svc.SkillParseError as exc:
        raise HTTPException(400, f"格式有误，原技能未改动：{exc}") from exc
    await db.commit()
    return _out(skill)


@router.patch("/{skill_id}", response_model=SkillOut)
async def patch_skill(
    skill_id: str,
    req: SkillPatch,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("skill.manage", "skill")),
):
    """启用/停用、改名、改内容（text 传完整 SKILL.md）。"""
    if req.enabled is not None:
        skill = await skill_svc.set_enabled(db, owner, skill_id, req.enabled)
        if skill is None:
            raise HTTPException(404, "技能不存在")
    if req.name is not None:
        skill, err = await skill_svc.rename_skill(db, owner, skill_id, req.name)
        if skill is None:
            raise HTTPException(400 if err else 404, err or "技能不存在")
    if req.text is not None:
        skill, err = await skill_svc.update_skill_text(db, owner, skill_id, req.text)
        if skill is None:
            raise HTTPException(400 if err else 404, err or "技能不存在")
    skill = await skill_svc.get_skill(db, owner, skill_id)
    if skill is None:
        raise HTTPException(404, "技能不存在")
    await db.commit()
    return _out(skill)


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("skill.manage", "skill")),
):
    if not await skill_svc.delete_skill(db, owner, skill_id):
        raise HTTPException(404, "技能不存在")
    await db.commit()
    return {"ok": True}


@router.get("/meta")
async def skills_meta(owner: str = Depends(current_owner_id)):
    """前端展示用的能力开关（配错了也好看出来）。

    值本身不敏感，但仍然要求登录：匿名可读的端点会污染「全站都要鉴权」这条
    不变量（tests/test_auth.py 会把它挑出来），也没必要给爬虫留口子。
    """
    return {
        "enabled": settings.ENABLE_SKILLS,
        "catalog_max": settings.SKILL_CATALOG_MAX,
        "body_max_chars": settings.SKILL_BODY_MAX_CHARS,
    }
