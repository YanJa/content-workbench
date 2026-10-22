"""Skill 管理路由。

三种录入方式，覆盖「从哪来」的全部现实情况：
  - 上传 SKILL.md（最常见：从 Claude / 别人的技能包拷过来）
  - 粘贴正文（懒得拼文件，直接贴）
  - 手表单（填 name + description + 正文）

打包 .zip 暂不支持：阶段一只做「正文注入」，附属文件不会进上下文，
收下 zip 却用不上其中的 references/ 反而是误导。等阶段二做了按需读文件再加。
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


class SkillCreate(BaseModel):
    text: str = Field(..., description="完整的 SKILL.md 内容（含 frontmatter）")


class SkillPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = None


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
    )


@router.get("", response_model=list[SkillOut])
async def list_skills(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """当前用户的全部技能（含解析失败的，前端要能看到原因）。"""
    return [_out(s) for s in await skill_svc.list_skills(db, owner)]


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
    """上传 SKILL.md。文件名参与 name 校验（等价于「name 必须等于父目录名」）。"""
    filename = file.filename or "SKILL.md"
    if not filename.lower().endswith((".md", ".markdown", ".txt")):
        raise HTTPException(400, "请上传 .md 文件（SKILL.md）")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件是空的")
    if len(raw) > SKILL_TEXT_MAX:
        raise HTTPException(400, "文件过大")

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

    try:
        skill, _created = await skill_svc.upsert_skill(db, owner, text, filename)
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
    """启用/停用、改名。"""
    if req.enabled is not None:
        skill = await skill_svc.set_enabled(db, owner, skill_id, req.enabled)
        if skill is None:
            raise HTTPException(404, "技能不存在")
    if req.name is not None:
        skill, err = await skill_svc.rename_skill(db, owner, skill_id, req.name)
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
