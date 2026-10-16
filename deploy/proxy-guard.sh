#!/usr/bin/env bash
# 固定出口代理守护脚本（解决微信 IP 白名单漂移）
#
# 背景：
#   沙箱/容器出网 IP 是**动态池**（实测一次就探到 20+ 个），微信只接受单 IP 白名单，
#   根本加不完。做法是让**微信接口**的请求改走一台**固定公网 IP** 的机器出网。
#
# 方案：
#   借助已有的 SSH 免密通道（同 tunnel-guard.sh 用的 wb_tunnel_key），
#   在沙箱本地建一个 SOCKS5 代理（ssh -D），出口就是那台固定 IP 机器。
#   后端只需设 WECHAT_PROXY=socks5://127.0.0.1:10800，微信请求即走固定出口。
#
# 拓扑：
#   后端(沙箱) --socks5://127.0.0.1:10800--> [SSH -D] --SSH--> 固定IP机器 --出网--> api.weixin.qq.com
#
# 用法：
#   chmod +x deploy/proxy-guard.sh
#   setsid ./deploy/proxy-guard.sh > /tmp/proxy-guard.log 2>&1 &
# 停止：
#   pkill -f proxy-guard.sh
#
# 自测：
#   curl -s --socks5-hostname 127.0.0.1:10800 https://ifconfig.me   # 应返回固定 IP

set -u

SSH_KEY="${PROXY_KEY:-/root/.ssh/wb_tunnel_key}"
REMOTE_USER="${PROXY_USER:-ubuntu}"
REMOTE_HOST="${PROXY_HOST:-49.233.223.252}"      # 固定公网 IP 的机器
REMOTE_SSH_PORT="${PROXY_SSH_PORT:-22}"
LOCAL_PORT="${PROXY_LOCAL_PORT:-10800}"          # 沙箱本地 SOCKS5 监听端口
EXPECT_IP="${PROXY_EXPECT_IP:-49.233.223.252}"   # 期望出口 IP（用于健康校验）

CHECK_INTERVAL=15

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $*"; }

# 用 SOCKS5 探一次出口 IP，既验证隧道活着，也验证出口是我们想要的固定 IP
socks_egress_ip() {
    curl -s --max-time 10 --socks5-hostname "127.0.0.1:${LOCAL_PORT}" \
        https://ifconfig.me/ip 2>/dev/null | tr -d '[:space:]'
}

start_proxy() {
    log "建立 SOCKS5 代理 127.0.0.1:${LOCAL_PORT} -> ${REMOTE_USER}@${REMOTE_HOST}"
    setsid ssh \
        -i "$SSH_KEY" \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/root/.ssh/known_hosts \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes \
        -N -D "127.0.0.1:${LOCAL_PORT}" \
        -p "$REMOTE_SSH_PORT" \
        "${REMOTE_USER}@${REMOTE_HOST}" \
        >>/tmp/proxy-ssh.log 2>&1 </dev/null &
}

log "守护进程启动 (pid=$$)"
log "目标出口 ${EXPECT_IP}，本地 SOCKS5 127.0.0.1:${LOCAL_PORT}"

while true; do
    ip="$(socks_egress_ip)"
    if [ "$ip" != "$EXPECT_IP" ]; then
        log "出口异常（当前='${ip:-无}'，期望='${EXPECT_IP}'），重建代理..."
        pkill -f "ssh .*-D 127.0.0.1:${LOCAL_PORT} " >/dev/null 2>&1
        sleep 1
        start_proxy
        sleep 5
        ip="$(socks_egress_ip)"
        if [ "$ip" = "$EXPECT_IP" ]; then
            log "重建成功，出口 = ${ip}"
        else
            log "重建后出口仍为 '${ip:-无}'，${CHECK_INTERVAL}s 后重试"
        fi
    fi
    sleep "$CHECK_INTERVAL"
done
