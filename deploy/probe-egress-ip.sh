#!/usr/bin/env bash
# 探测本机出网公网 IP（动态出口池场景下收集全部观测到的 IP）
# 用法: bash probe-egress-ip.sh [次数] [间隔秒]
# 默认 60 次、每次间隔 2 秒。
# 说明：本沙箱出网走轮换 IP 池，单次查询只能拿到当次出口，
#       需多次采样去重才能把观测到的全部出口 IP 列全。

set -u

N="${1:-60}"
INTERVAL="${2:-2}"

# 多个探测源，逐个尝试，取第一个成功返回的（避免单源被墙/超时）
SOURCES=(
  "https://ifconfig.me"
  "https://icanhazip.com"
  "https://ipinfo.io/ip"
  "https://api64.ipify.org"
  "https://api.ipify.org"
  "https://ip.3322.net"
  "https://myip.ipip.net"
)

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

probe_once() {
  local u ip
  for u in "${SOURCES[@]}"; do
    ip="$(curl -s --max-time 8 "$u" 2>/dev/null | tr -d '[:space:]')"
    # 只接受纯 IPv4
    if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      echo "$ip"
      return 0
    fi
  done
  return 1
}

echo "开始探测出网 IP：共 ${N} 次，间隔 ${INTERVAL}s"
echo "------------------------------------------------------------"

ok=0
for ((i=1; i<=N; i++)); do
  ip="$(probe_once)"
  if [[ -n "$ip" ]]; then
    echo "$ip" >> "$TMP"
    printf "[%3d/%3d] %s\n" "$i" "$N" "$ip"
    ok=$((ok+1))
  else
    printf "[%3d/%3d] (本次探测失败)\n" "$i" "$N"
  fi
  [[ $i -lt $N ]] && sleep "$INTERVAL"
done

echo "------------------------------------------------------------"
echo "成功 $ok / $N 次"
echo
echo "=== 观测到的全部出口 IP（去重，按出现次数降序）==="
sort "$TMP" | uniq -c | sort -rn | awk '{printf "%6d 次  %s\n", $1, $2}'
echo
echo "=== 去重后的 IP 列表（每行一个，便于粘贴）==="
sort -u "$TMP"
echo
echo "=== 汇总为 /24 网段（便于按段加白名单）==="
sort -u "$TMP" | awk -F. '{print $1"."$2"."$3".0/24"}' | sort -u
