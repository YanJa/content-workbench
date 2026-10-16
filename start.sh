#!/usr/bin/env bash
# 内容运营工作台 一键启动（Linux / macOS）
#
# 两种运行形态，本脚本自动识别：
#
#   A. 沙箱内（检测到平台 supervisord）
#      把 backend / frontend / tunnel-guard 交给平台 supervisord 托管，
#      进程崩溃自动拉起。脚本跑完即退出，不占用终端。
#      注意：沙箱休眠重启后配置会丢（平台卷只读 + include 目录是 tmpfs），
#            重新执行本脚本即可，幂等。详见 deploy/README.md。
#
#   B. 沙箱外（本机 / 服务器）
#      传统前台方式：准备依赖 → 后台起进程 → wait 挂住终端。
#
# 子命令：
#   ./start.sh          启动（默认）
#   ./start.sh status   查看状态（仅形态 A）
#   ./start.sh stop     停止（仅形态 A）
#
# 需要：uv、pnpm、Python 3.11+
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# 形态 A 的判定：存在平台 supervisord 且它的 include 目录可写
SV_DIR="${IDE_EDITOR_SERVER_DIR:-/.PlnPyKFp4CRfFtgC1}"
SV_BIN="$SV_DIR/bin/supervisord"
SV_CONF="$SV_DIR/supervisord-conf/supervisord.conf"
SV_INCLUDE="${SV_DIR}_run/supervisord-conf"
HAS_SUPERVISOR=0
if [ -x "$SV_BIN" ] && [ -f "$SV_CONF" ] && [ -d "$SV_INCLUDE" ] && [ -w "$SV_INCLUDE" ]; then
    HAS_SUPERVISOR=1
fi

# supervisord 状态：剥掉 ANSI 颜色码，方便后续用 ^ 锚定匹配
sv_status() {
    "$SV_BIN" -c "$SV_CONF" ctl status 2>/dev/null | sed -r 's/\x1b\[[0-9;]*m//g'
}

ACTION="${1:-start}"

echo "============================================================"
echo "  内容运营工作台  一键启动"
echo "============================================================"

# ---------- 形态 A：托管给 supervisord ----------
if [ "$HAS_SUPERVISOR" = "1" ]; then
    echo "  运行形态：沙箱（supervisord 托管，崩溃自愈）"
    echo

    case "$ACTION" in
    status)
        # ctl status 输出带 ANSI 颜色码，必须先剥掉，否则 ^cwb- 锚定匹配不到
        sv_status | grep -E '^cwb-' || echo "  cwb-* 未托管，先运行 ./start.sh"
        exit 0
        ;;
    stop)
        "$SV_BIN" -c "$SV_CONF" ctl stop cwb-tunnel cwb-frontend cwb-backend 2>/dev/null || true
        echo "  已停止"
        exit 0
        ;;
    esac

    # 依赖就绪检查：缺什么装什么，不重复装
    echo "[1/3] 检查依赖..."
    PY="${CWB_PYTHON:-/root/.pyenv/versions/3.11.1/bin/python3}"
    [ -x "$PY" ] || PY="$(command -v python3)"
    if ! "$PY" -c 'import fastapi, uvicorn, openai' 2>/dev/null; then
        echo "      安装后端依赖（$PY）..."
        if command -v uv >/dev/null 2>&1; then
            (cd "$ROOT/backend" && uv pip install --python "$PY" -r requirements.txt -i "$MIRROR")
        else
            "$PY" -m pip install -r "$ROOT/backend/requirements.txt" -i "$MIRROR"
        fi
    else
        echo "      后端依赖已就绪"
    fi

    if [ ! -f "$ROOT/backend/.env" ]; then
        echo "      [!] 未找到 backend/.env，已从 .env.example 生成"
        echo "      请先编辑填入 DEEPSEEK_API_KEY，然后重新运行本脚本"
        cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
        exit 1
    fi

    if [ ! -d "$ROOT/frontend/node_modules" ]; then
        echo "      安装前端依赖..."
        (cd "$ROOT/frontend" && pnpm install)
    else
        echo "      前端依赖已就绪"
    fi

    echo "[2/3] 安装/刷新 supervisord 配置..."
    bash "$ROOT/deploy/install-autostart.sh"

    echo "[3/3] 健康检查..."
    sleep 2
    curl -s -o /dev/null -m 5 -w "      前端 :5173  HTTP %{http_code}\n" http://127.0.0.1:5173/ || true
    curl -s -m 5 http://127.0.0.1:8000/api/health -w "\n" | sed 's/^/      后端 :8000  /' || true

    echo
    echo "============================================================"
    echo "  完成（服务由 supervisord 托管，本脚本不占用终端）"
    echo "    查看状态： ./start.sh status"
    echo "    停止服务： ./start.sh stop"
    echo "    日志：     tail -f /tmp/cwb-backend.log /tmp/cwb-frontend.log /tmp/tunnel-guard.log"
    echo
    echo "  ⚠️ 沙箱休眠重启后需重新执行 ./start.sh（平台卷只读，无法写入开机钩子）"
    echo "============================================================"
    exit 0
fi

# ---------- 形态 B：本机前台运行 ----------
echo "  运行形态：本机（前台进程，Ctrl+C 结束）"
echo

# ---------- 后端 ----------
echo "[1/2] 准备后端..."
cd "$ROOT/backend"

[ -d .venv ] || { echo "      创建虚拟环境..."; uv venv --python 3.11; }

echo "      安装依赖（清华源）..."
uv pip install -r requirements.txt -i "$MIRROR"

if [ ! -f .env ]; then
    echo "      [!] 未找到 backend/.env，已从 .env.example 生成"
    echo "      请先编辑填入 DEEPSEEK_API_KEY，然后重新运行本脚本"
    cp .env.example .env
    exit 1
fi

echo "      启动后端 :8000"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 >/tmp/cwb-backend.log 2>&1 &
BACKEND_PID=$!
sleep 6

# ---------- 前端 ----------
echo "[2/2] 准备前端..."
cd "$ROOT/frontend"

[ -d node_modules ] || { echo "      安装依赖..."; pnpm install; }

echo "      启动前端 :5173"
pnpm dev >/tmp/cwb-frontend.log 2>&1 &
FRONTEND_PID=$!

echo
echo "============================================================"
echo "  启动完成"
echo "    前端   http://localhost:5173"
echo "    后端   http://localhost:8000/api/health"
echo
echo "  后端 PID=$BACKEND_PID  前端 PID=$FRONTEND_PID"
echo "  停止：kill $BACKEND_PID $FRONTEND_PID"
echo "============================================================"

wait
