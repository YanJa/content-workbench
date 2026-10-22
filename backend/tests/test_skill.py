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


def test_registry_schemas_without_ctx_has_no_enum():
    """无上下文的展示用定义不能带 enum —— 否则会把别人的技能名泄漏出去。"""
    act = next(s for s in registry.schemas() if s["function"]["name"] == "activate_skill")
    assert "enum" not in act["function"]["parameters"]["properties"]["name"]
