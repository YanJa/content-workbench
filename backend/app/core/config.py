"""应用配置。

所有配置从环境变量读取，敏感信息（API Key）绝不写入代码。
本地开发把 .env.example 复制成 .env 填写即可。
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 基础 ----------
    APP_NAME: str = "内容运营工作台"
    APP_ENV: Literal["dev", "prod"] = "dev"
    DEBUG: bool = True
    API_PREFIX: str = "/api"

    # 前端地址（CORS 白名单，逗号分隔）
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 是否额外放行本机的任意端口。
    # 场景：前端通过静态文件通道打开时，页面 origin 是 localhost:随机端口，
    # 写不进上面的白名单。只允许 loopback 地址，不放行公网域名。
    CORS_ALLOW_LOCALHOST: bool = True

    # ---------- 数据库 ----------
    # 一期用 SQLite；后续换 PostgreSQL 只需改这个字符串
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/workbench.db"

    # ---------- Redis ----------
    # 留空则自动降级为内存缓存（开发期无需装 Redis）
    REDIS_URL: str = ""
    REDIS_TTL: int = 3600

    # ---------- 大模型 ----------
    # DeepSeek 当前主力模型：deepseek-flash (V4.1-Flash, 1M 上下文, 原生视觉)
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL_FLASH: str = "deepseek-flash"
    DEEPSEEK_MODEL_PRO: str = "deepseek-v4-pro"

    # 兜底模型（DeepSeek 服务波动时切换，兼容 OpenAI 接口即可）
    FALLBACK_API_KEY: str = ""
    FALLBACK_BASE_URL: str = ""
    FALLBACK_MODEL: str = ""

    # 生成参数
    LLM_TEMPERATURE: float = 0.8
    LLM_MAX_TOKENS: int = 4096
    LLM_TIMEOUT: int = 120

    # 峰谷感知：高峰时段价格翻倍，批量任务应避开
    # 高峰 = 工作日 UTC 01:00-04:00 与 06:00-10:00（北京时间 09-12 点、14-18 点）
    ENABLE_PEAK_GUARD: bool = False

    # ---------- 认证（用户模块） ----------
    # JWT 签发与直链 HMAC 签名共用密钥。留空 = 首次启动自动生成并持久化到
    # data/secret.key（见 core/security.py）；生产环境建议显式配置。
    JWT_SECRET: str = ""
    # Token 有效期（小时）。直链 HMAC 签名不过期，不受此项影响。
    JWT_TTL_HOURS: int = 168
    # 注册通道：invite=需邀请码 / open=开放 / closed=关闭。
    # 无论哪种模式，第一个注册的用户永远放行（认领存量数据刚需）。
    AUTH_REGISTER_MODE: Literal["invite", "open", "closed"] = "invite"
    # 邀请码（AUTH_REGISTER_MODE=invite 时生效）；留空 = 仅允许首用户注册。
    INVITE_CODE: str = ""

    # ---------- 会话 ----------
    # 保留最近 N 轮原文，更早的压缩为摘要
    CONTEXT_WINDOW_TURNS: int = 10
    CONTEXT_SUMMARY_THRESHOLD: int = 16  # 超过这个轮数触发摘要

    # ---------- 工具调用 ----------
    ENABLE_TOOLS: bool = True
    # 危险操作（如推送发布）需人工确认，不由模型直接触发
    TOOL_REQUIRE_CONFIRM: str = "push_wechat_draft,render_video"

    # ---------- 微信 ----------
    WECHAT_APPID: str = ""
    WECHAT_SECRET: str = ""

    # 微信接口出网代理（解决「沙箱出网 IP 是动态池、加不完白名单」的问题）。
    # 形如 socks5://127.0.0.1:10800 或 http://127.0.0.1:8888。
    # 留空 = 直连（保持原行为）。只作用于调用 api.weixin.qq.com 的请求，
    # 不影响 LLM 等其他出网流量（见 services/wechat.py 的 _proxy 逻辑）。
    # 配套部署脚本：deploy/proxy-guard.sh（建隧道 + 断线自愈）。
    WECHAT_PROXY: str = ""
    # 走代理时是否校验「实际出口 IP」与预期一致（防隧道指向错机器）。
    # 形如 49.233.223.252；留空则跳过校验。仅启动自检与 /wechat 系统状态用。
    WECHAT_PROXY_EXPECT_IP: str = ""

    # ---------- 文件 ----------
    UPLOAD_DIR: str = "./data/uploads"
    OUTPUT_DIR: str = "./data/outputs"
    MAX_UPLOAD_MB: int = 50

    # ---------- 配图生图 ----------
    # 本地生图脚本的绝对路径（用户提供）。留空则前端降级为「手动上传图片」。
    # 契约见 app/services/imagegen.py 顶部注释。
    IMAGE_SCRIPT: str = ""
    # 单次生图超时（秒）。生图模型较慢，给足 3 分钟。
    IMAGE_SCRIPT_TIMEOUT: int = 180
    # 默认出图尺寸（脚本支持 1024x1024 / 1024x1536 等）
    IMAGE_SIZE: str = "1024x1024"

    # ---------- 封面图（模板化，与上面的 AI 生图是两条独立通道） ----------
    # 封面脚本：HTML 模板 + 无头浏览器截图，出「文字确定、品牌统一」的头图。
    # 默认指向项目自带的 backend/scripts/make-cover.py，留空则关闭封面通道。
    # 契约见 app/services/covers.py 顶部注释。
    COVER_SCRIPT: str = "scripts/make-cover.py"
    # 单次渲染超时（秒）。纯本地截图，很快。
    COVER_TIMEOUT: int = 60
    # 封面默认标签（左上角），如「码上聊聊 · 效率指南」。留空则不带标签。
    COVER_TAG: str = ""
    # 封面默认风格：gradient(渐变) / dark(深色) / light(浅色)
    COVER_STYLE: str = "gradient"
    # 封面默认尺寸：wide(900x383 公众号头条) / square(800x800) / og(1200x630)
    COVER_SIZE: str = "wide"

    # ---------- 声音复刻 ----------
    # 录音与音色库目录（每个音色一个子目录，含参考音频与 voice.json）
    VOICE_DIR: str = "./data/voices"
    # 复刻后端：edge=不复刻（用官方音色）/ cloud=云端 API / xtts=本地 XTTS-v2
    VC_PROVIDER: Literal["edge", "cloud", "xtts"] = "edge"
    # 云端复刻（阿里云 CosyVoice / MiniMax / 火山方舟等 OpenAI 风格端点）
    VC_CLOUD_BASE_URL: str = ""
    VC_CLOUD_API_KEY: str = ""
    VC_CLOUD_MODEL: str = "cosyvoice-v1"
    # 录音大小上限（MB）。样本只需要 10~30 秒，给到 20MB 足够宽容
    MAX_VOICE_UPLOAD_MB: int = 20

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def cors_origin_regex(self) -> str | None:
        """本机任意端口的匹配正则；关闭 CORS_ALLOW_LOCALHOST 时返回 None。"""
        if not self.CORS_ALLOW_LOCALHOST:
            return None
        return r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

    @property
    def confirm_tools(self) -> set[str]:
        return {t.strip() for t in self.TOOL_REQUIRE_CONFIRM.split(",") if t.strip()}

    @property
    def use_redis(self) -> bool:
        return bool(self.REDIS_URL)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
