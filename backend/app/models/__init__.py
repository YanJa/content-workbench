"""模型聚合导出。

导入顺序很重要：`Base.metadata.create_all` 只认识已经被 import 过的模型类。
这里统一导入一遍，避免"表建不出来"的坑。
"""
from app.models.content import Content, QueueItem  # noqa: F401
from app.models.conversation import Conversation, Message  # noqa: F401
from app.models.invite import InviteCode  # noqa: F401
from app.models.knowledge import KnowledgeChunk, KnowledgeItem  # noqa: F401
from app.models.model_pool import (  # noqa: F401
    ModelBinding,
    ModelCatalog,
    ModelProvider,
    ModelProviderKey,
    ModelQuotaUsage,
)
from app.models.renderjob import RenderJob  # noqa: F401
from app.models.settings import AppSetting, WeChatAccount  # noqa: F401
from app.models.skill import Skill  # noqa: F401
from app.models.permission import RolePermission  # noqa: F401
from app.models.quota import QuotaUsage  # noqa: F401
from app.models.revenue import WechatAdDaily  # noqa: F401
from app.models.slide import PptSlideClaim  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.usage import LlmUsage  # noqa: F401
from app.models.zhihu import HotSnapshot, HotTopic, ZhihuApiUsage  # noqa: F401

__all__ = [
    "Content",
    "QueueItem",
    "Conversation",
    "Message",
    "LlmUsage",
    "RenderJob",
    "WeChatAccount",
    "AppSetting",
    "Skill",
    "User",
    "PptSlideClaim",
    "QuotaUsage",
    "RolePermission",
    "InviteCode",
    "KnowledgeItem",
    "KnowledgeChunk",
    "ModelProvider",
    "ModelProviderKey",
    "ModelCatalog",
    "ModelBinding",
    "ModelQuotaUsage",
    "HotSnapshot",
    "HotTopic",
    "ZhihuApiUsage",
    "WechatAdDaily",
]
