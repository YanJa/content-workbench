#!/usr/bin/env bash
#
# keepalive.sh —— 沙箱防休眠保活（每 5 分钟模拟浏览器访问）
#
# 背景：cloudstudio 系沙箱在无活动一段时间后会休眠/回收，服务被暂停。
#   实测几次休眠间隔仅 3~5 分钟。本脚本周期性发起「类浏览器」请求，
#   让平台侧持续看到本沙箱有访问活动，尽量推迟/避免休眠。
#
# 三类目标地址（防休眠的「有效信号」强弱不同）：
#   1. 平台域名（egress 出网）：从沙箱请求平台网关，产生关联本沙箱的流量。
#      —— 这是「有对外访问」最直接的一档，平台网关收到请求即刷新活跃。
#   2. 用户预览 URL：你访问应用的真实链接（形如 *.sandbox.cloudstudio.club/...）。
#      —— 走平台预览代理回路，是防休眠最强的信号。写在 keepalive.targets 里。
#   3. 本地服务：http://127.0.0.1:5173 / :8000/health。
#      —— 只能保服务不崩（localhost 流量平台网关看不到），顺带做健康自检。
#
# 调参：
#   KEEPALIVE_INTERVAL  间隔秒数（默认 300 = 5 分钟，可调小到 120 更激进）
#
# 用法（已由 cwb.conf 托管，不用手动起）：
#   setsid bash deploy/keepalive.sh >/tmp/cwb-keepalive.log 2>&1 &
#
# 注意：本脚本只「尽量」防休眠，平台若按「编辑器/预览客户端长连接」判定，
#   纯请求可能仍会休眠；那种情况请把真实预览 URL 填进 keepalive.targets。

set -u

# 间隔（秒）：用户要求 5 分钟；若仍频繁休眠可降到 120~180
INTERVAL="${KEEPALIVE_INTERVAL:-300}"

# 完整浏览器 UA + 常见 Accept 头，让请求尽量像真实浏览器（而非脚本 ping）
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
ACCEPT_LANG="zh-CN,zh;q=0.9,en;q=0.8"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGETS_FILE="$ROOT/deploy/keepalive.targets"

# 默认目标：本地健康自检 + 平台 egress（出网刷新活跃）
DEFAULT_TARGETS=(
  "http://127.0.0.1:5173/"
  "http://127.0.0.1:8000/health"
  "https://example.com"
  "https://codingcorp.cloudstudio.net/api"
  "https://preview.cloudstudio.work"
)

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# 收集目标：默认 + targets 文件里用户自定义的（支持 # 注释与空行）
collect_targets() {
  local -a all=("${DEFAULT_TARGETS[@]}")
  if [[ -f "$TARGETS_FILE" ]]; then
    while IFS= read -r line; do
      # 去掉行内注释与首尾空白
      line="${line%%#*}"
      line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      [[ -n "$line" ]] && all+=("$line")
    done < "$TARGETS_FILE"
  fi
  printf '%s\n' "${all[@]}"
}

log "保活守护启动 (pid=$$)，每 ${INTERVAL}s 一轮，UA 模拟 Chrome"

while true; do
  while IFS= read -r u; do
    [[ -z "$u" ]] && continue
    code=$(curl -s -m 10 -A "$UA" \
      -H "Accept: $ACCEPT" \
      -H "Accept-Language: $ACCEPT_LANG" \
      -o /dev/null -w '%{http_code}' "$u" 2>/dev/null) || code="ERR"
    log "hit ${u} -> ${code}"
  done < <(collect_targets)
  sleep "$INTERVAL"
done
