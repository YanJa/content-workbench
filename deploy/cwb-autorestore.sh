#!/usr/bin/env bash
#
# cwb-autorestore.sh —— CWB 服务配置看门狗
#
# 背景：平台 ${IDE_EDITOR_SERVER_DIR}/workspace-init/init 会在运行期反复重建
# tmpfs include 目录（实测一天内冲掉过多次），每次重建后 cwb.conf 丢失 →
# supervisord reload → 四个 cwb-* program 被整体移除 → 隧道断 → 站点 502。
# 手动跑 install-autostart.sh 能恢复，但治标不治本。
#
# 本脚本是「治标」的自动化：独立常驻进程（不挂 supervisord，平台 reload
# 动不了它），周期性检查 cwb.conf 是否还在 include 目录、cwb-tunnel 是否
# 还在 supervisord 里，缺任何一个就重跑 install-autostart.sh（幂等）。
#
# 局限（说清楚，不误导）：
#   - 沙箱休眠/容器重启会杀掉本进程，那种情况仍需手动跑一次 install-autostart.sh；
#   - init 是只读挂载改不了，没法做到「平台侧开机自启」。
#
# 用法：setsid bash deploy/cwb-autorestore.sh >/tmp/cwb-autorestore.log 2>&1 &

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SV_DIR="${IDE_EDITOR_SERVER_DIR:-/.PlnPyKFp4CRfFtgC1}"
SV_MAIN_CONF="$SV_DIR/supervisord-conf/supervisord.conf"
SV_INCLUDE_DIR="${SV_DIR}_run/supervisord-conf"
CONF="$SV_INCLUDE_DIR/cwb.conf"
CTL=("$SV_DIR/bin/supervisord" -c "$SV_MAIN_CONF")
INSTALL="$ROOT/deploy/install-autostart.sh"
INTERVAL=20

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "看门狗启动 (pid=$$)，每 ${INTERVAL}s 巡检 cwb.conf + cwb-tunnel"

while true; do
    if [[ ! -f "$CONF" ]]; then
        log "cwb.conf 被冲掉（tmpfs 重建），执行恢复..."
        bash "$INSTALL" >>/tmp/cwb-autorestore.log 2>&1
        log "已重跑 install-autostart.sh"
    elif ! "${CTL[@]}" ctl status 2>/dev/null | sed -r 's/\x1b\[[0-9;]*m//g' | grep -q "^cwb-tunnel"; then
        log "cwb.conf 在但 cwb-tunnel 不在 supervisord 里，执行恢复..."
        bash "$INSTALL" >>/tmp/cwb-autorestore.log 2>&1
        log "已重跑 install-autostart.sh"
    fi
    sleep "$INTERVAL"
done
