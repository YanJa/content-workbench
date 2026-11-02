"""素材存储抽象层。

为什么要有这一层：现有图片路径是硬编码拼出来的（`Path(settings.UPLOAD_DIR) / "gen" / cid`），
散在 layout.py / auth.py / imagegen.py 好几处。再往上加「云存储」就会变成到处 if。
新模块从第一天就走接口，将来切 S3 只换实现，调用方一行不用改。

**URL 也归后端管**，不归调用方：
    LocalStorage.url() → 本地 HMAC 签名直链（复用 core.security.link_url）
    TOSStorage.url()  → TOS 公网永久直链（public-read 对象，浏览器直连，不签名）
两者有效期语义不同，让调用方自己拼路径就失去了抽象的意义。

当前两种后端：
    local（默认）：文件落本地盘，对外 HMAC 签名直链。
    s3（火山 TOS）：文件落 TOS，浏览器直连 TOS 公网 URL，把图片流量从 ECS 带宽剥离。
切换只改 .env 的 STORAGE_BACKEND 一句，调用方（assets / 排版配图）完全不用动。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.logging import logger
from app.core.security import link_url


class StorageBackend(Protocol):
    """存储后端契约。key 形如 `assets/<owner>/<yyyymm>/<id>.jpg`（不含 bucket、不含域名）。

    排版配图走 `gen/<cid>/<fname>` 前缀；二者都是「相对 UPLOAD_DIR 的相对路径」，
    本地后端据此落盘，云端后端据此拼对象 key。
    """

    async def put(self, key: str, data: bytes, content_type: str = "") -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    def url(self, key: str, owner: str) -> str: ...


class LocalStorage:
    """本地磁盘后端。文件落在 UPLOAD_DIR 下，对外用签名直链。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _resolve(self, key: str) -> Path:
        """把 key 解析成绝对路径，并挡掉路径穿越。

        素材 key 一定以 `assets/` 或 `gen/` 开头（见 services/assets.py 的 key 构造
        与 storage.gen_key）；这两个前缀既是分类也是安全边界：有了它，
        `../../data/workbench.db` 这种 key 连第一关都过不去。
        """
        clean = key.strip().lstrip("/")
        if not (clean.startswith("assets/") or clean.startswith("gen/")):
            raise ValueError(f"非法素材 key: {key!r}")
        if ".." in clean.split("/"):
            raise ValueError(f"非法素材 key（含路径穿越）: {key!r}")
        return self.root / clean

    async def put(self, key: str, data: bytes, content_type: str = "") -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 磁盘 IO 丢线程池：20MB 视频同步写会把事件循环卡住几百毫秒，
        # 期间所有 SSE 流都停住。图片小，但这个类不该假定素材都小。
        await asyncio.to_thread(path.write_bytes, data)

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        try:
            await asyncio.to_thread(path.unlink, True)  # missing_ok=True
        except OSError as exc:  # noqa: BLE001
            # 删文件失败不该让「删除素材」这个动作整体失败：
            # 记录里那一行删掉才是用户要的结果，磁盘残留交给配额巡检清理。
            logger.warning("删除素材文件失败 key=%s: %s", key, exc)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._resolve(key).exists)

    def url(self, key: str, owner: str) -> str:
        # 直链签名绑 path（不含 query），所以 key 里带斜杠也没问题
        return link_url(f"{settings.API_PREFIX}/assets/file/{key}", owner)


class TOSStorage:
    """火山引擎 TOS（S3 兼容）后端。

    设计要点：
    - 上传/读回走**内网** S3 端点（同地域 ECS↔TOS 免费、更快），浏览器加载走**公网**
      桶域名（cwb.tos-cn-beijing.volces.com），流量不经过 ECS 1M 带宽。
    - 对象设为 public-read，url() 返回永久公网直链，浏览器/微信直接拉，无需预签名。
      （<img> 显示不校 CORS，省掉跨域配置；要读字节时后端经内网 get_object。）
    - boto3 是同步库，所有 IO 包进 asyncio.to_thread，不阻塞事件循环。
    """

    def __init__(self) -> None:
        self.bucket = settings.TOS_BUCKET
        self.endpoint = settings.tos_endpoint
        self.region = settings.TOS_REGION
        self.public_base = settings.tos_public_base
        self.acl_public = settings.TOS_ACL_PUBLIC
        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=settings.TOS_ACCESS_KEY,
            aws_secret_access_key=settings.TOS_SECRET_KEY,
            region_name=self.region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                # TOS 支持 virtual-hosted（{bucket}.{endpoint}）；显式声明避免
                # boto3 在自定义 endpoint 下回落到 path 风格出错。
                s3={"addressing_style": "virtual"},
            ),
        )
        logger.info(
            "素材存储后端：tos（bucket=%s, endpoint=%s, public=%s）",
            self.bucket, self.endpoint, self.public_base,
        )

    async def put(self, key: str, data: bytes, content_type: str = "") -> None:
        extra: dict = {}
        if content_type:
            extra["ContentType"] = content_type
        if self.acl_public:
            extra["ACL"] = "public-read"

        def _do() -> None:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=data, **extra,
            )

        await asyncio.to_thread(_do)

    async def get(self, key: str) -> bytes:
        def _do() -> bytes:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()

        return await asyncio.to_thread(_do)

    async def delete(self, key: str) -> None:
        def _do() -> None:
            self.client.delete_object(Bucket=self.bucket, Key=key)

        try:
            await asyncio.to_thread(_do)
        except ClientError as exc:  # noqa: BLE001
            logger.warning("删除 TOS 对象失败 key=%s: %s", key, exc)

    async def exists(self, key: str) -> bool:
        def _do() -> bool:
            try:
                self.client.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError:
                return False

        return await asyncio.to_thread(_do)

    def url(self, key: str, owner: str = "") -> str:
        # 永久公网直链，无需 owner / 签名
        return f"{self.public_base}/{key}"


# ---------------------------------------------------------------- 排版配图 key 辅助
def gen_key(content_id: str, filename: str) -> str:
    """排版配图的对象 key：`gen/<cid>/<filename>`，与本地盘落点同构。"""
    return f"gen/{content_id}/{filename}"


async def put_gen(content_id: str, filename: str, data: bytes) -> None:
    """把一张「排版配图」镜像到云存储。

    本地后端已由调用方写盘（serve_image / 微信转存都读本地），这里只负责在云端
    后端时把字节再传一份到 TOS。本地后端直接跳过，避免重复写盘。
    """
    if settings.STORAGE_BACKEND == "local":
        return
    await get_storage().put(gen_key(content_id, filename), data)


_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """取当前存储后端（进程内单例）。

    读配置而不是硬编码：`STORAGE_BACKEND=s3` 即切 TOS，一句话的事。
    """
    global _backend
    if _backend is None:
        if settings.STORAGE_BACKEND == "s3":
            if not (settings.TOS_ACCESS_KEY and settings.TOS_SECRET_KEY):
                logger.warning(
                    "STORAGE_BACKEND=s3 但 TOS 凭证缺失，回落本地后端（功能降级）"
                )
                _backend = LocalStorage(Path(settings.UPLOAD_DIR))
            else:
                _backend = TOSStorage()
        else:
            _backend = LocalStorage(Path(settings.UPLOAD_DIR))
            logger.info("素材存储后端：local（root=%s）", settings.UPLOAD_DIR)
    return _backend
