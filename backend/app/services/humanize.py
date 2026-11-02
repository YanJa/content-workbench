"""人工痕迹体检与「加痕迹」改写。

两件事，两条完全不同的技术路线，别混为一谈：

**体检（scan）是零 LLM 的**：纯规则 + 统计，毫秒级、零成本、可重复、每条命中都能
指出原文位置。这不是为了省钱（虽然确实省），是因为**可解释性本身就是产品价值** ——
市面付费检测器给你一个黑箱概率（「AI 率 73%」），你既不知道哪里不对、也不知道怎么改，
而且那个概率在人类化文本上只有 11-28% 的检出率，低分也不代表是人写的。我们要给的
是「哪句话、什么毛病、怎么改」。

**改写（humanize）必须走 LLM**：本地规则改写（同义词替换、句长随机扰动）只会产生
新的机器味——把「显著提升」换成「明显提高」，读者一眼还是能看出是模板。真正的
「人味」是主观看法、具体细节、不完美的节奏，这些只能靠模型理解语义后注入。

**一处必须守住的边界**：改写最大的坑是悄悄把关键数字改掉。所以 `verify_facts()`
做本地硬校验（数字/百分比/日期/链接/专名逐一比对），丢了明确报警，绝不静默。

阈值来源：句长 CV、短句占比等指标参考 HC3-Chinese 人类-AI 对照样本的 Cohen's d
（句长 CV：人类 0.52 / AI 0.32；短句占比：人类 25% / AI 2.6%）。阈值不是拍脑袋定的，
但也**不是判定标准**——见 `scan()` 返回的 `note` 字段，这句话必须原样带到 UI 上。
"""
from __future__ import annotations

import re
import statistics
import time
import uuid
from typing import Any

from app.core.config import settings

# ------------------------------------------------------------------ 规则表
# 中文的 AI 味在**句式与连接词**，不在词频。英文那套（delve / tapestry / em dash）
# 搬到中文不成立，所以规则表全部是中文语料重写过的。

# 机械连接词分两档，是为了减少误伤：
#   STRONG 是纯 AI 腔，人类正常写作基本不会用（碰一个就扣分）；
#   WEAK 是「人类也常用、但 AI 用得过于密集」的词（最后、与此同时…），
#   单独出现不该定罪——所以按次数累计、且单位扣分低。
# 不分开的话，「我最后想说一句」这种正常人话也会被判成 AI，那是冤枉。
_MECH_CONNECTORS_STRONG = [
    "综上所述", "总而言之", "总的来看", "总的来说",
    "值得注意的是", "需要指出的是", "不难看出", "由此可见", "众所周知", "众所周知的是",
    "在此基础之上", "在此基础上", "进一步来看",
    "从某种意义上", "不可否认", "毋庸置疑", "客观来说", "实事求是地说",
]
_MECH_CONNECTORS_WEAK = [
    "首先", "其次", "再次", "最后", "换句话说", "也就是说", "与此同时",
]

# 空洞宏大词 / 互联网黑话：抽象名词堆叠，AI 最爱用来凑「深度感」。
_BUZZWORDS = [
    "赋能", "抓手", "闭环", "生态", "深耕", "赛道", "颗粒度", "打法", "链路", "对齐",
    "全方位", "多维度", "深层次", "深度融合", "着力", "扎实推进", "切实", "有效提升",
    "显著", "极大地", "广泛", "充分", "积极", "稳步", "持续发力", "再上新台阶",
]

# 模板句式：排比 + 对仗，写得越工整越像机器。
_TEMPLATE_PATTERNS = [
    (r"不仅[^\n。！？]{0,20}[，、]?更[^\n。！？]{0,20}", "「不仅…更…」对仗"),
    (r"不是[^\n。！？]{0,25}[，、]?而是[^\n。！？]{0,25}", "「不是…而是…」对仗"),
    (r"随着[^\n。！？]{0,25}的(?:不断)?(?:发展|普及|深入|提升|变化)", "「随着…的发展」开场"),
    (r"在[^\n。！？]{0,25}(?:背景|趋势|浪潮|时代)下", "「在…背景下」开场"),
    (r"对于[^\n。！？]{0,20}而言", "「对于…而言」"),
    (r"我们可以(?:看到|发现|得出)", "「我们可以看到」"),
    (r"这(?:不仅|既|既不是)[^\n。！？]{0,15}", "「这不仅…」总结句"),
]

