"""公众号排版主题注册表（源自 gzh-design 技能）。

【为什么把这些 .md 拷进后端】
gzh-design 是宿主 Agent 的技能，后端进程读不到它的目录，也无法 import。
但它最核心的资产是**组件库文本**（各主题的 HTML 组件 + 装配规则）——
LLM 完全可以「读文本 → 按规则装配」，所以只要把文本搬进后端当提示词资产，
就能让后端自己完成排版，不必依赖宿主 Agent 逐个手动处理。

数据流：`theme-index.md` 提供主题清单；排版时按需加载**选中的那一套**
主题组件库 + `common-components.md`，拼进 system prompt。

文件来源（只读搬运，勿手改）：
    /root/.codebuddy/skills/skill_2097954066825015296/gzh-design/references/
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ASSETS_DIR = Path(__file__).parent

# 主题标识 -> 展示信息（"组件库文件" 去掉 theme-/ 前缀与 .md 后缀即为标识）
# 与 theme-index.md 的表格保持一致；新增主题先登记 theme-index.md 再补这里。
THEMES: dict[str, dict[str, str]] = {
    "zhici-qinglv": {
        "name": "只此青绿",
        "color": "#007C7E",
        "scenes": "观点复盘、文化叙事、品牌故事、东方美学",
        "desc": "青绿杂志风，优雅克制",
    },
    "moyu-green": {
        "name": "摸鱼绿",
        "color": "#059669",
        "scenes": "教程、测评、清单、工具盘点",
        "desc": "卡片丰富、信息密度高",
    },
    "red-white": {
        "name": "红白色系",
        "color": "#DC2626",
        "scenes": "深度分析、观点、力量感话题",
        "desc": "经典编辑风，编号章节+引言卡",
    },
    "graphite-minimal": {
        "name": "石墨极简风",
        "color": "#52525B",
        "scenes": "设计、科技评论、专业观点、高端品牌",
        "desc": "极简克制、留白理性、全灰阶",
    },
    "zen-whitespace": {
        "name": "留白禅意风",
        "color": "#4A5D52",
        "scenes": "禅意冥想、极简生活、深度随笔、艺术留白",
        "desc": "呼吸感最强",
    },
    "moyu-ticket": {
        "name": "摸鱼票据风",
        "color": "#059669",
        "scenes": "测评、工具对比、创意评测",
        "desc": "票据/门票视觉隐喻，星级评分",
    },
    "olive-journal": {
        "name": "橄榄手记",
        "color": "#1e1f23",
        "scenes": "内刊手记、深度评测、案例复盘、系统性说明文档",
        "desc": "编辑部内刊质感，信息密度偏高",
    },
    "mashang-liaoliao": {
        "name": "码上聊聊",
        "color": "#FF3B30",
        "scenes": "工具推荐、AI 资讯、教程实操、品牌号日常更新",
        "desc": "暖橙品牌风，气泡分隔+引导卡自成体系",
    },
}

DEFAULT_THEME = "moyu-green"


def list_themes() -> list[dict[str, str]]:
    """给前端的主题选项列表。"""
    return [{"id": tid, **info} for tid, info in THEMES.items()]


@lru_cache(maxsize=16)
def load_theme_lib(theme_id: str) -> str:
    """读取某主题的组件库全文（含骨架/配方表/映射规则）。带缓存。

    读入后做一次**幂等兜底注入**（见 `_ensure_qrcode_slot`）：即使本文件被
    gzh-design 技能重新搬运覆盖（.md 顶部注明「只读搬运，勿手改」），
    「文末关注卡带 {{二维码}} 占位」这个能力也不会丢。
    """
    path = ASSETS_DIR / f"theme-{theme_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"主题组件库不存在: {theme_id}")
    return _ensure_qrcode_slot(path.read_text(encoding="utf-8"))


# 文末关注卡的固定文案，用于定位「扫码/长按关注」那一行
_FOLLOW_HINT = "扫码 / 长按关注"
_QR_SLOT_HTML = (
    '  <!--QRCODE_SLOT-->\n'
    '  <p style="margin:16px 0 0;text-align:center;">'
    '<span leaf="">{{二维码}}</span></p>\n'
    "  <!--/QRCODE_SLOT-->"
)


def _ensure_qrcode_slot(text: str) -> str:
    """幂等兜底：若主题含关注卡文案但缺 `{{二维码}}` 占位，则自动注入。

    为什么需要它：`.md` 是「只读搬运」资产，可能被技能重新覆盖；把能力
    放在**运行时**而不是只依赖 `.md` 内容，才能长期稳定。
    - 已含 `{{二维码}}` → 直接返回（幂等）。
    - 不含关注卡文案（其它主题）→ 直接返回（天然 no-op，零破坏）。
    - 定位方式：找 `扫码 / 长按关注` 那一行，在其所在的
      `<section ...>…</section>` 闭合后插入占位块。
    """
    if "{{二维码}}" in text or _FOLLOW_HINT not in text:
        return text
    idx = text.find(_FOLLOW_HINT)
    # 从提示文案往后找最近的一处 `</section>` 收尾（关注卡内那层分隔行）
    close = text.find("</section>", idx)
    if close == -1:
        return text
    insert_at = close + len("</section>")
    return text[:insert_at] + "\n" + _QR_SLOT_HTML + text[insert_at:]


@lru_cache(maxsize=1)
def load_common_lib() -> str:
    """通用增量库：代码块 / 图片 / 小标签标题，所有主题共用。"""
    return (ASSETS_DIR / "common-components.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_skill_rules() -> str:
    """排版工作流与红线（gzh-design SKILL.md 全文）。"""
    return (ASSETS_DIR / "gzh-skill.md").read_text(encoding="utf-8")
