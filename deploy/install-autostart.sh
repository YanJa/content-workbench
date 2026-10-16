#!/usr/bin/env bash
#
# 把内容运营工作台挂到平台 supervisord 上，让 backend/frontend/tunnel 由它托管
# （进程挂了自动拉起，不用人工介入）。
#
# ⚠️  关于「休眠唤醒后是否还在」，先说清楚实测结论：
#   1. 平台 supervisord 的 include 目录是 ${IDE_EDITOR_SERVER_DIR}_run/supervisord-conf，
#      它是 **tmpfs（10MB）**，容器每次启动都会被重建 —— 写在里面的配置重启即丢失。
#   2. 想持久化，就得让启动时把配置重新拷进 tmpfs。干这活的是
#      ${IDE_EDITOR_SERVER_DIR}/workspace-init/init（里面硬编码只拷 vs-code.conf）。
#   3. 但 /.PlnPyKFp4CRfFtgC1 是 **只读挂载**（ext4 ro），改不了 init，也写不进
#      supervisord-conf 目录 —— 所以「真·开机自启」在当前沙箱里做不到。
#
#   结论：本脚本做到的是「本次容器生命周期内由 supervisord 托管」。
#   沙箱休眠重启后，**重新执行一次本脚本**即可恢复（幂等，3 秒完成）。
#
# 用法：bash deploy/install-autostart.sh
# 卸载：bash deploy/install-autostart.sh --uninstall

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_SRC="$ROOT/deploy/cwb.conf"

SV_DIR="${IDE_EDITOR_SERVER_DIR:-/.PlnPyKFp4CRfFtgC1}"
SV_MAIN_CONF="$SV_DIR/supervisord-conf/supervisord.conf"
SV_INCLUDE_DIR="${SV_DIR}_run/supervisord-conf"
CTL=("$SV_DIR/bin/supervisord" -c "$SV_MAIN_CONF")

# ctl status 输出带 ANSI 颜色码（管道模式下也有），剥掉才能用 ^ 锚定匹配
sv_status() { "${CTL[@]}" ctl status 2>/dev/null | sed -r 's/\x1b\[[0-9;]*m//g'; }

log() { echo "[autostart] $*"; }

# ---------- 卸载 ----------
if [[ "${1:-}" == "--uninstall" ]]; then
    log "停止并移除 cwb-* 服务"
    "${CTL[@]}" ctl stop cwb-tunnel cwb-frontend cwb-backend cwb-proxy >/dev/null 2>&1 || true
    rm -f "$SV_INCLUDE_DIR/cwb.conf"
    "${CTL[@]}" ctl reload >/dev/null 2>&1 || true
    log "已卸载"
    exit 0
fi

# ---------- 前置检查 ----------
[[ -f "$CONF_SRC" ]] || { echo "找不到 $CONF_SRC" >&2; exit 1; }
[[ -f "$SV_MAIN_CONF" ]] || { echo "找不到平台 supervisord 主配置" >&2; exit 1; }
[[ -x "$SV_DIR/bin/supervisord" ]] || { echo "找不到 supervisord" >&2; exit 1; }

# ---------- 安装配置到 include 目录（tmpfs，可写） ----------
mkdir -p "$SV_INCLUDE_DIR"
cp -f "$CONF_SRC" "$SV_INCLUDE_DIR/cwb.conf"
log "已写入 $SV_INCLUDE_DIR/cwb.conf"

# ---------- 让 supervisord 重新加载 ----------
"${CTL[@]}" ctl reload
log "supervisord 已 reload"

# ---------- 等几秒看结果 ----------
sleep 6
echo
echo "============================================================"
sv_status | grep -E "^cwb-" || echo "  cwb-* 尚未出现在列表里，手动起一下："
if ! sv_status | grep -qE "^cwb-backend"; then
    "${CTL[@]}" ctl start cwb-proxy cwb-backend cwb-frontend cwb-tunnel 2>&1 | head -5
    sleep 5
    sv_status | grep -E "^cwb-"
fi
echo
echo "  查看状态："
echo "    $SV_DIR/bin/supervisord -c $SV_MAIN_CONF ctl status"
echo
echo "  ⚠️ 沙箱休眠重启后需重新执行：bash $ROOT/deploy/install-autostart.sh"
echo "============================================================"
