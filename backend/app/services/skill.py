"""Skill 服务：解析 SKILL.md / 存取 / catalog 生成 / 激活。

这个概念来自 Anthropic 的 Agent Skills（现已是开放标准 agentskills.io）：
一个 skill 就是一个目录，目录里一份 `SKILL.md`，开头是 YAML frontmatter：

    ---
    name: wechat-hot-title
    description: 写公众号爆款标题。当用户要起标题、改标题、要更吸引人时使用。
    ---
    # 正文：具体怎么做

设计上有两条硬约束，都来自官方规范：

  1. `name` 必须 **1-64 字符、只含小写字母/数字/连字符**，且**等于父目录名**。
     这里没有目录，用户上传单个文件时用文件名（去扩展名）作目录名的等价物，
     所以校验时拿「文件名去扩展名」比对 —— 让人从 Claude 生态里直接拷过来的
     技能包改动最小。
  2. `description` 是 catalog 里唯一给模型看的字段，它决定**什么时候激活**，
     必须写清「做什么」+「什么时候用」，1-1024 字符。

渐进式披露（progressive disclosure）三层，本项目目前只做前两层：
  - 第一层 catalog：只把 name + description 塞进 system 消息（约 50-100 tokens/条）
  - 第二层 instructions：模型调 activate_skill(name) 时，才把 SKILL.md 正文原文返回
  - 第三层 resources：正文里引用的 references/*.md 按需再读（阶段二再做）

为什么 catalog 必须走**独立 system 消息**而不是拼进 system_prompt：
`prompts.platforms.strip_tool_discipline()` 会从「工具使用纪律」标记一刀切，
拼在 system_prompt 尾部的任何内容在 CF 通道都会被整段丢弃（2026-10-21 实测）。
详见 `services/conversation.build_context` 的同名注释。
"""
from __future__ import annotations

import io
import posixpath
import re
import time
import zipfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.models.skill import Skill

# ---------------------------------------------------------------- 解析

