"""朱雀 AI 检测（zhuque 服务）+ 报告单生成（detect_report）+ 路由降级链路。

背景：用户接入了腾讯朱雀 @makers/zhuque-text（官方同款三分类：人工/疑似AI/AI 特征），
并要求我们自己的检测也能出官方报告单那种版式的报告。v1.7.4 落地：
  - services/zhuque.py：分类客户端，归一化 labels_ratio / segment_labels；
  - services/detect_report.py：报告单 HTML（环形图 + AI 分布色带 + 片段解析
    + 本地体检 + 官方同款免责声明）；
  - routers/humanize.py：POST /zhuque（同步检测）、POST /report（生成报告，
    朱雀未配置/失败自动降级为纯本地体检报告）。

所有测试均 mock HTTP / settings，不真调朱雀（免费 token 额度不拿来烧）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import settings
from app.routers.humanize import ReportRequest, ZhuqueRequest, zhuque_detect
from app.routers.humanize import detect_report_page
from app.services import detect_report, humanize as humanize_svc, zhuque as zhuque_svc

_HUMAN_TEXT = (
    "今天早上我出门的时候发现楼下的煎饼摊排了十几个人的队。说实话我有点不理解，"
    "这个摊子五块钱一个煎饼，比隔壁贵两块，但队伍就是不断。后来我想明白了，"
    "大家买的不是煎饼，是那个现做的热乎劲儿。"
)

_FAKE_ZHUQUE_PAYLOAD = {
    "status": "success",
    "softmax_confidence": 0.9998,
    "ratio_confidence": 1,
    "labels_ratio": {"0": 0.1076, "1": 0.1910, "2": 0.7014},
    "segment_labels": [
        {"text": "第一段：AI 写的总结陈词。综上所述，这不仅是一次变革。", "label": 1, "conf": 0.9975, "order": 1, "position": [0, 24]},
        {"text": "第二段：我说不好，大概也许可能是因为天气太热了。", "label": 0, "conf": 0.4753, "order": 2, "position": [24, 48]},
    ],
    "usage": {"total_tokens": 92},
    "msg": "",
    "makers_models_usage": {"total_tokens": 154},
}


def _run(coro):
    return asyncio.run(coro)


def _fake_client(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"{}"
    resp.json.return_value = payload
    inst = AsyncMock()
    inst.__aenter__.return_value = inst
    inst.post.return_value = resp
    return inst


# ---------------------------------------------------------------- 客户端归一化
async def _a_classify_success():
    with patch.object(settings, "ZHUQUE_API_KEY", "test-key"), \
         patch("httpx.AsyncClient", return_value=_fake_client(_FAKE_ZHUQUE_PAYLOAD)):
        res = await zhuque_svc.classify("待检测文本")
    assert res["ok"] is True
    assert res["human_ratio"] == 0.1076
    assert res["ai_ratio"] == 0.1910
    assert res["suspect_ratio"] == 0.7014
    assert res["softmax_confidence"] == 0.9998
    assert len(res["segments"]) == 2
    assert res["segments"][0]["label_name"] == "AI特征"
    assert res["segments"][1]["label_name"] == "人工特征"
    assert res["tokens_used"] == 154  # makers_models_usage 才是扣额度口径


def test_classify_success_normalizes():
    _run(_a_classify_success())


async def _a_classify_unconfigured():
    with patch.object(settings, "ZHUQUE_API_KEY", ""):
        res = await zhuque_svc.classify("待检测文本")
    assert res["ok"] is False
    assert res["configured"] is False
    assert "ZHUQUE_API_KEY" in res["error"]


def test_classify_unconfigured():
    """未配 key 时明确报错，不静默也不发请求。"""
    _run(_a_classify_unconfigured())


async def _a_classify_empty():
    res = await zhuque_svc.classify("   ")
    assert res["ok"] is False
    assert "没有可检测" in res["error"]


def test_classify_empty_text():
    _run(_a_classify_empty())


async def _a_classify_api_error():
    payload = {"status": "error", "msg": "quota exceeded"}
    with patch.object(settings, "ZHUQUE_API_KEY", "test-key"), \
         patch("httpx.AsyncClient", return_value=_fake_client(payload)):
        res = await zhuque_svc.classify("待检测文本")
    assert res["ok"] is False
    assert "quota exceeded" in res["error"]


def test_classify_api_error():
    _run(_a_classify_api_error())


# ---------------------------------------------------------------- 阈值与结论
def test_band_of_thresholds():
    """官方报告同款阈值：<0.5 人工 / 0.5–0.9 疑似 / ≥0.9 AI。"""
    assert zhuque_svc.band_of(0.0)[0] == "human"
    assert zhuque_svc.band_of(0.49)[0] == "human"
    assert zhuque_svc.band_of(0.5)[0] == "suspect"
    assert zhuque_svc.band_of(0.89)[0] == "suspect"
    assert zhuque_svc.band_of(0.9)[0] == "ai"
    assert zhuque_svc.band_of(1.0)[0] == "ai"


def test_conclusion_tone():
    """官方样张同款口径：人工占比 10.76%（<20%）→「人工创作特征较弱」。"""
    tone, text = zhuque_svc.conclusion(human_ratio=0.1076, ai_ratio=0.1910, suspect_ratio=0.7014)
    assert tone == "bad"
    assert text == "人工创作特征较弱"
    tone, text = zhuque_svc.conclusion(human_ratio=0.9, ai_ratio=0.05, suspect_ratio=0.05)
    assert tone == "good"


# ---------------------------------------------------------------- 报告单生成
def _full_report() -> str:
    scan = humanize_svc.scan(_HUMAN_TEXT, "zhihu")
    zq = {
        "ok": True,
        "human_ratio": 0.1076,
        "ai_ratio": 0.1910,
        "suspect_ratio": 0.7014,
        "segments": [
            {"text": "第一段：AI 写的总结陈词。综上所述，这不仅是一次变革。", "label": 1, "label_name": "AI特征", "conf": 0.9975, "order": 1, "position": [0, 24]},
            {"text": "第二段：我说不好，大概也许可能是因为天气太热了。", "label": 0, "label_name": "人工特征", "conf": 0.4753, "order": 2, "position": [24, 48]},
        ],
    }
    return detect_report.render_report(text=_HUMAN_TEXT, scan=scan, zhuque_result=zq, platform="zhihu")


def test_report_contains_zhuque_sections():
    """报告单必须含官方同款四件套 + 本地体检 + 免责声明。"""
    html_text = _full_report()
    assert "AI 生成检测报告单" in html_text
    assert "人工创作特征较弱" in html_text            # 结论横幅
    assert "检测结果" in html_text                     # 环形图区
    assert "AI 分布图" in html_text                    # 色带
    assert "片段解析" in html_text                     # 分段表
    assert "AIGC 值" in html_text
    assert "检测片段详情" in html_text                 # 片段原文
    assert "本地人工痕迹体检" in html_text             # 我们的规则体检
    assert "人工痕迹指数" in html_text
    assert "仅为辅助判断" in html_text                 # 官方同款免责声明
    assert "不应作为任何审核或处罚的决定性依据" in html_text


def test_report_escapes_text():
    """正文里的 HTML 必须被转义或清洗，防报告注入。
    scan 的样本清洗会剥掉 HTML 标签（第一道防线），报告层的 _e() 转义是第二道；
    无论哪层生效，最终报告里都不能出现可执行的裸标签。"""
    evil = "综上所述<script>alert(1)</script>这不仅是变革，更是未来。"
    scan = humanize_svc.scan(evil, "gzh")
    html_text = detect_report.render_report(text=evil, scan=scan, zhuque_result=None)
    assert "<script>alert(1)</script>" not in html_text
    assert "<script>" not in html_text


def test_report_degrades_without_zhuque():
    """朱雀未配置时降级为纯本地体检报告，而不是报错。"""
    scan = humanize_svc.scan(_HUMAN_TEXT, "zhihu")
    html_text = detect_report.render_report(
        text=_HUMAN_TEXT,
        scan=scan,
        zhuque_result={"ok": False, "error": "未配置 ZHUQUE_API_KEY（EdgeOne Makers API Key，见系统设置）"},
    )
    assert "AI 生成检测报告单" in html_text
    assert "朱雀 AI 检测未执行" in html_text
    assert "本地人工痕迹体检" in html_text
    assert "仅为辅助判断" in html_text  # 免责声明仍在


# ---------------------------------------------------------------- 路由层
async def _a_route_zhuque():
    with patch.object(zhuque_svc, "classify", new=AsyncMock(return_value={"ok": True, "human_ratio": 0.5})):
        res = await zhuque_detect(ZhuqueRequest(text="待检测文本"), owner="admin", db=None)
    assert res["ok"] is True


def test_route_zhuque_calls_service():
    _run(_a_route_zhuque())


async def _a_route_report_degrades():
    """朱雀未配置时报告端点降级为纯本地报告，不 500。"""
    with patch.object(zhuque_svc, "configured", return_value=False):
        resp = await detect_report_page(
            ReportRequest(text=_HUMAN_TEXT, platform="zhihu"), owner="admin", db=None
        )
    body = resp.body.decode()
    assert "AI 生成检测报告单" in body
    assert "朱雀 AI 检测未执行" in body


def test_route_report_degrades():
    _run(_a_route_report_degrades())


async def _a_route_report_full():
    """朱雀就绪时报告含三分类与片段解析。"""
    with patch.object(zhuque_svc, "configured", return_value=True), \
         patch.object(zhuque_svc, "classify", new=AsyncMock(return_value={
             "ok": True,
             "human_ratio": 0.1076,
             "ai_ratio": 0.1910,
             "suspect_ratio": 0.7014,
             "segments": [{"text": "片段一", "label": 1, "label_name": "AI特征", "conf": 0.9975, "order": 1, "position": [0, 3]}],
         })):
        resp = await detect_report_page(
            ReportRequest(text=_HUMAN_TEXT, platform="zhihu"), owner="admin", db=None
        )
    body = resp.body.decode()
    assert "片段解析" in body
    assert "10.76%" in body
    assert "本地人工痕迹体检" in body


def test_route_report_full():
    _run(_a_route_report_full())
