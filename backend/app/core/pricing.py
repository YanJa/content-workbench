"""AI 用量计价引擎。

价格单位：**元 / 百万 tokens**。DeepSeek 单价取自官方定价（2026-10 校准）。

--------------------------------------------------------------------------
设计目标：换模型、加模型、调价都不用改代码
--------------------------------------------------------------------------
单价表是**数据**不是逻辑，所以支持三层覆盖（后者覆盖前者）：

  1. 内置默认值（本文件 PRICING，DeepSeek 官方价）
  2. 外部 JSON 文件（config/pricing.json，或环境变量 PRICING_FILE 指定路径）
  3. 环境变量 PRICING_JSON（临时调试 / 容器注入用）

新增一个模型 = 往 JSON 里加一条，不用动 Python。运行时也能
`register_model()` 或调 `POST /api/usage/pricing/reload` 热加载。

--------------------------------------------------------------------------
一条单价支持的字段（除 in/out 外全部可选）
--------------------------------------------------------------------------
    "deepseek-flash": {
      "label": "DeepSeek Flash",        # 展示名，前端直接显示
      "vendor": "deepseek",             # 厂商，用于分组
      "in": 1.0,                        # 输入（未命中缓存）
      "out": 4.0,                       # 输出
      "in_cached": 0.02,                # 输入（缓存命中）→ 不填则按 in 计
      "cache_write": 0.0,               # 缓存写入 → 目前只记录不计价
      "context_window": 1000000,        # 上下文窗口，仅展示
      "reasoning": true,                # 是否推理模型（思维链计入输出）
      "peak_multiplier": 2.0,           # 高峰倍数 → 1.0 表示该模型无峰谷
      "tiers": [                        # 长上下文档位价：输入超过 above 后
        {"above": 128000, "in": 2.0, "out": 8.0}   # 整档按这个价（不是分段累加）
      ]
    }

--------------------------------------------------------------------------
三个已知简化，别拿这个模块对账
--------------------------------------------------------------------------
  1. **不含中国法定节假日日历** —— 节假日全天应算空闲，这里会按工作日规则算成高峰。
  2. 思维链 token 已包含在 completion_tokens 里，按「输出」计一次即可，**不要**再加一遍。
  3. **只支持按 token 计费**。按次计费（生图按张）、按时长计费（语音按秒）的模型
     本引擎表达不了，别硬塞进来。
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 北京时间。沙箱内 /etc/localtime 未必是 CST，显式用偏移量，避免算错峰谷。
CST = timezone(timedelta(hours=8))

# 高峰时段（北京时间）：(开始小时, 结束小时)，左闭右开
PEAK_WINDOWS: list[tuple[int, int]] = [(9, 12), (14, 18)]

# 全局默认高峰倍数（DeepSeek 规则：空闲 = 高峰的一半）
PEAK_MULTIPLIER = 2.0

# ---------------------------------------------------------------- 内置单价表
PRICING: dict[str, dict[str, Any]] = {
    "deepseek-flash": {
        "label": "DeepSeek Flash",
        "vendor": "deepseek",
        "in": 1.0,
        "out": 4.0,
        "in_cached": 0.02,
        "context_window": 1_000_000,
        "reasoning": True,
    },
    "deepseek-v4-pro": {
        "label": "DeepSeek V4 Pro",
        "vendor": "deepseek",
        "in": 4.5,
        "out": 13.5,
        "in_cached": 0.15,
        "context_window": 1_000_000,
        "reasoning": True,
    },
    # 免费模型：in/out 填 0（是「明确不要钱」，不是「没登记」），并显式标 free: true。
    # 要点：免费模型也必须在表里登记——否则 priced_as 会回落到兜底模型（DeepSeek），
    # 标签被错显成 DeepSeek；且 is_known 会判 False，前端又补「单价未登记」，
    # 于是出现「免费·单价未登记」这种自相矛盾。填 0 + free:true 可同时解决这两点。
    "glm-4-flash": {
        "label": "GLM-4-Flash",
        "vendor": "zhipu",
        "in": 0.0,
        "out": 0.0,
        "free": True,
    },
    "@cf/meta/llama-3.1-8b-instruct-fp8": {
        "label": "Cloudflare 对话（免费）",
        "vendor": "cloudflare",
        "in": 0.0,
        "out": 0.0,
        "free": True,
    },
    "@cf/baai/bge-m3": {
        "label": "Cloudflare 向量化（免费）",
        "vendor": "cloudflare",
        "in": 0.0,
        "out": 0.0,
        "free": True,
    },
    "@cf/black-forest-labs/flux-1-schnell": {
        "label": "Cloudflare 文生图（免费）",
        "vendor": "cloudflare",
        "in": 0.0,
        "out": 0.0,
        "free": True,
    },
    # 同上：CF 上的智谱开源模型，免费档。和 glm-4-flash（zhipu）一样是免费模型，
    # 不在表里的后果是「单价未登记」警告 + 按 deepseek 兜底价误记成花钱。
    "@cf/zai-org/glm-4.7-flash": {
        "label": "GLM-4.7-Flash（CF 免费）",
        "vendor": "cloudflare",
        "in": 0.0,
        "out": 0.0,
        "free": True,
    },
}

# 模型别名：官方说旧名仍可调用，但按新名价格计费
ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4-pro-0813": "deepseek-v4-pro",
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}

# 没登记过的模型按这个价兜底，并让调用方知道是估算（前端要显式标注）
DEFAULT_MODEL = "deepseek-flash"

# 单价文件的默认位置（backend/config/pricing.json）
_HERE = Path(__file__).resolve().parent.parent.parent  # -> backend/
DEFAULT_PRICING_FILE = _HERE / "config" / "pricing.json"

# 最近一次加载状态，给 /api/usage/pricing 展示"现在按哪套价在算"
_load_state: dict[str, Any] = {"file": None, "file_loaded": False, "env_loaded": False}


# ---------------------------------------------------------------- 加载
def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """按模型粒度合并，字段级覆盖（不是整条替换，免得补个 label 把价格丢了）。"""
    for model, rules in patch.items():
        if not isinstance(rules, dict):
            continue
        base[str(model)] = {**base.get(str(model), {}), **rules}
    return base


def load_pricing_file(path: str | Path | None = None) -> bool:
    """从 JSON 文件加载/刷新单价。返回是否真的加载了。

    文件格式就是 PRICING 的子集，键是模型名：
        {"qwen-max": {"label":"通义千问 Max","vendor":"aliyun","in":20,"out":60}}
    """
    file_path = Path(path or os.getenv("PRICING_FILE", "") or DEFAULT_PRICING_FILE)
    if not file_path.is_file():
        _load_state["file"] = str(file_path)
        _load_state["file_loaded"] = False
        return False
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _load_state["file"] = str(file_path)
        _load_state["file_loaded"] = False
        _load_state["error"] = f"单价文件解析失败：{exc}"
        return False
    if isinstance(data, dict):
        _deep_merge(PRICING, data)
    _load_state["file"] = str(file_path)
    _load_state["file_loaded"] = True
    _load_state.pop("error", None)
    return True


def _load_env_override() -> bool:
    """PRICING_JSON 环境变量覆盖。给容器/临时调试用，格式同文件。"""
    raw = os.getenv("PRICING_JSON", "").strip()
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if isinstance(data, dict):
        _deep_merge(PRICING, data)
    return True


def register_model(model: str, rules: dict[str, Any]) -> None:
    """运行时登记一个模型的单价（插件/工具调用方用）。"""
    PRICING[str(model)] = {**PRICING.get(str(model), {}), **rules}


def reload_pricing() -> dict[str, Any]:
    """回到内置默认值再重跑一遍完整加载链。返回加载状态。"""
    PRICING.clear()
    PRICING.update(copy.deepcopy(_BUILTIN_PRICING))
    _load_state["file_loaded"] = load_pricing_file()
    _load_state["env_loaded"] = _load_env_override()
    return dict(_load_state)


_BUILTIN_PRICING = copy.deepcopy(PRICING)

# 模块导入时按 内置 → 文件 → 环境变量 的顺序加载一次
_load_state["file_loaded"] = load_pricing_file()
_load_state["env_loaded"] = _load_env_override()


# ---------------------------------------------------------------- 查询
def canonical_model(model: str) -> str:
    """把别名/旧名归一到计价用的模型名。"""
    if not model:
        return DEFAULT_MODEL
    return ALIASES.get(model, model)


def is_known(model: str) -> bool:
    """这个模型的单价登记过吗？没登记过前端要提示"按兜底价估算"。"""
    return canonical_model(model) in PRICING


def is_free(model: str) -> bool:
    """这是免费模型吗？

    两种写法都认：显式 `"free": true`，或者 in/out/in_cached 全填 0。
    免费模型的费用是 **0 元**，但它**不是未知模型** —— 这两件事必须分开，
    否则「没登记过单价」会被当成「不要钱」，账目只会越算越少，比算多了更危险。
    """
    rules = PRICING.get(canonical_model(model))
    if rules is None:
        return False
    if rules.get("free") is True:
        return True
    keys = [k for k in ("in", "out", "in_cached") if rules.get(k) is not None]
    # 一个价都没填不算免费，算「不知道多少钱」
    if not keys:
        return False
    return all(float(rules[k]) == 0.0 for k in keys) and not rules.get("tiers")


def _has_any_price(rules: dict[str, Any]) -> bool:
    """这条规则里有任何一个可用单价吗？

    「登记了条目但一条价都没填」是很常见的疏漏（只填了 label）。这种情况
    **不能当成免费** —— 它和没登记是一回事，都得按兜底价估，否则账目偏低。
    """
    return any(rules.get(k) is not None for k in ("in", "out", "in_cached"))


def billing_of(model: str) -> str:
    """这个模型在单价表里的性质："free" / "paid" / "unknown"。

    与 `is_known` 的区别是它直接给出**三态**，调用方不用自己拼逻辑：
      - free：明确免费（free 标记或单价全 0）→ 记 0 元
      - paid：有它自己的价 → 按价算
      - unknown：没登记 / 登记了没填价 → 按兜底价估，前端标 ⚠
    """
    if is_free(model):
        return "free"
    if priced_as(model) == canonical_model(model):
        return "paid"
    return "unknown"


def priced_as(model: str) -> str:
    """实际按哪个模型的价在算（没价可依时返回兜底模型名）。"""
    name = canonical_model(model)
    rules = PRICING.get(name)
    if rules is not None and _has_any_price(rules):
        return name
    return DEFAULT_MODEL


def price_of(model: str) -> dict[str, Any]:
    """取单价规则。没登记 / 登记了但没价，都回落到兜底模型的价。"""
    return PRICING.get(priced_as(model), PRICING[DEFAULT_MODEL])


def label_of(model: str) -> str:
    """展示名。没配 label 就直接显示模型名。"""
    rules = PRICING.get(canonical_model(model), {})
    return rules.get("label") or canonical_model(model)


def peak_multiplier_of(model: str) -> float:
    """高峰倍数。有些厂商全天一个价，配成 1.0 就不会被莫名翻倍。"""
    rules = PRICING.get(priced_as(model), {})
    return float(rules.get("peak_multiplier", PEAK_MULTIPLIER))


def is_peak_hour(ts: int | None = None) -> bool:
    """判断某个时间戳是否处于高峰计费时段。

    ts 为 unix 秒；None 表示"现在"。
    规则：北京时间周一至周五（不含法定节假日）9-12 点、14-18 点。
    """
    moment = datetime.fromtimestamp(ts, CST) if ts else datetime.now(CST)
    if moment.weekday() >= 5:  # 5=周六 6=周日
        return False
    return any(start <= moment.hour < end for start, end in PEAK_WINDOWS)


def _unit_price(rules: dict[str, Any], key: str, prompt_tokens: int) -> float | None:
    """取某个计费项的有效单价，应用长上下文档位。

    档位是"整档价"：输入超过 above 后全部按档内价，不是分段累加。
    （厂商文档基本都按这个口径写，分段累加法反而对不上。）
    """
    price = rules.get(key)
    if price is None:
        return None
    for tier in rules.get("tiers", []) or []:
        if prompt_tokens > int(tier.get("above", 0)) and tier.get(key) is not None:
            price = tier[key]
    return float(price)


def cost_breakdown(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    *,
    is_peak: bool | None = None,
    ts: int | None = None,
) -> dict[str, Any]:
    """把一次调用的费用拆开算，便于前端回答"钱花在哪儿了"。

    返回：
      {
        "model": 实际计价的模型, "label": 展示名, "known": 是否登记过单价,
        "billing": "free" | "paid" | "unknown"（免费 / 有价 / 没登记按兜底估）,
        "is_peak": ..., "peak_multiplier": ...,
        "parts": [{"key","label","tokens","unit_price","cost"}...],
        "cost": 合计
      }
    """
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    cached = max(0, min(int(cached_tokens or 0), prompt))
    miss = prompt - cached

    actual = priced_as(model)
    rules = price_of(model)
    free = is_free(model)
    # 「known」= 这笔钱是按**它自己的价**算出来的。
    # 没登记、或登记了没填价，都是按兜底价估的，前端要给 ⚠，别装作精确。
    known = free or actual == canonical_model(model)

    if is_peak is None:
        is_peak = is_peak_hour(ts)
    # 免费模型不谈峰谷：0 乘几倍都是 0，但展示「高峰 ×2」会让人以为有价格波动
    mult = 1.0 if free else (peak_multiplier_of(model) if is_peak else 1.0)

    parts: list[dict[str, Any]] = []
    if cached:
        unit = _unit_price(rules, "in_cached", prompt) or _unit_price(rules, "in", prompt) or 0.0
        parts.append(
            {
                "key": "in_cached",
                "label": "输入·缓存命中",
                "tokens": cached,
                "unit_price": round(unit * mult, 4),
                "cost": cached * unit * mult / 1_000_000.0,
            }
        )
    if miss:
        unit = _unit_price(rules, "in", prompt) or 0.0
        parts.append(
            {
                "key": "in",
                "label": "输入·未命中",
                "tokens": miss,
                "unit_price": round(unit * mult, 4),
                "cost": miss * unit * mult / 1_000_000.0,
            }
        )
    if completion:
        unit = _unit_price(rules, "out", prompt) or 0.0
        parts.append(
            {
                "key": "out",
                "label": "输出（含思维链）",
                "tokens": completion,
                "unit_price": round(unit * mult, 4),
                "cost": completion * unit * mult / 1_000_000.0,
            }
        )

    total = sum(p["cost"] for p in parts)
    return {
        "model": actual,
        "label": label_of(actual),
        "known": known,
        "billing": "free" if free else ("paid" if known else "unknown"),
        "is_peak": bool(is_peak),
        "peak_multiplier": mult,
        "parts": parts,
        "cost": round(total, 6),
    }


def estimate_cost(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    *,
    is_peak: bool | None = None,
    ts: int | None = None,
) -> float:
    """估算一次调用的费用（元）。cost_breakdown 的简写版。

    口径：
      - cached_tokens 按 in_cached 单价；剩下的输入按 in 单价
      - completion_tokens 按 out 单价（思维链含在内，不再单算）
      - 高峰 × 模型自己的 peak_multiplier（默认 2.0）
    """
    return cost_breakdown(
        model,
        prompt_tokens,
        completion_tokens,
        cached_tokens,
        is_peak=is_peak,
        ts=ts,
    )["cost"]


def pricing_table() -> list[dict[str, Any]]:
    """给前端展示的单价表（含高峰价，前端不用自己乘）。"""
    out = []
    for model, p in PRICING.items():
        mult = float(p.get("peak_multiplier", PEAK_MULTIPLIER))
        row: dict[str, Any] = {
            "model": model,
            "label": p.get("label") or model,
            "vendor": p.get("vendor", ""),
            "unit": "元 / 百万 tokens",
            "context_window": p.get("context_window"),
            "reasoning": bool(p.get("reasoning", False)),
            "idle": {},
            "peak": {},
            "tiers": p.get("tiers", []),
            "free": is_free(model),
        }
        for key in ("in_cached", "in", "out"):
            if p.get(key) is None:
                continue
            row["idle"][key] = float(p[key])
            row["peak"][key] = round(float(p[key]) * mult, 4)
        # 没有缓存价就别在界面上显示这一列，显示 0 会让人以为缓存不要钱
        row["has_cache_price"] = p.get("in_cached") is not None
        out.append(row)
    out.sort(key=lambda r: (r["vendor"], r["model"]))
    return out


def pricing_state() -> dict[str, Any]:
    """单价表的加载来源，页面上展示"现在按哪套价在算"，排查时用得上。"""
    return {
        "models": len(PRICING),
        "default_model": DEFAULT_MODEL,
        "peak_multiplier": PEAK_MULTIPLIER,
        "peak_windows": [f"{s:02d}:00-{e:02d}:00" for s, e in PEAK_WINDOWS],
        "timezone": "Asia/Shanghai (UTC+8)",
        **dict(_load_state),
    }