# 聊天残留：助手语气漏进正文，一眼暴露。
_CHAT_RESIDUE = [
    "希望对你有帮助", "希望对你有所帮助", "希望以上内容", "以上就是", "以上就是我的分享",
    "如果你还有疑问", "如有疑问", "欢迎在评论区", "欢迎留言", "欢迎交流",
    "让我知道", "我很乐意", "作为一个AI", "作为AI", "作为一个人工智能",
]

# 求互动套话：知乎尤其忌讳。
_INTERACTION_BAIT = [
    "你觉得呢", "你怎么看", "欢迎点赞", "点赞关注", "关注收藏", "一键三连",
    "评论区告诉我", "在评论区聊聊", "记得点赞", "别忘了关注",
]

# 三段式：三个并列短语用顿号/逗号串起来（「创新、洞察与赋能」）。
_RULE_OF_THREE_RE = re.compile(
    r"[一-鿿]{2,8}[、，][一-鿿]{2,8}[、，][一-鿿]{2,8}(?:[和与及])?"
)

# 句子切分：中英混排都要能切。
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;…\n]+")
# 只留正文用的「字数」：中文字符 + 数字 + 英文字母（标点不算字数）
_WORD_RE = re.compile(r"[一-鿿]|[A-Za-z]+|\d+(?:\.\d+)?")


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def _split_paragraphs(text: str) -> list[str]:
    raw = re.split(r"\n\s*\n|\n", text)
    return [p.strip() for p in raw if p.strip()]


