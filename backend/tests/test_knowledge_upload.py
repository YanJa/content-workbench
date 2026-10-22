"""知识库「文档上传」入库的单测。

覆盖：
  - doc_extract 对 txt/md/json（纯 Python）与不支持类型的解析；
  - POST /knowledge/upload 端点：落盘、抽文、source=upload、sha256 去重；
  - 边界：不支持扩展名 / 空文件 / 过大文件的拒绝。

沿用 test_knowledge 的套路：临时库 + 假 embedding + 后台任务 SessionLocal 指向临时库；
UPLOAD_DIR 也改到 tmp，避免测试往开发目录写文件。
"""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.datastructures import UploadFile

import app.models  # noqa: F401
from app.core.config import settings
from app.db.session import Base
from app.models.knowledge import KnowledgeItem
from app.services import embedding, knowledge as kb
from app.services import doc_extract as de
from app.routers import knowledge as kb_router


def _fake_embed_async(texts):
    # 假向量：让后台向量化任务能跑完（否则 item 卡在 processing）
    return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "kb.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    Maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(embedding, "embed_texts", _fake_embed_async)
    monkeypatch.setattr(embedding, "configured", lambda: True)
    monkeypatch.setattr(kb, "SessionLocal", Maker)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    kb.invalidate_cache()
    return SimpleNamespace(Maker=Maker, tmp=tmp_path)


def _uf(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


async def _call_upload(env, raw: bytes, filename: str, title: str = ""):
    async with env.Maker() as db:
        return await kb_router.upload(
            file=_uf(filename, raw),
            title=title,
            db=db,
            owner="default",
            _perm=SimpleNamespace(id="u1"),
        )


# ---------------------------------------------------------------- doc_extract

def test_extract_text_plain(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("你好\n第二行", encoding="utf-8")
    text, err = de.extract_text(str(p), "a.txt")
    assert err is None and "你好" in text


def test_extract_text_markdown(tmp_path):
    p = tmp_path / "b.md"
    p.write_text("# 标题\n\n正文", encoding="utf-8")
    text, err = de.extract_text(str(p), "b.md")
    assert err is None and "# 标题" in text


def test_extract_text_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"k": "v", "n": [1, 2]}), encoding="utf-8")
    text, err = de.extract_text(str(p), "c.json")
    assert err is None and '"k"' in text


def test_extract_text_unsupported(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"123")
    text, err = de.extract_text(str(p), "x.png")
    assert text == "" and err and "不支持" in err


# ---------------------------------------------------------------- 上传端点

def test_upload_txt_creates_upload_item(env):
    raw = "文档第一段\n文档第二段".encode("utf-8")

    async def _run():
        res = await _call_upload(env, raw, "doc.txt", title="我的文档")
        assert res["ok"]
        assert res["already_exists"] is False
        item = res["item"]
        assert item["source"] == "upload"
        async with env.Maker() as db:
            row = (
                await db.execute(select(KnowledgeItem).where(KnowledgeItem.id == item["id"]))
            ).scalar_one()
            assert row.title == "我的文档"
            assert "文档第一段" in row.raw_text
            assert row.file_path and (env.tmp / "uploads" / row.file_path).exists()
            assert (row.meta or {}).get("file_hash")

    asyncio.run(_run())


def test_upload_pdf(env):
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab 未安装")
    p = env.tmp / "f.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(80, 750, "Hello PDF content")
    c.save()
    raw = p.read_bytes()

    async def _run():
        res = await _call_upload(env, raw, "f.pdf")
        assert res["ok"]
        assert res["item"]["source"] == "upload"

    asyncio.run(_run())


def test_upload_dedup_by_hash(env):
    raw = "重复上传的同一份文档".encode("utf-8")

    async def _run():
        r1 = await _call_upload(env, raw, "dup.txt")
        r2 = await _call_upload(env, raw, "dup.txt")
        assert r1["ok"] and r2["ok"]
        assert r2["already_exists"] is True
        assert r1["item"]["id"] == r2["item"]["id"]

    asyncio.run(_run())


def test_upload_unsupported_extension_rejected(env):
    from fastapi import HTTPException

    async def _run():
        with pytest.raises(HTTPException) as ei:
            await _call_upload(env, b"x", "x.png")
        assert ei.value.status_code == 400

    asyncio.run(_run())


def test_upload_empty_file_rejected(env):
    from fastapi import HTTPException

    async def _run():
        with pytest.raises(HTTPException) as ei:
            await _call_upload(env, b"", "empty.txt")
        assert ei.value.status_code == 400

    asyncio.run(_run())


def test_upload_too_large_rejected(env, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(de, "KB_UPLOAD_MAX_BYTES", 10)

    async def _run():
        with pytest.raises(HTTPException) as ei:
            await _call_upload(env, b"x" * 100, "big.txt")
        assert ei.value.status_code == 400

    asyncio.run(_run())