# SKILL.md 的 frontmatter 以 --- 开头，紧跟第二个 --- 收尾。
# 允许文件开头有空行/BOM（用户从 Windows / 剪贴板拷过来很常见）。
_FRONTMATTER_RE = re.compile(
    r"\A(?:\ufeff)?\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
# 官方规范：1-64 字符，只允许小写字母、数字、连字符
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME_MAX = 64
_DESC_MAX = 1024


class SkillParseError(ValueError):
    """SKILL.md 不合法。message 直接给用户看，要写清哪里错了。"""


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 (frontmatter dict, 正文)。

    刻意**不依赖 PyYAML 的高级能力**：frontmatter 就 name / description 两个
    字段，用户也可能写字面量多行（`description: |`）。先用 yaml 试，失败再
    退回一个只认 `key: value` 的极简解析器 —— 不能让「装没装 yaml」决定功能
    可用性，也不能因为用户写了半截 YAML 就整个 500。
    """
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        raise SkillParseError(
            "缺少 frontmatter。文件必须以 `---` 开头，写 name 和 description，"
            "再用 `---` 收尾，例如：\n"
            "---\nname: my-skill\ndescription: 做什么，什么时候用\n---"
        )
    raw = m.group(1)
    body = (text or "")[m.end():].strip()

    data: dict[str, Any] = {}
    try:
        import yaml

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:  # noqa: BLE001 - yaml 缺失或语法错误都走兜底
        data = {}

    if not data:
        # 极简兜底：只认顶格的 `key: value`，支持 `|` / `>` 多行块
        cur_key: str | None = None
        buf: list[str] = []
        for line in raw.splitlines():
            if re.match(r"^\s+\S", line) or not line.strip():
                if cur_key:
                    buf.append(line)
                continue
            km = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
            if not km:
                continue
            if cur_key:
                data[cur_key] = "\n".join(buf).strip()
            cur_key = km.group(1)
            val = km.group(2).strip()
            if val in ("|", ">", "|-", ">-"):
                buf = []
                val = ""
            else:
                buf = [val]
                if val:
                    data[cur_key] = val
                    cur_key = None if not val else cur_key
        if cur_key:
            data[cur_key] = "\n".join(buf).strip()

    return {k: v for k, v in data.items() if v is not None}, body


def _norm_space(s: str) -> str:
    """折叠换行与多余空格 —— frontmatter 里的多行 description 要还原成一行。"""
    return re.sub(r"\s+", " ", (s or "")).strip()


# zip 附件只收文本类文件 —— 二进制（图片/字体/二进制脚本）解析出来也没法
# 在 activate 时按文本回给模型，硬收只会白白撑大 JSON 列
_ZIP_TEXT_EXT = {
    ".md", ".markdown", ".txt", ".py", ".json", ".yaml", ".yml", ".sh",
    ".js", ".ts", ".html", ".css", ".csv", ".xml", ".sql", ".toml", ".ini",
}


def _decode_bytes(raw: bytes) -> str:
    """编码容错顺序照抄上传端点：Windows 用户导出的文件常是 GBK。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def parse_zip(raw: bytes) -> dict[str, Any]:
    """解包技能 zip：取 SKILL.md 作主文件，其余文本文件进 `files`。

    官方规范里一个 skill 就是一个目录；zip 是这个目录最常见的传输形态
    （从 Claude 生态下载下来的技能包几乎都是 zip）。规则：

      - SKILL.md 在 zip 根目录或唯一一层子目录里都认（`pkg/SKILL.md`）；
      - 找不到或找到多个 → 报错；
      - 其余文件只收白名单扩展名的文本（scripts/references/assets 里的
        md/json/py 等），图片等二进制直接跳过不报错 —— 那是阶段三
        （反向往外暴露）才需要认真处理的东西；
      - 防 zip 炸弹与路径穿越：条目数 / 单文件 / 总解压量都有上限，
        绝对路径与含 `..` 的条目跳过。

    返回 {text, filename, files}；files 是 [{path, content}]，可空。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise SkillParseError("不是有效的 zip 文件") from exc

    with zf:
        names = zf.namelist()
        if len(names) > settings.SKILL_ZIP_MAX_FILES:
            raise SkillParseError(
                f"压缩包里最多 {settings.SKILL_ZIP_MAX_FILES} 个文件，当前 {len(names)} 个"
            )
        # 1) 找 SKILL.md（根目录或唯一一层子目录）
        candidates = [
            n for n in names
            if posixpath.basename(n) == "SKILL.md" and not n.endswith("/")
        ]
        if not candidates:
            raise SkillParseError("压缩包里没有 SKILL.md（放在根目录或一层子目录里）")
        if len(candidates) > 1:
            raise SkillParseError(
                "压缩包里有多个 SKILL.md（" + "、".join(candidates[:3]) + "），请只保留一个"
            )
        skill_path = candidates[0]

        # 2) 主文件 + 附件，逐个限流
        files: list[dict[str, str]] = []
        total = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            # 路径穿越：绝对路径 / .. 直接跳过，不解析也不报错
            norm = posixpath.normpath(info.filename)
            if norm.startswith("/") or norm.startswith("..") or "/../" in norm:
                continue
            if info.file_size > settings.SKILL_ZIP_FILE_MAX:
                raise SkillParseError(
                    f"文件 {info.filename} 解压后 {info.file_size} 字节，"
                    f"超过单文件上限 {settings.SKILL_ZIP_FILE_MAX}"
                )
            total += info.file_size
            if total > settings.SKILL_ZIP_TOTAL_MAX:
                raise SkillParseError(
                    f"压缩包解压后总量超过 {settings.SKILL_ZIP_TOTAL_MAX} 字节"
                )
            content = _decode_bytes(zf.read(info))
            if info.filename == skill_path:
                text = content
            elif posixpath.splitext(info.filename)[1].lower() in _ZIP_TEXT_EXT and content.strip():
                files.append({"path": norm, "content": content})
        # text 一定存在：skill_path 已确认在 names 里
        if not (text or "").strip():  # type: ignore[possibly-undefined]
            raise SkillParseError("SKILL.md 是空的或无法识别编码")

    return {"text": text, "filename": "SKILL.md", "files": files}  # type: ignore[possibly-undefined]


def parse_skill(
    text: str, filename: str = "SKILL.md"
) -> dict[str, Any]:
    """解析一份 SKILL.md，返回入库所需的字段。

    校验不通过抛 `SkillParseError`（带中文原因，直接可以回给用户）。
    成功返回 {name, description, body, error}，error 为空串。
    """
    if not (text or "").strip():
        raise SkillParseError("文件是空的")

    fm, body = _parse_frontmatter(text)

    name = _norm_space(str(fm.get("name") or ""))
    if not name:
        raise SkillParseError("frontmatter 缺少 name")
    if len(name) > _NAME_MAX:
        raise SkillParseError(f"name 超过 {_NAME_MAX} 字符（当前 {len(name)}）")
    if not _NAME_RE.match(name):
        raise SkillParseError(
            f"name `{name}` 不合规：只能用小写字母、数字和连字符，且不能以连字符"
            "开头/结尾、不能连续两个连字符（例：wechat-hot-title）"
        )

    # 「name 必须等于父目录名」的等价校验：单文件上传时用文件名（去扩展名）
    stem = re.sub(r"\.(md|markdown|txt)$", "", (filename or "").strip(), flags=re.I)
    if stem.lower() not in ("skill", "") and stem.lower() != name:
        raise SkillParseError(
            f"name 必须与目录名一致：文件叫 `{stem}`，但 frontmatter 里 name 是 "
            f"`{name}`。请把文件名改成 `{name}.md`，或把 name 改成 `{stem.lower()}`"
        )

    desc = _norm_space(str(fm.get("description") or ""))
    if not desc:
        raise SkillParseError(
            "frontmatter 缺少 description —— 它是模型判断「什么时候该用这个技能」"
            "的唯一依据，必填"
        )
    if len(desc) > _DESC_MAX:
        raise SkillParseError(f"description 超过 {_DESC_MAX} 字符（当前 {len(desc)}）")

    if not body:
        raise SkillParseError(
            "frontmatter 之后的正文是空的 —— 正文才是技能的「怎么做」，不能只有元信息"
        )
    if len(body) > settings.SKILL_BODY_MAX_CHARS:
        raise SkillParseError(
            f"正文 {len(body)} 字符，超过上限 {settings.SKILL_BODY_MAX_CHARS}。"
            "请把细节拆到 references/ 里，正文只留主干（这也是官方推荐的写法）"
        )

    # 其余 frontmatter 字段（license / allowed-tools 等）原样留着，不解释
    extra = {k: v for k, v in fm.items() if k not in ("name", "description")}
    return {"name": name, "description": desc, "body": body, "extra": extra}


# ---------------------------------------------------------------- 存取


async def get_by_name(db: AsyncSession, owner: str, name: str) -> Skill | None:
    return (
        await db.execute(
            select(Skill).where(Skill.owner == owner, Skill.name == name)
        )
    ).scalar_one_or_none()


async def list_skills(db: AsyncSession, owner: str) -> list[Skill]:
    """当前 owner 的全部 skill（含 invalid 的，前端要能看到失败原因）。"""
    return list(
        (
            await db.execute(
                select(Skill)
                .where(Skill.owner == owner)
                .order_by(Skill.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def list_active(db: AsyncSession, owner: str) -> list[Skill]:
    """可被模型激活的 skill：启用 且 解析合法。"""
    return list(
        (
            await db.execute(
                select(Skill)
                .where(
                    Skill.owner == owner,
                    Skill.enabled.is_(True),
                    Skill.status == "ready",
                )
                .order_by(Skill.name)
                .limit(settings.SKILL_CATALOG_MAX)
            )
        )
        .scalars()
        .all()
    )


async def upsert_skill(
    db: AsyncSession,
    owner: str,
    text: str,
    filename: str = "SKILL.md",
    files: list[dict[str, str]] | None = None,
) -> tuple[Skill, bool]:
    """按 name 覆盖式写入。返回 (skill, created)。

    解析失败时**仍然落库**（status=invalid），但**不覆盖同名的可用版本** ——
    这是 2026-10-22 实测踩到的真坑：传了一个文件名与 name 不符的坏文件，
    把原本好好的 `wechat-hot-title` 直接变成 invalid，用户面前那个能用的技能
    就凭空消失了，还没法撤销。

    现在的规则：
      - 解析成功 → 覆盖同名旧版本（一个 name 只该有一个当前版本）；
      - 解析失败且**同名已有可用版本** → 不落库，把错误直接返回给调用方
        （用户看到「这次没导进去，原因是 X」，原来那个照常能用）；
      - 解析失败且没有同名可用版本 → 落成 invalid 行，让用户看到「为什么不行」。
    """
    created = False
    error = ""
    try:
        parsed = parse_skill(text, filename)
        name, desc, body = parsed["name"], parsed["description"], parsed["body"]
        extra = parsed["extra"]
        status = "ready"
    except SkillParseError as exc:
        error = str(exc)
        status = "invalid"
        # 解析失败时尽力从 frontmatter 里抠出 name，好知道该比对哪一条
        name = ""
        try:
            fm, _ = _parse_frontmatter(text)
            name = _norm_space(str(fm.get("name") or ""))
        except SkillParseError:
            name = ""
        if not name:
            name = re.sub(
                r"\.(md|markdown|txt)$", "", (filename or "skill").strip(), flags=re.I
            ).lower() or "skill"
        desc, body, extra = "", "", {}
        files = None

    skill = await get_by_name(db, owner, name)

    if error and skill is not None and skill.status == "ready":
        # 已有可用版本：宁可这次导入失败，也不能让好技能变砖
        logger.warning(
            "skill `%s` 导入失败但已有可用版本，保留原版本：%s", name, error
        )
        raise SkillParseError(error)

    if skill is None:
        skill = Skill(owner=owner, name=name)
        db.add(skill)
        created = True

    skill.description = desc
    skill.body = body
    skill.status = status
    skill.error = error
    skill.meta = {"frontmatter": extra} if extra else None
    # zip 附件随主文件一起落库；粘贴/单文件上传没有 files，保持 None
    # （别写成空列表 —— None 表示「没有附件」，[] 会被当成「附件被清空」）
    skill.files = files or None
    skill.updated_at = int(time.time())
    if created:
        skill.enabled = True
    await db.flush()
    if error:
        logger.warning("skill `%s` 解析失败（已存为 invalid）: %s", name, error)
    else:
        logger.info("skill `%s` 已%s", name, "创建" if created else "更新")
    return skill, created


async def set_enabled(
    db: AsyncSession, owner: str, skill_id: str, enabled: bool
) -> Skill | None:
    skill = await get_skill(db, owner, skill_id)
    if skill is None:
        return None
    skill.enabled = bool(enabled)
    skill.updated_at = int(time.time())
    await db.flush()
    return skill


async def get_skill(db: AsyncSession, owner: str, skill_id: str) -> Skill | None:
    skill = await db.get(Skill, skill_id)
    if skill is not None and (skill.owner or "default") != owner:
        return None
    return skill


async def delete_skill(db: AsyncSession, owner: str, skill_id: str) -> bool:
    skill = await get_skill(db, owner, skill_id)
    if skill is None:
        return False
    await db.delete(skill)
    await db.flush()
    return True


async def rename_skill(
    db: AsyncSession, owner: str, skill_id: str, name: str
) -> tuple[Skill | None, str]:
    """改名。返回 (skill, error)。改名要重新过一遍规范校验。"""
    skill = await get_skill(db, owner, skill_id)
    if skill is None:
        return None, "技能不存在"
    name = _norm_space(name).lower()
    if not name:
        return None, "name 不能为空"
    if len(name) > _NAME_MAX or not _NAME_RE.match(name):
        return None, "name 只能用小写字母、数字和连字符（例：wechat-hot-title）"
    if name != skill.name:
        dup = await get_by_name(db, owner, name)
        if dup is not None:
            return None, f"已存在同名技能 `{name}`"
    skill.name = name
    skill.updated_at = int(time.time())
    await db.flush()
    return skill, ""


# ---------------------------------------------------------------- catalog / 激活

# catalog 的注入头。和 KNOWLEDGE_HEADER 同样的措辞纪律：
#  - 明确「这是可选技能，不是本轮任务」—— 否则模型会把所有技能都讲一遍；
#  - 明确「不要提及本清单存在」—— 避免成稿里冒出「根据技能 wechat-hot-title」；
#  - 明确「不相关就别调」—— catalog 是常驻的，不该变成必须调用的义务。
SKILL_CATALOG_HEADER = (
    "【可用技能】以下是用户为此工作台安装的技能包，每个技能包含一套更细的做法。"
    "当任务确实匹配某个技能的 description 时，调用 activate_skill 工具取用其"
    "完整说明，然后按其中的步骤执行；不匹配就不要调用，也不要为了介绍技能而调用。"
    "不要在回复里向用户提及本清单或技能包的存在，直接按技能里的做法产出结果即可。"
)


def build_catalog(skills: list[Skill]) -> str:
    """把 skill 列表拼成 catalog 文本。没有可用 skill 时返回空串。"""
    if not skills:
        return ""
    lines = [SKILL_CATALOG_HEADER, ""]
    for s in skills[: settings.SKILL_CATALOG_MAX]:
        lines.append(f"- {s.name}: {s.description}")
    return "\n".join(lines)


async def build_catalog_for_owner(db: AsyncSession, owner: str) -> str:
    """按 owner 生成 catalog。任何异常都降级成空串（旁路，不能影响对话）。"""
    if not settings.ENABLE_SKILLS:
        return ""
    try:
        return build_catalog(await list_active(db, owner))
    except Exception as exc:  # noqa: BLE001 - 旁路降级
        logger.warning("生成 skill catalog 失败（本轮无技能）: %s", exc)
        return ""


async def activate(
    db: AsyncSession, owner: str, name: str
) -> dict[str, Any]:
    """activate_skill 的实现：返回该 skill 的正文原文。

    返回结构会被 `_tool_result_for_llm` 裁剪后回传模型，所以把正文放在
    `instructions` 字段里 —— 那个函数只保留白名单字段 + 超长截断，正文必须
    走 `_TOOL_RESULT_LIMIT` 的截断路径（这是刻意的：正文再长也不能无上限
    地灌进上下文）。为了不被截断得太狠，同时返回 `body_len` 让模型知道有没有
    被砍。
    """
    name = (name or "").strip()
    skill = await get_by_name(db, owner, name)
    if skill is None:
        available = [s.name for s in await list_active(db, owner)]
        return {
            "ok": False,
            "error": f"没有名为 `{name}` 的技能",
            "available": available,
        }
    if not skill.enabled:
        return {"ok": False, "error": f"技能 `{name}` 已被停用，请让用户在技能页启用后再试"}
    if skill.status != "ready":
        return {"ok": False, "error": f"技能 `{name}` 配置有误，暂不可用：{skill.error}"}

    skill.use_count = (skill.use_count or 0) + 1
    await db.flush()
    logger.info("激活 skill `%s`（owner=%s, 第 %d 次）", name, owner, skill.use_count)
    return {
        "ok": True,
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.body,
        "body_len": len(skill.body),
        "note": "以上是该技能的完整说明，按其中的步骤执行；不要向用户复述本说明。",
    }