def _cv(values: list[float]) -> float:
    """变异系数（标准差/均值）。句长 CV 是中文最强的单指标（Cohen's d=1.22）。"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    return statistics.pstdev(values) / mean


def _clips(text: str, keyword: str, width: int = 14) -> list[str]:
    """命中词的上下文片段，给用户看「在哪」。"""
    out = []
    for m in re.finditer(re.escape(keyword), text):
        a = max(0, m.start() - width // 2)
        b = min(len(text), m.end() + width)
        frag = text[a:b].replace("\n", " ")
        out.append(("…" if a > 0 else "") + frag + ("…" if b < len(text) else ""))
        if len(out) >= 3:
            break
    return out


def _plain_text(markdown: str) -> str:
    """体检与改写都只认纯文本：HTML 标签、Markdown 标记都会污染统计。

    复用知乎渲染那套机械剥离思路（不引新依赖），够用。
    """
    t = re.sub(r"<[^>]+>", "", markdown or "")
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[-*+]\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*>\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"[*_`~]", "", t)
    return t.strip()


# ------------------------------------------------------------------ 体检
def _pick(registry: dict, platform: str, sub_type: str = "", default: Any = None) -> Any:
    """按 (platform, sub_type) 查表：复合 key 优先，回落平台级。

    公众号贴图和小红书同档（heavy、口语、emoji 是形式本身），但它是 gzh 的
    子形态——发布、统计、微信审核口径都归 gzh，只在「怎么写」上另立一档。
    """
    if sub_type:
        v = registry.get(f"{platform}_{sub_type}")
        if v is not None:
            return v
    return registry.get(platform, default)


def scan(text: str, platform: str = "gzh", sub_type: str = "") -> dict[str, Any]:
    """人工痕迹体检。返回 0-100 的人工痕迹指数，**越高越像人写的**。

    为什么用正向指标：市面工具全是「AI 率，越低越好」，用户很容易看反；而我们要做的
    动作是「补痕迹」，正向指数与动作目标同向。

    计分：100 - 规则层扣分（上限 60） - 统计层扣分（上限 40）。
    样本太短时统计层不可靠（检测器在短文上误判率本来就高），按规则层占比外推，
    并在 note 里说明——**不能给用户一个看起来精确、实际站不住的分数**。
    """
    body = _plain_text(text)
    if not body:
        return {
            "score": 0, "level": "unknown", "ok": False,
            "error": "没有可检查的正文", "hits": [], "stats": {}, "tips": [], "note": "",
        }

    hits: list[dict[str, Any]] = []
    rule_penalty = 0.0

    def add(key: str, label: str, severity: str, penalty: float,
            samples: list[str], how: str) -> None:
        nonlocal rule_penalty
        if not samples:
            return
        rule_penalty += penalty
        hits.append({
            "key": key, "label": label, "severity": severity,
            "count": len(samples), "samples": samples[:3], "how": how,
        })

    # 1) 机械连接词（强腔 3 分/处，弱腔 1 分/处）
    conn_hits: list[str] = []
    conn_pen = 0.0
    for w in _MECH_CONNECTORS_STRONG:
        clips = _clips(body, w)
        if clips:
            conn_pen += 3.0 * body.count(w)
            conn_hits.extend(clips)
    for w in _MECH_CONNECTORS_WEAK:
        clips = _clips(body, w)
        if clips:
            conn_pen += 1.0 * body.count(w)
            conn_hits.extend(clips)
    if conn_hits:
        pen = min(18.0, conn_pen)
        add("connector", f"机械连接词（{len(conn_hits)} 类）", "high" if pen >= 9 else "mid",
            pen, conn_hits[:3], "删掉或换成口语过渡（「话说回来」「这里有个坑」），两段之间本来就不用连接词")

    # 2) 宏大词 / 黑话
    buzz_hits: list[str] = []
    buzz_count = 0
    for w in _BUZZWORDS:
        clips = _clips(body, w)
        if clips:
            buzz_count += body.count(w)
            buzz_hits.extend(clips)
    if buzz_hits:
        pen = min(14.0, 2.5 * buzz_count)
        add("buzzword", f"空洞宏大词（{buzz_count} 处）", "mid", pen, buzz_hits[:3],
            "换成具体事实：把「显著提升」写成「从 3 小时压到 40 分钟」")

    # 3) 模板句式
    tpl_hits: list[str] = []
    for pat, name in _TEMPLATE_PATTERNS:
        for m in re.finditer(pat, body):
            a = max(0, m.start() - 4)
            tpl_hits.append(("…" if a > 0 else "") + body[a:m.end() + 8])
    if tpl_hits:
        add("template", f"模板句式（{len(tpl_hits)} 处）", "mid", min(12.0, 3.0 * len(tpl_hits)),
            tpl_hits[:3], "拆开写成两句：一句说事实，一句说你的判断")

    # 4) 三段式排比
    three = [m.group(0) for m in _RULE_OF_THREE_RE.finditer(body)]
    if three:
        add("rule_of_three", f"三段式排比（{len(three)} 处）", "low", min(8.0, 2.0 * len(three)),
            three[:3], "人写东西不会每条都凑三个——删一个或拆成两句")

    # 5) 聊天残留（最致命，直接暴露）
    residue: list[str] = []
    for w in _CHAT_RESIDUE:
        residue.extend(_clips(body, w))
    if residue:
        add("chat_residue", "助手语气残留", "high", 20.0, residue[:3],
            "整句删掉。这类话没有读者想看，且一出现就等于自报家门")

    # 6) 求互动套话
    bait: list[str] = []
    for w in _INTERACTION_BAIT:
        bait.extend(_clips(body, w))
    if bait:
        pen = 12.0 if platform == "zhihu" else 6.0
        add("interaction_bait", "求互动套话", "high" if platform == "zhihu" else "low", pen,
            bait[:3], "删掉。要讨论就把问题写具体，别喊口号")

    # 7) 平台专属
    if platform == "xiaohongshu":
        first_person = len(re.findall(r"(?:我|我们)(?:[的]|觉得|觉得吧|亲测|试过|踩过)", body))
        emoji = len(re.findall(r"[\U0001f300-\U0001faff\u2600-\u27bf]", body))
        if first_person == 0:
            add("xhs_no_self", "小红书没有第一人称", "high", 12.0, [body[:40]],
                "小红书的人味来自「我试过」——补一句亲身经历，哪怕是「我踩过的坑」")
        if emoji == 0:
            add("xhs_no_emoji", "小红书零 emoji", "mid", 6.0, [body[:40]],
                "分点开头加 1-2 个 emoji 是平台惯例，不是 AI 痕迹")
    elif platform == "zhihu":
        if len(re.findall(r"[\U0001f300-\U0001faff\u2600-\u27bf]", body)) >= 5:
            add("zhihu_emoji", "知乎正文堆 emoji", "mid", 8.0, [body[:40]],
                "知乎正文堆 emoji 显得不专业（合规自检也查这一条）")
    elif platform == "gzh":
        digits = len(re.findall(r"\d+(?:\.\d+)?(?:%|％|万|亿|年|月|日|分钟|小时|块|元)?", body))
        if digits == 0:
            add("gzh_no_detail", "通篇没有一个具体数字", "high", 12.0, [body[:40]],
                "公众号的「人味」来自细节：加一个时间、金额、地名或人名")

    rule_penalty = min(60.0, rule_penalty)

    # ---------------- 统计层
    sentences = _split_sentences(body)
    paras = _split_paragraphs(body)
    lens = [len(s) for s in sentences] or [0]
    stat: dict[str, Any] = {
        "chars": len(_WORD_RE.findall(body)),
        "sentences": len(sentences),
        "paragraphs": len(paras),
    }
    stat_penalty = 0.0
    stat_hits: list[dict[str, Any]] = []

    # 样本太短：统计特征全部站不住（检测器在 <200 字上误判率高出 40%），不参与计分
    short_sample = stat["sentences"] < 6 or stat["chars"] < 150

    if not short_sample:
        cv = _cv([float(x) for x in lens])
        stat["sent_len_cv"] = round(cv, 3)
        if cv < 0.35:
            stat_penalty += 15.0
            stat_hits.append({
                "key": "len_cv", "label": f"句子长度太齐整（CV {cv:.2f}，人类约 0.52）",
                "severity": "high", "count": len(sentences), "samples": [],
                "how": "把相邻的短句合并成一个长句，再把某个长句砍成半句——制造长短交错",
            })
        elif cv < 0.45:
            stat_penalty += 8.0
            stat_hits.append({
                "key": "len_cv", "label": f"句子节奏偏均匀（CV {cv:.2f}）",
                "severity": "mid", "count": len(sentences), "samples": [],
                "how": "插一两个半截话（「其实我一开始也不信」），节奏就活了",
            })

        short_ratio = sum(1 for x in lens if x < 10) / len(lens)
        stat["short_sent_ratio"] = round(short_ratio, 3)
        if short_ratio < 0.08:
            stat_penalty += 12.0
            stat_hits.append({
                "key": "short_sent", "label": f"几乎没有短句（{short_ratio:.0%}，人类约 25%）",
                "severity": "high", "count": len(sentences), "samples": [],
                "how": "人类说话会蹦短句：把一两个长判断改成一句「别买。」「没用。」",
            })
        elif short_ratio < 0.15:
            stat_penalty += 6.0
            stat_hits.append({
                "key": "short_sent", "label": f"短句偏少（{short_ratio:.0%}）",
                "severity": "mid", "count": len(sentences), "samples": [],
                "how": "再砍两三个短句出来",
            })

        # 段落长度均匀度是**我自己加的弱信号**（HC3 那批指标里没有它的 Cohen's d），
        # 所以：样本少于 5 段不评估（4 段的 CV 完全是噪声），权重也只有 4 分。
        if len(paras) >= 5:
            para_cv = _cv([float(len(re.sub(r"\s", "", p))) for p in paras])
            stat["para_len_cv"] = round(para_cv, 3)
            if para_cv < 0.22:
                stat_penalty += 4.0
                stat_hits.append({
                    "key": "para_uniform", "label": "每段长度一样长",
                    "severity": "low", "count": len(paras), "samples": [],
                    "how": "有一段写成两行就够，另一段展开写——别端着",
                })

        # 句首重复：连续段落用同一个词开头，是 AI 分段最典型的手艺痕迹
        heads = [p[:2] for p in paras if len(p) >= 2]
        if len(heads) >= 4:
            dup = 1 - len(set(heads)) / len(heads)
            if dup > 0.3:
                stat_penalty += 6.0
                stat_hits.append({
                    "key": "head_repeat", "label": f"段落开头雷同（{dup:.0%}）",
                    "severity": "mid", "count": len(heads), "samples": heads[:3],
                    "how": "换掉其中几段的第一个词，别都用「另外」「同时」「还有」",
                })

        # 标点多样性：通篇只有逗号句号 = 没有语气
        marks = set(re.findall(r"[，。！？：；、…—\"“”‘’（）]", body))
        if len(marks) <= 2:
            stat_penalty += 5.0
            stat_hits.append({
                "key": "punct", "label": "标点只有逗号句号", "severity": "low",
                "count": len(marks), "samples": [],
                "how": "加一个问号或破折号——人类写着写着会插一句反问",
            })

        stat_penalty = min(40.0, stat_penalty)

    hits.extend(stat_hits)

    # ---------------- 合成分数
    if short_sample:
        # 统计层缺席时按规则层占比外推到 100 分制，避免短文虚高
        score = 100.0 - rule_penalty / 0.6
        note = (
            f"正文只有 {stat['chars']} 字 / {stat['sentences']} 句，统计特征（句长节奏等）"
            "样本不足、未参与计分，当前分数由用词与句式特征外推得出，仅供参考。"
        )
    else:
        score = 100.0 - rule_penalty - stat_penalty
        note = (
            "这是「特征扫描」，不是「是否 AI 生成」的判定。"
            "检测器在真实场景的误判率有 15-30%（非母语写作更高），"
            "而对改过的 AI 文本检出率只有 11-28%——分数用来找问题，不用来下结论。"
        )

    score = max(0.0, min(100.0, round(score)))
    if score >= 80:
        level, level_label = "natural", "读着像人写的"
    elif score >= 60:
        level, level_label = "slight", "有点机器味"
    elif score >= 40:
        level, level_label = "obvious", "一眼能看出是机器写的"
    else:
        level, level_label = "heavy", "通篇机器味"

    hits.sort(key=lambda h: {"high": 0, "mid": 1, "low": 2}.get(h["severity"], 3))

    return {
        "ok": True,
        "score": int(score),
        "level": level,
        "level_label": level_label,
        "platform": platform,
        "stats": stat,
        "hits": hits,
        "tips": _tips(platform, sub_type),
        "note": note,
        "short_sample": short_sample,
    }


# ------------------------------------------------------------------ 改写
# 强度档：同一段话，在知乎「有主观看法」是加分，在学术论文里就是不合格。
# 所以默认档按平台走，但允许用户覆盖 —— 平台是猜测，用户才知道这篇要发给谁看。
LEVELS = ("light", "medium", "heavy")
LEVEL_LABELS = {"light": "轻度", "medium": "中度", "heavy": "重度"}
LEVEL_DESC = {
    "light": "只删机器味的词与套话，不动结构、不加主观判断",
    "medium": "删套话 + 补具体细节 + 加入个人判断 + 打散节奏",
    "heavy": "全篇口语化重写，允许半截话、自我修正、第一人称",
}
PLATFORM_DEFAULT_LEVEL = {
    "gzh": "medium",
    "gzh_tietu": "heavy",  # 贴图和小红书同档：短文案要的就是口语和第一人称
    "xiaohongshu": "heavy",
    "zhihu": "light",
    "douyin": "medium",
}

_HUMANIZE_SYSTEM = """你是一位中文编辑，专长是把「读起来像机器写的」稿子改成「读起来像人写的」。

