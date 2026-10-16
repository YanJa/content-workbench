"""PPT 幻灯片目录的归属声明（claim）。

背景（越权洞）：视频生产的幻灯片图按 `slides/<folder>/page_N.png` 存放，
folder = md5(pptx 文件内容)[:16]。两个人上传同一份 PPT，folder 相同、
目录撞车 —— 谁先生成谁的文件就在里面，后一个人（或任何拿到路径的人）
不鉴权就能读。原端点 serve_slide_image / preview_card 零校验。

修法：folder 首次被使用时登记归属（懒认领），之后只有归属者
（或持有该路径 HMAC 签名直链的人）能访问；归属不符一律 404
（不泄露「这个目录存在」）。

表极简：slide_key 主键 + owner + 创建时间，没有更新需求。
"""
from __future__ import annotations

import time

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class PptSlideClaim(Base):
    __tablename__ = "ppt_slide_claims"

    # slides/<folder>/ 的 folder 段，md5 截断 16 字符
    slide_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[int] = mapped_column(default=_now)
