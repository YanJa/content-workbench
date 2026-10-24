"""Skill 解析 / 存取 / catalog / activate_skill 测试。

注意本项目的测试约定（没有 pytest-asyncio）：异步用例一律写成
`def test_x(): asyncio.run(_run())`，用内存 SQLite 自建 session。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.skill import Skill  # noqa: F401 - 让 create_all 看到这张表
from app.services import skill as skill_svc
from app.tools import builtin  # noqa: F401 - 完成工具注册（activate_skill 在里面）
from app.tools.registry import registry


@pytest.fixture(autouse=True)
def _without_builtin_skills():
    """本文件的用例只关心「用户上传的技能」，把内置技能摘掉。

    内置技能（zhihu-content-pack 等）是全局常驻的 —— 见 test_builtin_skills.py。
    它们在的话，「空库 == 没有任何技能」这类断言永远不成立，而这些用例要验的
    恰恰是用户维度（上传 / 停用 / 改名 / 越权），跟内置无关。
    直接改缓存：_builtin_skills() 命中缓存就不会去读目录。
    """
    saved = skill_svc._BUILTIN_CACHE
    skill_svc._BUILTIN_CACHE = []
    yield
    skill_svc._BUILTIN_CACHE = saved

GOOD_MD = """---
name: wechat-hot-title
description: 写公众号爆款标题。当用户要起标题、改标题、要更吸引人时使用。
---
# 怎么做

1. 用数字开头
2. 制造悬念
"""


# ---------------------------------------------------------------- 纯解析


def test_parse_good():
    parsed = skill_svc.parse_skill(GOOD_MD, "wechat-hot-title.md")
    assert parsed["name"] == "wechat-hot-title"
    assert "起标题" in parsed["description"]
    assert parsed["body"].startswith("# 怎么做")


def test_parse_multiline_description_folded():
    text = GOOD_MD.replace(
        "description: 写公众号爆款标题。当用户要起标题、改标题、要更吸引人时使用。",
        "description: >\n  写公众号爆款标题。\n  当用户要起标题时使用。",
    )
    parsed = skill_svc.parse_skill(text, "wechat-hot-title.md")
    # 折叠成一个空格分隔的单行 —— catalog 里每条只能占一行
    assert "\n" not in parsed["description"]
    assert "写公众号爆款标题。 当用户要起标题时使用。" == parsed["description"]


def test_parse_missing_frontmatter():
    with pytest.raises(skill_svc.SkillParseError) as e:
        skill_svc.parse_skill("# 只有正文没有元信息", "x.md")
    assert "frontmatter" in str(e.value)


def test_parse_bad_name_chars():
    bad = GOOD_MD.replace("name: wechat-hot-title", "name: WeChat_Hot Title")
    with pytest.raises(skill_svc.SkillParseError) as e:
        skill_svc.parse_skill(bad, "SKILL.md")
    assert "不合规" in str(e.value)


def test_parse_name_longer_than_64():
    long_name = "a" * 65
    bad = GOOD_MD.replace("name: wechat-hot-title", f"name: {long_name}")
    with pytest.raises(skill_svc.SkillParseError) as e:
        skill_svc.parse_skill(bad, f"{long_name}.md")
    assert "64" in str(e.value)


def test_parse_name_must_match_filename():
    with pytest.raises(skill_svc.SkillParseError) as e:
        skill_svc.parse_skill(GOOD_MD, "some-other-name.md")
    assert "目录名一致" in str(e.value)


def test_parse_name_mismatch_exempt_for_skill_md():
    """叫 SKILL.md 就不比对文件名 —— 官方包里的文件名固定是 SKILL.md。"""
    assert skill_svc.parse_skill(GOOD_MD, "SKILL.md")["name"] == "wechat-hot-title"


def test_parse_description_over_1024():
    long_desc = "描述" * 600
    bad = GOOD_MD.replace(
        "description: 写公众号爆款标题。当用户要起标题、改标题、要更吸引人时使用。",
        f"description: {long_desc}",
    )
    with pytest.raises(skill_svc.SkillParseError) as e:
        skill_svc.parse_skill(bad, "wechat-hot-title.md")
    assert "1024" in str(e.value)


def test_parse_empty_body():
    text = "---\nname: a1\ndescription: 测试用\n---\n\n"
    with pytest.raises(skill_svc.SkillParseError) as e:
        skill_svc.parse_skill(text, "a1.md")
    assert "正文" in str(e.value)


def test_parse_no_yaml_dependency_fallback(monkeypatch):
    """yaml 不可用时极简解析器要能顶上（不能让装没装 yaml 决定功能可用性）。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    parsed = skill_svc.parse_skill(GOOD_MD, "wechat-hot-title.md")
    assert parsed["name"] == "wechat-hot-title"
    assert "起标题" in parsed["description"]


