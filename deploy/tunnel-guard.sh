#!/usr/bin/env bash
# SSH 反向隧道守护脚本
#
# 作用：沙箱休眠、网络抖动导致 SSH 隧道断开后自动重连。
# 背景：ssh -R 不像 gateway 那样常驻，断了不会自愈，
#       没有这个脚本的话，每次休眠后都得手动重新建立隧道。
#
# 用法：
#   chmod +x deploy/tunnel-guard.sh
#   setsid ./deploy/tunnel-guard.sh > /tmp/tunnel-guard.log 2>&1 &
#
# 停止：
#   pkill -f tunnel-guard.sh && pkill -f "ssh -i /root/.ssh/wb_tunnel_key"

set -u

SSH_KEY="${TUNNEL_KEY:-/root/.ssh/wb_tunnel_key}"
REMOTE_USER="${TUNNEL_USER:-ubuntu}"
REMOTE_HOST="${TUNNEL_HOST:-49.233.223.252}"
REMOTE_PORT="${TUNNEL_REMOTE_PORT:-18080}"   # 服务器侧回环端口
LOCAL_HOST="127.0.0.1"
LOCAL_PORT="${TUNNEL_LOCAL_PORT:-5173}"      # 沙箱内 Vite dev server

CHECK_INTERVAL=15

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $*"
}

# 判断隧道是否还活着：优先查进程，再复核远端端口是否真的在监听
tunnel_alive() {
    pgrep -f "ssh -i ${SSH_KEY}.*${REMOTE_PORT}:${LOCAL_HOST}:${LOCAL_PORT}" >/dev/null 2>&1
}

# 本地目标端口（Vite/后端）是否在监听——隧道有没有意义的前提
local_target_alive() {
    (echo >/dev/tcp/${LOCAL_HOST}/${LOCAL_PORT}) 2>/dev/null
}

start_tunnel() {
    log "建立隧道 ${REMOTE_HOST}:${REMOTE_PORT} -> ${LOCAL_HOST}:${LOCAL_PORT}"
    # 目标服务没起来时先不起隧道：否则 -R 挂上去也是指向空气，
    # 而且会让 tunnel_alive 判定为「已连接」，掩盖真实问题。
    if ! (echo >/dev/tcp/${LOCAL_HOST}/${LOCAL_PORT}) 2>/dev/null; then
        log "本地 ${LOCAL_PORT} 未监听，跳过本次建连（等服务起来后自动重试）"
        return 0
    fi
    setsid ssh \
        -i "$SSH_KEY" \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/root/.ssh/known_hosts \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes \
        -N \
        -R "${REMOTE_PORT}:${LOCAL_HOST}:${LOCAL_PORT}" \
        "${REMOTE_USER}@${REMOTE_HOST}" \
        >>/tmp/tunnel.log 2>&1 </dev/null &
}

log "守护进程启动 (pid=$$)"
log "目标 ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PORT} -> ${LOCAL_HOST}:${LOCAL_PORT}"

while true; do
    if ! local_target_alive; then
        log "本地 ${LOCAL_PORT} 未监听，等待服务启动..."
        # 目标刚起来时给一点缓冲，避免立刻建连又失败
        sleep 2
        continue
    fi
    if ! tunnel_alive; then
        log "隧道不可用，尝试重连..."
        start_tunnel
        sleep 5
        if tunnel_alive; then
            log "重连成功"
        else
            log "重连失败，${CHECK_INTERVAL}s 后重试"
        fi
    fi
    sleep "$CHECK_INTERVAL"
done
