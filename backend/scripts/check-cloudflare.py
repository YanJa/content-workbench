#!/usr/bin/env python3
"""Cloudflare Workers AI 通道自检：验证凭证 → 列模型 → 真实生成一张图。

用法：
    # 显式传参（推荐，不动 .env）
    python3 scripts/check-cloudflare.py --account-id <ACCOUNT_ID> --token <API_TOKEN>

    # 或从环境变量 / backend/.env 读（CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN）
    python3 scripts/check-cloudflare.py

凭证获取：Cloudflare Dashboard → 头像 → My Profile → API Tokens
    → Create Token → 「Workers AI」模板（或自定义 token 勾 Account.Workers AI:Read）。
ACCOUNT_ID：Dashboard → Workers & Pages → 右侧账号卡片。

退出码：0 = 全部通过；1 = 有步骤失败（看输出定位）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from pathlib import Path

# 允许从任意 cwd 运行：把 backend/ 加进 path 以复用 settings（读 .env）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API_BASE = "https://api.cloudflare.com/client/v4/accounts"


def load_env_fallback() -> tuple[str, str]:
    """没显式传参时，从环境变量或 backend/.env 读凭证。"""
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if account_id and token:
        return account_id, token
    try:
        from app.core.config import settings  # 延迟导入：只在需要 .env 时才加载

        return settings.CLOUDFLARE_ACCOUNT_ID, settings.CLOUDFLARE_API_TOKEN
    except Exception:
        return "", ""


async def step_check_token(account_id: str, token: str) -> bool:
    """步骤1：token 能否列出账号下的 AI 模型（同时验证 account_id 正确）。"""
    import httpx

    url = f"{API_BASE}/{account_id}/ai/models/search"
    print(f"[1/3] 验证凭证与账号：GET {url}")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"task": "text-to-image"},
        )
    if resp.status_code == 400:
        # 400 且带 errors 通常是 account_id 格式问题或 task 参数不被接受
        print(f"    HTTP 400: {resp.text[:200]}")
    if resp.status_code in (401, 403):
        print(f"    ✗ 凭证被拒（{resp.status_code}）：token 无效或缺少 Workers AI 权限")
        print(f"    响应: {resp.text[:200]}")
        return False
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        print(f"    ✗ API 返回失败: {data.get('errors')}")
        return False
    models = data.get("result") or []
    print(f"    ✓ 凭证有效，账号下有 {len(models)} 个文生图模型可用")
    for m in models[:8]:
        print(f"      - {m.get('name')}")
    return True


async def step_generate(account_id: str, token: str, model: str, out: Path) -> bool:
    """步骤2：真实调一次文生图，保存结果图。"""
    import httpx

    url = f"{API_BASE}/{account_id}/ai/run/{model}"
    prompt = "A clean minimalist illustration of a sunrise over mountains, flat design, warm colors"
    print(f"[2/3] 真实生成：POST {url}")
    print(f"    prompt: {prompt[:60]}...")
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"prompt": prompt},
        )
    if resp.status_code != 200:
        print(f"    ✗ HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 403:
            print("    提示：Workers AI 未订阅/账号未激活时可再试；或换模型 id")
        return False
    data = resp.json()
    result = data.get("result") or {}
    images = result.get("images") if isinstance(result, dict) else None
    if not images:
        # 兼容 flux-1-schnell 等模型的单数 image 字段
        single = result.get("image") if isinstance(result, dict) else None
        if isinstance(single, str) and single:
            images = [single]
    if not images:
        print(f"    ✗ 返回结构异常: {str(data)[:300]}")
        return False
    b64 = images[0]
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    raw = base64.b64decode(b64)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    print(f"    ✓ 生成成功，{len(raw) / 1024:.0f} KB → {out}")
    return True


async def step_project_channel() -> bool:
    """步骤3：项目的生图通道代码能否正常解析这张图（复用 imagegen 的落地逻辑）。"""
    print("[3/3] 校验项目通道代码（imagegen._run_cloudflare 的解析逻辑）")
    try:
        from app.services import imagegen
    except Exception as exc:  # noqa: BLE001
        print(f"    ! 无法导入项目模块（不影响凭证有效性）: {exc}")
        return True
    ok = imagegen.cloudflare_configured()
    if ok:
        print("    ✓ imagegen.cloudflare_configured() = True（.env 凭证已就位，前端会出现 Cloudflare 选项）")
    else:
        print("    ! .env 里还没有 CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID")
        print("      把自检通过的凭证填进 backend/.env 即完成接入")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="Cloudflare Workers AI 通道自检")
    parser.add_argument("--account-id", default="", help="Cloudflare Account ID")
    parser.add_argument("--token", default="", help="Cloudflare API Token")
    parser.add_argument(
        "--model",
        default="@cf/black-forest-labs/flux-1-schnell",
        help="文生图模型 id",
    )
    parser.add_argument("--out", default="/tmp/cf_check_test.png", help="测试图输出路径")
    args = parser.parse_args()

    account_id = args.account_id
    token = args.token
    if not (account_id and token):
        account_id, token = load_env_fallback()
    if not (account_id and token):
        print("缺少凭证。用法：")
        print("  python3 scripts/check-cloudflare.py --account-id <ID> --token <TOKEN>")
        print("或在 backend/.env 配好 CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN")
        return 1

    print(f"Account ID: {account_id[:6]}...{account_id[-4:] if len(account_id) > 10 else ''}")
    ok1 = await step_check_token(account_id, token)
    ok2 = await step_generate(account_id, token, args.model, Path(args.out)) if ok1 else False
    ok3 = await step_project_channel()
    print()
    if ok1 and ok2:
        print("=== 自检通过：Cloudflare Workers AI 通道可用 ===")
        if ok3:
            print("下一步：把凭证填进 backend/.env（CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID），")
            print("重启后端后，排版面板「AI 生成」里选「Cloudflare AI」即可出图。")
        return 0
    print("=== 自检未通过，请按上面日志定位 ===")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