# ---------------------------------------------------------------- 存取


def _run(coro):
    return asyncio.run(coro)


async def _mk_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_upsert_create_then_update(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            s1, created1 = await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            assert created1 is True
            assert s1.status == "ready"
            assert s1.enabled is True

            newer = GOOD_MD.replace("# 怎么做", "# 新做法")
            s2, created2 = await skill_svc.upsert_skill(db, "o1", newer, "SKILL.md")
            await db.commit()
            assert created2 is False
            assert s2.id == s1.id
            assert "新做法" in s2.body
            assert (await skill_svc.list_skills(db, "o1"))[0].id == s1.id
        await engine.dispose()

    _run(go())


def test_upsert_invalid_still_saved(tmp_path):
    """解析失败也落库：用户要看到「为什么没生效」。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            s, _ = await skill_svc.upsert_skill(db, "o1", "没有 frontmatter", "bad-name.md")
            await db.commit()
            assert s.status == "invalid"
            assert s.error
            # 无效的不能被激活
            assert await skill_svc.list_active(db, "o1") == []
        await engine.dispose()

    _run(go())


def test_broken_import_does_not_clobber_good_version(tmp_path):
    """坏文件不能把同名好技能打成 invalid（2026-10-22 实测踩到的坑）。

    现场：先传对了 wechat-hot-title，再传一个文件名与 name 不符的坏文件，
    原本能用的技能直接被覆盖成 invalid，用户面前凭空消失且无法撤销。
    """

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            good, _ = await skill_svc.upsert_skill(db, "o1", GOOD_MD, "wechat-hot-title.md")
            await db.commit()
            assert good.status == "ready"

            with pytest.raises(skill_svc.SkillParseError):
                await skill_svc.upsert_skill(db, "o1", GOOD_MD, "wrong-name.md")
            await db.commit()

            # 原版本必须完好
            still = await skill_svc.get_by_name(db, "o1", "wechat-hot-title")
            assert still.status == "ready"
            assert still.body == good.body
            assert len(await skill_svc.list_active(db, "o1")) == 1
        await engine.dispose()

    _run(go())


def test_broken_import_saved_as_invalid_when_no_previous(tmp_path):
    """没有旧版本时仍然落成 invalid —— 用户要能看到「为什么不行」。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            s, _ = await skill_svc.upsert_skill(db, "o1", "没有 frontmatter", "bad-one.md")
            await db.commit()
            assert s.status == "invalid"
            assert s.error
        await engine.dispose()

    _run(go())


def test_owner_isolation(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            assert len(await skill_svc.list_skills(db, "o1")) == 1
            assert await skill_svc.list_skills(db, "o2") == []
            # 跨 owner 激活不到
            res = await skill_svc.activate(db, "o2", "wechat-hot-title")
            assert res["ok"] is False
        await engine.dispose()

    _run(go())


def test_activate_returns_body_and_counts(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            res = await skill_svc.activate(db, "o1", "wechat-hot-title")
            assert res["ok"] is True
            assert "数字开头" in res["instructions"]
            await db.commit()
            s = await skill_svc.get_by_name(db, "o1", "wechat-hot-title")
            assert s.use_count == 1
        await engine.dispose()

    _run(go())


def test_activate_disabled_refused(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            s, _ = await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            await skill_svc.set_enabled(db, "o1", s.id, False)
            await db.commit()
            res = await skill_svc.activate(db, "o1", "wechat-hot-title")
            assert res["ok"] is False
            assert "停用" in res["error"]
            assert await skill_svc.list_active(db, "o1") == []
        await engine.dispose()

    _run(go())


def test_rename_and_duplicate(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            other = GOOD_MD.replace("wechat-hot-title", "xhs-title").replace(
                "写公众号爆款标题", "写小红书标题"
            )
            await skill_svc.upsert_skill(db, "o1", other, "xhs-title.md")
            await db.commit()

            s = await skill_svc.get_by_name(db, "o1", "wechat-hot-title")
            got, err = await skill_svc.rename_skill(db, "o1", s.id, "xhs-title")
            assert got is None and "已存在同名" in err

            got, err = await skill_svc.rename_skill(db, "o1", s.id, "GZH Title")
            assert got is None and "小写字母" in err

            got, err = await skill_svc.rename_skill(db, "o1", s.id, "gzh-title")
            assert got is not None and err == ""
        await engine.dispose()

    _run(go())


def test_delete(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            s, _ = await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            assert await skill_svc.delete_skill(db, "o2", s.id) is False
            assert await skill_svc.delete_skill(db, "o1", s.id) is True
            await db.commit()
            assert await skill_svc.list_skills(db, "o1") == []
        await engine.dispose()

    _run(go())


# ---------------------------------------------------------------- catalog


def test_catalog_format(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            cat = await skill_svc.build_catalog_for_owner(db, "o1")
            assert skill_svc.SKILL_CATALOG_HEADER in cat
            assert "- wechat-hot-title: 写公众号爆款标题" in cat
        await engine.dispose()

    _run(go())


def test_catalog_empty_when_no_skills(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            assert await skill_svc.build_catalog_for_owner(db, "nobody") == ""
        await engine.dispose()

    _run(go())


# ---------------------------------------------------------------- 工具层


def test_activate_skill_tool_enum_follows_owner(tmp_path):
    """动态工具：enum 跟着 owner 的技能集合走，没有可用技能时整个工具消失。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            # 无技能 → 工具不出现，避免模型反复试空名字
            names = [s["function"]["name"] for s in await registry.schemas_for({"db": db, "owner": "o1"})]
            assert "activate_skill" not in names

            await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()

            schemas = await registry.schemas_for({"db": db, "owner": "o1"})
            act = next(s for s in schemas if s["function"]["name"] == "activate_skill")
            assert act["function"]["parameters"]["properties"]["name"]["enum"] == [
                "wechat-hot-title"
            ]
            # 别的 owner 仍然没有
            names_o2 = [
                s["function"]["name"]
                for s in await registry.schemas_for({"db": db, "owner": "o2"})
            ]
            assert "activate_skill" not in names_o2

            # 真的能执行
            res = await registry.execute(
                "activate_skill", {"name": "wechat-hot-title"}, {"db": db, "owner": "o1"}
            )
            assert res["ok"] is True
            assert "数字开头" in res["instructions"]
        await engine.dispose()

    _run(go())


def test_use_skills_false_hides_tool(tmp_path):
    """本轮关掉技能：即使有可用技能，activate_skill 也不出现在 tools 里。

    只靠「不注入 catalog」是不够的 —— 工具还在的话，模型仍可能凭 description
    去猜一个名字调用。开关必须同时关掉这两半。
    """

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()

            on = await registry.schemas_for({"db": db, "owner": "o1", "use_skills": True})
            assert "activate_skill" in [s["function"]["name"] for s in on]

            off = await registry.schemas_for({"db": db, "owner": "o1", "use_skills": False})
            assert "activate_skill" not in [s["function"]["name"] for s in off]
        await engine.dispose()

    _run(go())


def test_use_skills_none_or_missing_means_on(tmp_path):
    """不带这个字段（老客户端 / ctx 里没有）按开启处理，保持默认体验。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            for ctx in (
                {"db": db, "owner": "o1"},
                {"db": db, "owner": "o1", "use_skills": None},
            ):
                names = [s["function"]["name"] for s in await registry.schemas_for(ctx)]
                assert "activate_skill" in names
        await engine.dispose()

    _run(go())


def test_registry_schemas_without_ctx_has_no_enum():
    """无上下文的展示用定义不能带 enum —— 否则会把别人的技能名泄漏出去。"""
    act = next(s for s in registry.schemas() if s["function"]["name"] == "activate_skill")
    assert "enum" not in act["function"]["parameters"]["properties"]["name"]


# ---------------------------------------------------------------- 路由层（直调，不走 HTTP）
#
# 照 tests/test_knowledge_upload.py 的套路：临时库 + 直调端点函数。
# 这样能覆盖 HTTP 层看不到的两件事——编码容错、以及「坏文件返回 400 而不是
# 覆盖好版本」这条语义。

import io  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from starlette.datastructures import UploadFile  # noqa: E402

from app.routers import skills as skills_router  # noqa: E402


def _uf(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _perm():
    return SimpleNamespace(id="u1")


def test_route_paste_then_toggle_then_delete(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            created = await skills_router.create_skill(
                skills_router.SkillCreate(text=GOOD_MD),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            assert created.name == "wechat-hot-title"
            assert created.status == "ready"

            off = await skills_router.patch_skill(
                created.id,
                skills_router.SkillPatch(enabled=False),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            assert off.enabled is False

            listed = await skills_router.list_skills(db=db, owner="o1")
            assert len(listed) == 1 and listed[0].enabled is False

            assert (await skills_router.delete_skill(
                created.id, db=db, owner="o1", _perm=_perm()
            ))["ok"] is True
            assert await skills_router.list_skills(db=db, owner="o1") == []
        await engine.dispose()

    _run(go())


def test_route_upload_gbk_encoded_file(tmp_path):
    """Windows 导出的 SKILL.md 常是 GBK —— 必须能正确解码，不能乱码进正文。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            raw = GOOD_MD.encode("gbk")
            out = await skills_router.upload_skill(
                file=_uf("wechat-hot-title.md", raw),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            assert out.status == "ready"
            assert "公众号爆款标题" in out.description
            assert "数字开头" in out.body
        await engine.dispose()

    _run(go())


def test_route_upload_bad_file_returns_400_and_keeps_good(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            good = await skills_router.upload_skill(
                file=_uf("wechat-hot-title.md", GOOD_MD.encode("utf-8")),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            assert good.status == "ready"

            with pytest.raises(HTTPException) as e:
                await skills_router.upload_skill(
                    file=_uf("wrong-name.md", GOOD_MD.encode("utf-8")),
                    db=db,
                    owner="o1",
                    _perm=_perm(),
                )
            assert e.value.status_code == 400
            assert "原技能未改动" in e.value.detail

            rows = await skills_router.list_skills(db=db, owner="o1")
            assert len(rows) == 1 and rows[0].status == "ready"
        await engine.dispose()

    _run(go())


def test_route_rejects_non_markdown_upload(tmp_path):
    """zip 已支持（见下方 zip 用例）；这里守住「其它扩展名仍然被拒」。"""
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            with pytest.raises(HTTPException) as e:
                await skills_router.upload_skill(
                    file=_uf("skill.pdf", b"%PDF-1.4"),
                    db=db,
                    owner="o1",
                    _perm=_perm(),
                )
            assert e.value.status_code == 400
            assert ".md" in e.value.detail
        await engine.dispose()

    _run(go())


# ---------------------------------------------------------------- /api/tools 展示
#
# 系统设置页「可用工具」卡片读的是 GET /api/tools。它是**无上下文**端点，
# 曾经因为 DynamicTool 缺 description 属性直接 500，前端 catch 成空数组，
# 整张卡片显示「可用工具（0）」（2026-10-22 实际踩到）。这两项守住回归。


def test_list_tools_endpoint_includes_dynamic_tool():
    async def go():
        from app.routers.tools import list_tools

        rows = await list_tools()
        names = [r.name for r in rows]
        assert "query_queue" in names
        assert "activate_skill" in names
        act = next(r for r in rows if r.name == "activate_skill")
        # 无上下文时不能带 enum（否则会泄漏别人的技能名）
        assert act.description
        assert act.requires_confirm is False
        # 其他工具的确认标记不能被带歪
        assert next(r for r in rows if r.name == "push_wechat_draft").requires_confirm is True

    _run(go())


def test_dynamic_tool_exposes_tool_like_attributes():
    """任何按 Tool 接口读属性的地方都该能读动态工具。"""
    dt = next(t for t in registry.all() if t.name == "activate_skill")
    assert isinstance(dt.description, str) and dt.description
    assert dt.requires_confirm is False


# ---------------------------------------------------------------- zip 上传
#
# zip 是「一个 skill 一个目录」最常见的传输形态（从 Claude 生态下载的技能包
# 几乎都是 zip）。核心语义：SKILL.md 为准，其余文本文件进 files；
# 防炸弹（条目数/单文件/总量）与路径穿越要挡住。

import zipfile  # noqa: E402


def _zip_bytes(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_parse_zip_root_and_subdir():
    skill_md = GOOD_MD  # name: wechat-hot-title
    # 根目录形态
    r1 = skill_svc.parse_zip(_zip_bytes({"SKILL.md": skill_md, "refs/tips.md": "# tips\n多看少写"}))
    assert "wechat-hot-title" in r1["text"]
    assert r1["files"] == [{"path": "refs/tips.md", "content": "# tips\n多看少写"}]
    # 一层子目录形态
    r2 = skill_svc.parse_zip(_zip_bytes({"wechat-hot-title/SKILL.md": skill_md, "scripts/gen.py": "print(1)"}))
    assert r2["files"][0]["path"] == "scripts/gen.py"


def test_parse_zip_errors():
    with pytest.raises(skill_svc.SkillParseError, match="SKILL.md"):
        skill_svc.parse_zip(_zip_bytes({"readme.md": "没有 SKILL.md"}))
    with pytest.raises(skill_svc.SkillParseError, match="多个"):
        skill_svc.parse_zip(_zip_bytes({"a/SKILL.md": GOOD_MD, "b/SKILL.md": GOOD_MD}))
    with pytest.raises(skill_svc.SkillParseError, match="不是有效的 zip"):
        skill_svc.parse_zip(b"not a zip")


def test_parse_zip_skips_binary_and_traversal():
    png = bytes(range(256))
    r = skill_svc.parse_zip(_zip_bytes({
        "SKILL.md": GOOD_MD,
        "assets/logo.png": png,            # 二进制：跳过
        "../evil.md": "x",                 # 穿越：跳过
        "/abs.md": "y",                    # 绝对路径：跳过
    }))
    assert r["files"] == []


def test_parse_zip_limits():
    many = {f"f{i}.md": "x" for i in range(40)}
    many["SKILL.md"] = GOOD_MD
    with pytest.raises(skill_svc.SkillParseError, match="最多"):
        skill_svc.parse_zip(_zip_bytes(many))
    big = {"SKILL.md": GOOD_MD, "huge.md": "x" * (skill_svc.settings.SKILL_ZIP_FILE_MAX + 1)}
    with pytest.raises(skill_svc.SkillParseError, match="单文件上限"):
        skill_svc.parse_zip(_zip_bytes(big))


def test_route_upload_zip_saves_files():
    async def go():
        engine, Maker = await _mk_session(tmp_path_zip())
        async with Maker() as db:
            out = await skills_router.upload_skill(
                file=_uf("pkg.zip", _zip_bytes({
                    "wechat-hot-title/SKILL.md": GOOD_MD,
                    "refs/style.md": "# 风格\n数字开头",
                })),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            assert out.status == "ready"
            assert out.files == ["refs/style.md"]
            # 坏 zip → 400 且不落库
            try:
                await skills_router.upload_skill(
                    file=_uf("bad.zip", b"junk"), db=db, owner="o1", _perm=_perm()
                )
                raise AssertionError("应当 400")
            except HTTPException as e:
                assert e.status_code == 400 and "zip" in e.detail
            assert len(await skills_router.list_skills(db=db, owner="o1")) == 1
        await engine.dispose()

    _run(go())


def tmp_path_zip():
    import tempfile
    return pathlib.Path(tempfile.mkdtemp())


import pathlib  # noqa: E402


# ---------------------------------------------------------------- 编辑内容


def test_update_skill_text_roundtrip(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            s, _ = await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            sid = s.id

            newer = GOOD_MD.replace(
                "description: 写公众号爆款标题。当用户要起标题、改标题、要更吸引人时使用。",
                "description: 写知乎爆款标题。当用户要起标题、改标题时使用。",
            ).replace("# 怎么做", "# 新做法")
            updated, err = await skill_svc.update_skill_text(db, "o1", sid, newer)
            assert err == ""
            assert updated.description.startswith("写知乎爆款标题")
            assert "# 新做法" in updated.body
            assert updated.status == "ready" and updated.error == ""
            assert updated.name == "wechat-hot-title"  # name 不动
            await db.commit()

            # 坏内容原样不动
            bad, err2 = await skill_svc.update_skill_text(db, "o1", sid, "# 没有 frontmatter")
            assert bad is None and "frontmatter" in err2
            after = await skill_svc.get_skill(db, "o1", sid)
            assert "# 新做法" in after.body

            # 想在这里改名 → 拒绝
            renamed = GOOD_MD.replace("name: wechat-hot-title", "name: zhihu-hot-title")
            _, err3 = await skill_svc.update_skill_text(db, "o1", sid, renamed)
            assert "name" in err3

            # 不存在的 id
            _, err4 = await skill_svc.update_skill_text(db, "o1", "nope", GOOD_MD)
            assert err4 == "技能不存在"
        await engine.dispose()

    _run(go())


def test_update_skill_text_fixes_invalid(tmp_path):
    """invalid 的技能（如之前导入失败的）可以通过编辑救活。"""
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            s, _ = await skill_svc.upsert_skill(db, "o1", GOOD_MD, "SKILL.md")
            await db.commit()
            sid = s.id
            # 手动搞成 invalid（模拟导入失败留下的行）
            s.status = "invalid"
            s.error = "frontmatter 缺少 description"
            s.description = ""
            s.body = ""
            await db.commit()

            updated, err = await skill_svc.update_skill_text(db, "o1", sid, GOOD_MD)
            assert err == ""
            assert updated.status == "ready"
            assert updated.error == ""
            assert updated.description != ""
            await db.commit()
        await engine.dispose()

    _run(go())


def test_route_patch_text(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            created = await skills_router.create_skill(
                skills_router.SkillCreate(text=GOOD_MD), db=db, owner="o1", _perm=_perm()
            )
            await db.commit()
            sid = created.id

            newer = GOOD_MD.replace("# 怎么做", "# 改过的做法")
            out = await skills_router.patch_skill(
                sid,
                skills_router.SkillPatch(text=newer),
                db=db,
                owner="o1",
                _perm=_perm(),
            )
            await db.commit()
            assert out.status == "ready"
            assert "# 改过的做法" in out.body

            # 坏内容 → 400，且原内容不动
            try:
                await skills_router.patch_skill(
                    sid,
                    skills_router.SkillPatch(text="空的"),
                    db=db,
                    owner="o1",
                    _perm=_perm(),
                )
                raise AssertionError("应当 400")
            except HTTPException as e:
                assert e.status_code == 400
            after = await skills_router.patch_skill(
                sid, skills_router.SkillPatch(enabled=True), db=db, owner="o1", _perm=_perm()
            )
            assert "# 改过的做法" in after.body
        await engine.dispose()

    _run(go())