## 你的三条铁律（违反任何一条，这次改写就是失败的）

1. **一个数字都不能改**：百分比、金额、日期、时长、数量、版本号、链接、人名、地名、
   机构名、专有名词，必须与原文完全一致。改写完会有程序逐项比对，改一个就算事故。
2. **不许新增事实**：原文没有的数据、案例、引言、时间点，一个字都不许编。
   需要细节而原文没有时，改句式，不要造内容。
3. **不许做同义词替换式改写**：把「显著提升」换成「明显提高」是另一种机器味，
   不是人味。人味来自下面这些东西。

## 什么才是「人味」（按效果排序）

- **不完美**：长短句交错、口语过渡（"话说回来""这里有个坑"）、自我修正
  （"其实我一开始也不信"）、偶尔一句半截话。AI 的破绽是**太顺**。
- **具体**：把抽象判断写成可感知的描述（"效率提升" → "原来要改三遍，现在一遍过"）。
  注意：只能用原文已有的信息，不许编数字。
- **有判断**：敢下结论，也敢说"这部分我没把握"。中立罗列是最典型的机器腔。

## 本次改写强度：{level_name}（{level_desc}）

## 目标平台：{platform_name}

## 输出要求

只输出改写后的正文，保留段落换行。**不要**写任何说明、**不要**前后对比、
**不要**用 Markdown 代码块围栏、不要加标题。"""

_PLATFORM_TONE = {
    "gzh": "公众号：允许故事感与情绪起伏，但不要变成小红书式的碎碎念",
    "gzh_tietu": "公众号贴图：口语、第一人称、emoji 是这个形式本身，不算 AI 痕迹",
    "xiaohongshu": "小红书：口语、第一人称、可以碎碎念，emoji 保留原文风格",
    "zhihu": "知乎：克制、有判断、有边界。禁止求互动套话，禁止堆 emoji，禁止八股连接词",
    "douyin": "抖音：短句、强节奏，一句话一个信息点",
}


def build_humanize_messages(text: str, platform: str = "gzh", level: str = "medium",
                            hits: list[dict[str, Any]] | None = None,
                            sub_type: str = "") -> list[dict[str, str]]:
    """构造改写请求。命中项会作为「重点改这几处」喂进去 —— 体检不是白做的，
    它指出的具体问题比通用指令有效得多。
    """
    if level not in LEVELS:
        level = _pick(PLATFORM_DEFAULT_LEVEL, platform, sub_type, "medium")
    system = _HUMANIZE_SYSTEM.format(
        level_name=LEVEL_LABELS[level],
        level_desc=LEVEL_DESC[level],
        platform_name=_pick(_PLATFORM_TONE, platform, sub_type, ""),
    )
    focus = ""
    if hits:
        lines = []
        for h in hits[:6]:
            samples = " / ".join(h.get("samples") or [])[:120]
            lines.append(f"- 【{h['label']}】{h['how']}" + (f"（例：{samples}）" if samples else ""))
        focus = "体检发现的问题，优先改这些：\n" + "\n".join(lines) + "\n\n"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{focus}原文：\n\n{text}\n\n改后正文："},
    ]


# 事实校验：改写前后逐一比对。数字丢了就是事故，必须报警，绝不静默。
_FACT_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|千|百|年|月|日|号|分钟|小时|天|周|次|个|人|元|块|倍|度|分|秒)?")
_FACT_URL_RE = re.compile(r"https?://[^\s)）\]】，。]+")
_FACT_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")


def verify_facts(before: str, after: str) -> list[str]:
    """比对改写前后是否丢了数字/链接/专名。返回人类可读的告警列表（空 = 没丢）。"""
    warnings: list[str] = []

    def diff(name: str, pat: re.Pattern[str], unit: str) -> None:
        from collections import Counter
        b = Counter(m.group(0).strip() for m in pat.finditer(before or ""))
        a = Counter(m.group(0).strip() for m in pat.finditer(after or ""))
        lost = b - a
        if lost:
            items = ", ".join(f"{k}（×{v}）" for k, v in list(lost.items())[:6])
            warnings.append(f"改写后少了 {len(lost)} 个{unit}：{items}")

    diff("数字", _FACT_NUM_RE, "数字/数量")
    diff("链接", _FACT_URL_RE, "链接")
    diff("专名", _FACT_LATIN_RE, "英文词/专名")
    return warnings


def _tips(platform: str, sub_type: str = "") -> list[str]:
    """平台专属的「人味」标准——同一段话，在小红书是人味十足，在知乎就是太水。"""
    return _pick(
        {
            "gzh": [
                "公众号的人味来自「故事与细节」：时间、地点、金额、人名，有一样就不像机器写的",
                "允许情绪起伏：先讲一件具体的事，再讲你的判断",
            ],
            "gzh_tietu": [
                "贴图的人味就是「像跟朋友说话」：短句、口语、emoji 是形式本身",
                "只讲一个点，别把贴图写成缩水文章——那是最典型的机器味",
            ],
            "xiaohongshu": [
                "小红书要「口语和亲身经历」：「我踩过的坑」比「注意事项」有说服力",
                "emoji 与碎碎念是平台惯例，不算 AI 痕迹",
            ],
            "zhihu": [
                "知乎的人味来自「有判断、有边界」：敢下结论，也敢说「这部分我没把握」",
                "求互动套话与 emoji 堆砌在知乎是扣分项，不是人味",
            ],
            "douyin": [
                "抖音要「短句强节奏」：一句话一个信息点，长定语从句直接砍",
            ],
        },
        platform,
        sub_type,
        [],
    )


# ------------------------------------------------------------------ 异步改写任务
# 与排版同一套外壳（秒回 task_id + 后台协程 + 前端轮询）：改写一篇长文要几十秒，
# 而前端 http 超时只有 60s，同步接口会挂。
_HUMANIZE_JOBS: dict[str, dict[str, Any]] = {}
_HUMANIZE_LOCK: Any = None  # 惰性创建：asyncio.Lock() 必须在有事件循环时建

# 任务句柄强引用。
# `asyncio.create_task()` 的返回值**必须有人拿着**：事件循环只对 task 持弱引用，
# 一旦这个 task 被 GC 掉，协程就在下一个 await 点悄悄消失 —— 任务表里永远停在
# 「running / 55%」，既不报错也不结束。这是「卡在 55%」最难查的一类原因。
_HUMANIZE_TASKS: dict[str, Any] = {}


def _lock() -> Any:
    global _HUMANIZE_LOCK
    if _HUMANIZE_LOCK is None:
        import asyncio
        _HUMANIZE_LOCK = asyncio.Lock()
    return _HUMANIZE_LOCK


def _prune_humanize_jobs(keep: int = 50, max_age: float = 1800.0) -> None:
    if len(_HUMANIZE_JOBS) <= keep:
        return
    now = time.time()
    stale = sorted(
        (k for k, v in _HUMANIZE_JOBS.items()
         if v.get("finished_at") and now - v["finished_at"] > max_age),
        key=lambda k: _HUMANIZE_JOBS[k]["finished_at"],
    )
    for k in stale[: max(0, len(_HUMANIZE_JOBS) - keep)]:
        _HUMANIZE_JOBS.pop(k, None)


def _set_hjob(task_id: str, **kw: Any) -> None:
    job = _HUMANIZE_JOBS.get(task_id)
    if job is not None:
        job.update(kw)


def clean_llm_output(raw: str) -> str:
    """剥掉模型爱加的代码块围栏与说明性开头。"""
    t = (raw or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n?", "", t)
    t = re.sub(r"\n?```$", "", t)
    # 「改写如下：」「以下是改写后的正文：」这类开场白一并去掉
    t = re.sub(r"^(?:改写(?:后|如下|后的正文)?|以下是[^：\n]{0,12})[：:]\s*\n?", "", t)
    return t.strip()


async def humanize_text(
    text: str,
    platform: str = "gzh",
    level: str = "medium",
    owner: str = "default",
    db: Any = None,
    sub_type: str = "",
) -> dict[str, Any]:
    """改写一段正文，返回 {text, before_score, after_score, warnings, level}。

    前后各扫一次分，是为了**给出可验证的效果**：改完分数没动，说明这次改写没用，
    得如实告诉用户，而不是一律弹「改写成功」。
    """
    from app.services.llm import get_llm
    from app.services.usage import make_sink

    before = scan(text, platform, sub_type)
    if level not in LEVELS:
        level = _pick(PLATFORM_DEFAULT_LEVEL, platform, sub_type, "medium")

    # 改写是「理解语义后注入人味」，轻量档做不好，走与排版同档的 reasoning
    sink = make_sink(db, owner=owner or "default", scene="humanize") if db is not None else None
    llm = get_llm()
    result = await llm.chat(
        build_humanize_messages(text, platform, level, before.get("hits"), sub_type),
        task="reasoning",
        feature="humanize",
        owner=owner or "default",
        usage_sink=sink,
    )
    new_text = clean_llm_output(result.content or "")
    if not new_text:
        return {
            "ok": False, "error": "模型没返回改写结果",
            "text": "", "before_score": before["score"], "after_score": before["score"],
            "warnings": [], "level": level,
        }

    warnings = verify_facts(text, new_text)
    after = scan(new_text, platform, sub_type)
    return {
        "ok": True,
        "text": new_text,
        "before_score": before["score"],
        "after_score": after["score"],
        "warnings": warnings,
        "level": level,
        "platform": platform,
        "sub_type": sub_type,
    }


async def submit_humanize_job(
    content_id: str,
    platform: str = "gzh",
    level: str = "",
    sub_type: str = "",
    owner: str = "default",
) -> dict[str, Any]:
    """登记改写任务并立即返回 task_id。"""
    from app.prompts.platforms import normalize_sub_type

    _prune_humanize_jobs()
    task_id = uuid.uuid4().hex
    st = normalize_sub_type(platform, sub_type)
    _HUMANIZE_JOBS[task_id] = {
        "task_id": task_id,
        "content_id": content_id,
        "owner": owner,
        "platform": platform,
        "sub_type": st,
        "level": level or _pick(PLATFORM_DEFAULT_LEVEL, platform, st, "medium"),
        "status": "queued",
        "progress": 0,
        "stage": "排队中…",
        # indeterminate=True 表示「当前阶段没有可信的百分比」：进度条该停在原地，
        # 由前端用已用时长告诉用户它还活着。见 _run_humanize_job 的注释。
        "indeterminate": False,
        "result": None,
        "error": "",
        "started_at": time.time(),
        "finished_at": None,
    }
    import asyncio

    task = asyncio.create_task(_run_humanize_job(task_id, content_id, platform, level, st))
    _HUMANIZE_TASKS[task_id] = task
    task.add_done_callback(lambda _t, k=task_id: _HUMANIZE_TASKS.pop(k, None))
    return {"ok": True, "task_id": task_id, "status": "queued", "poll": task_id}


async def _run_humanize_job(task_id: str, content_id: str, platform: str, level: str,
                            sub_type: str = "") -> None:
    """后台 worker：自建 Session（请求已返回，不能复用请求期的 db）。"""
    import asyncio

    from app.db.session import SessionLocal
    from app.models.content import Content
    from app.prompts.platforms import normalize_sub_type

    _set_hjob(task_id, status="running", progress=10, stage="正在读原文…")
    try:
        # 全局串行锁：两个改写同时跑，各自 2 分钟以上，还都算不上错峰。
        # 但排队必须说出来 —— 否则第二个人看到的是「正在读原文…」一动不动，
        # 以为是卡死，其实连模型都还没调。
        if _lock().locked():
            _set_hjob(task_id, progress=5, stage="排队中：前面还有一个改写任务在跑…")
        async with _lock():
            async with SessionLocal() as db:
                c = await db.get(Content, content_id)
                if c is None:
                    _set_hjob(task_id, status="error", error="内容不存在", finished_at=time.time())
                    return
                src = c.body or ""
                if not src.strip():
                    _set_hjob(task_id, status="error", error="源文是空的，没有可改写的内容",
                              finished_at=time.time())
                    return
                # 前端没传子形态时，从内容本身兜底（队列里存的就是权威值）
                st = sub_type or normalize_sub_type(platform, c.sub_type)
                _set_hjob(task_id, progress=30, stage="AI 正在注入人的痕迹…")
                # 从这里开始进入模型调用：没有可信百分比，只能给时长。
                # 秒数由 get_humanize_job 按 started_at 现算，不在 stage 里拼 ——
                # 前端自己也有一份本地计时器，塞进文案会重复成「已用 5 秒 已用 7 秒」。
                _set_hjob(task_id, progress=55, indeterminate=True,
                          stage="AI 正在重写（保留所有数字与事实）…")
                try:
                    res = await asyncio.wait_for(
                        humanize_text(
                            src, platform,
                            level or _pick(PLATFORM_DEFAULT_LEVEL, platform, st, "medium"),
                            owner=c.owner or "default", db=db, sub_type=st,
                        ),
                        timeout=settings.HUMANIZE_TIMEOUT,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    # 没有这道闸，请求会一直挂着：openai 的 timeout 管的是单次读写，
                    # 推理模型慢慢吐 token 时永远不会触发，只能等到进程重启。
                    _set_hjob(
                        task_id, status="error", indeterminate=False, finished_at=time.time(),
                        error=(
                            f"模型 {settings.HUMANIZE_TIMEOUT} 秒内没有返回，已中止本次改写。"
                            "长稿建议拆成两段分别改，或把强度调到「轻度」。"
                        ),
                    )
                    return
                _set_hjob(task_id, progress=90, indeterminate=False,
                          stage="正在校验事实是否被改坏…")
                if not res.get("ok"):
                    _set_hjob(task_id, status="error", error=res.get("error", "改写失败"),
                              finished_at=time.time())
                    return
                _set_hjob(task_id, status="done", progress=100, stage="改写完成",
                          result=res, finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        _set_hjob(task_id, status="error", error=f"改写失败：{exc}", finished_at=time.time())


def get_humanize_job(task_id: str, owner: str = "default") -> dict[str, Any] | None:
    job = _HUMANIZE_JOBS.get(task_id)
    if job is None:
        return None
    # 归属校验：任务归属发起者，防登录用户轮询他人 task_id 探进度（task_id
    # 本身是随机 UUID 不易枚举，但写上这层不影响正常流程、彻底闭合越权读数）。
    if job.get("owner") not in (None, owner):
        return None
    # elapsed 由服务端算：前端计时器只负责让秒数一直跳，真值以这里为准
    base = job.get("finished_at") or time.time()
    job["elapsed"] = max(0, int(base - (job.get("started_at") or base)))
    return job


async def apply_humanized(db: Any, content_id: str, text: str, owner: str = "default") -> dict[str, Any]:
    """把改写结果写回**源文**（content.body）。

    关键点：改的是源文，排版产物（body_html / layouts）是从旧源文生成的，
    留着就是「面板显示的和源文不是一回事」。所以一并清掉、状态回退到未排版，
    由用户重新点排版——**不自动重排**，那是另一笔模型开销，得用户点头。
    """
    from app.models.content import Content

    c = await db.get(Content, content_id)
    if c is None:
        return {"ok": False, "error": "内容不存在"}
    if (c.owner or "default") != owner:
        return {"ok": False, "error": "只能改自己的内容"}
    c.body = text
    c.body_html = ""
    c.layouts = {}
    c.layout_status = "draft"
    c.processing = ""
    await db.commit()
    after = scan(text, c.platform or "gzh")
    return {
        "ok": True,
        "content_id": content_id,
        "score": after["score"],
        "message": "源文已更新，排版产物已失效，需要重新排版一次",
    }
