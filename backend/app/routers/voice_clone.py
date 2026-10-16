"""声音复刻路由（/api/video/voices/*）。

流程：上传录音 → 异步复刻 → 列表里拿到 `clone_<id>` → 渲染时选它当音色。
接口刻意保持"一次上传就能用"，不暴露"训练轮数/步数"这类参数 —— 用户
不需要理解零样本克隆，只要知道"录够 10 秒就能用我的声音念稿"。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.security import link_url
from app.core.tenant import current_owner_id, link_auth
from app.services import voices as V

router = APIRouter(prefix="/video/voices", tags=["声音复刻"])

ALLOWED_EXT = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm", ".amr")


@router.get("/provider")
async def provider():
    """当前复刻后端状态：前端据此决定「录音克隆」按钮是否可用、提示啥文案。"""
    return V.provider_status()


@router.get("/clones")
async def list_voices(owner: str = Depends(current_owner_id)):
    """已复刻音色列表（含试听地址），可直接拼进音色选择器。

    路径刻意用 `/voices/clones` 而不是 `/voices`：后者是 tools.py 里
    「edge-tts 官方音色清单」的地址，同路径会因注册顺序被抢先匹配，
    导致复刻音色列表拿不到（踩过一次）。
    """
    items = V.list_profiles(owner)
    for it in items:
        # sample_url 是给 <audio src> 用的直链，浏览器不会带请求头，
        # 用 HMAC 签名（见 core/security.link_url）。
        it["sample_url"] = link_url(f"{settings.API_PREFIX}/video/voices/{it['id']}/sample", owner)
        it["value"] = V.CLONE_PREFIX + it["id"]
    return {"items": items, "provider": V.provider_status()}


@router.post("/clone")
async def clone(
    file: UploadFile = File(...),
    name: str = Form(""),
    owner: str = Depends(current_owner_id),
):
    """上传一段录音，后台开始复刻。立刻返回 profile_id，进度靠 GET /clone/{id}。

    为什么异步：本地 XTTS 首次要下模型，几十秒到几分钟；同步等会把
    浏览器的请求超时打爆。异步 + 轮询是唯一稳的做法。
    """
    status = V.provider_status()
    if not status["available"]:
        raise HTTPException(400, status["note"])
    if status["provider"] == "edge":
        raise HTTPException(
            400,
            "当前未开启声音复刻（VC_PROVIDER=edge）。本地零成本可用："
            "后端执行 pip install coqui-tts 并把 VC_PROVIDER 改成 xtts",
        )

    fname = (file.filename or "audio").lower()
    if not fname.endswith(ALLOWED_EXT):
        raise HTTPException(400, f"不支持的音频格式：{fname.split('.')[-1][:10]}")
    content = await file.read()
    if not content:
        raise HTTPException(400, "音频是空文件")
    if len(content) > settings.MAX_VOICE_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"录音超过 {settings.MAX_VOICE_UPLOAD_MB}MB，请裁剪后再传")

    profile = V.create_profile(name, content, file.filename or "audio", owner)
    V._clone_jobs[V._job_key(owner, profile["id"])] = V._new_job(profile["id"])
    asyncio.create_task(V.clone_voice(profile["id"], owner))
    return {
        "ok": True,
        "id": profile["id"],
        "name": profile["name"],
        "message": "录音已上传，正在复刻音色，请稍候",
    }


@router.get("/clone/{profile_id}")
async def clone_status(profile_id: str, owner: str = Depends(current_owner_id)):
    """复刻进度。完成时带上质量评估（录音太短/太轻都会被明确告知）。"""
    job = V.get_clone_job(profile_id, owner)
    if job:
        return job
    # 服务重启过（任务表在内存）或已完成：回落到持久化结果
    vm = V.get_profile(profile_id, owner)
    if vm:
        return {
            "profile_id": profile_id,
            "status": "done",
            "progress": 100,
            "stage": "完成",
            "quality": vm.get("quality"),
        }
    raise HTTPException(404, "复刻任务不存在（可能服务重启过，请重新上传录音）")


@router.get("/{profile_id}/sample")
async def sample(profile_id: str, owner: str = Depends(link_auth)):
    """试听：优先返回参考音频（就是用户自己的声音，最直观）。

    <audio src> 直链，HMAC 签名鉴权（见 core/security.link_url）。
    """
    p = V.profile_audio_path(profile_id, owner)
    if p is None:
        raise HTTPException(404, "试听文件不存在")
    media = "audio/wav" if p.suffix == ".wav" else "audio/mpeg"
    return FileResponse(str(p), media_type=media)


@router.get("/{profile_id}/meta")
async def meta(profile_id: str, owner: str = Depends(current_owner_id)):
    vm = V.get_profile(profile_id, owner)
    if not vm:
        raise HTTPException(404, "音色不存在")
    return {k: v for k, v in vm.items() if k != "ref_path"}


@router.delete("/{profile_id}")
async def delete(profile_id: str, owner: str = Depends(current_owner_id)):
    if not V.delete_profile(profile_id, owner):
        raise HTTPException(404, "音色不存在")
    return {"ok": True}


@router.put("/{profile_id}")
async def rename(profile_id: str, payload: dict, owner: str = Depends(current_owner_id)):
    """给音色改个名字（"我的音色 1015 16:20" 这种默认名没人记得住）。"""
    vm = V.get_profile(profile_id, owner)
    if not vm:
        raise HTTPException(404, "音色不存在")
    name = str(payload.get("name") or "").strip()[:40]
    if not name:
        raise HTTPException(400, "名称不能为空")
    d = V.profile_dir(profile_id, owner)
    meta_file = d / "meta.json"
    meta = json.loads(meta_file.read_text("utf-8")) if meta_file.is_file() else {}
    meta["name"] = name
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    vm["name"] = name
    (d / "voice.json").write_text(json.dumps(vm, ensure_ascii=False), "utf-8")
    return {k: v for k, v in vm.items() if k != "ref_path"}
