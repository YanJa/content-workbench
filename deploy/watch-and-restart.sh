#!/bin/bash
# 等某个渲染任务跑完后，自动重启后端。
#
# 用途：后端正在跑长渲染（复刻音色可能要 1.5 小时），但你改了后端代码
# （比如新的 /progress 轻量轮询接口），不想为了重启而打断渲染。
# 这个脚本盯着任务状态，一变成 done/failed 就重启，顺便验证新接口上线。
#
# 用法：
#   bash deploy/watch-and-restart.sh <job_id>
#
# 建议配合 nohup 或 supervisord 后台跑：
#   nohup bash deploy/watch-and-restart.sh 15d5fd5d... > /tmp/watch.log 2>&1 &

set -u

JOB_ID="${1:-}"
if [ -z "$JOB_ID" ]; then
  echo "用法: bash deploy/watch-and-restart.sh <job_id>"
  exit 1
fi

API="http://127.0.0.1:8000/api/video/jobs/${JOB_ID}"
INTERVAL="${WATCH_INTERVAL:-30}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "开始盯任务 $JOB_ID（每 ${INTERVAL}s 查一次）"

while true; do
  STATUS=$(curl -s -m 10 "$API" 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")

  if [ -z "$STATUS" ]; then
    log "查询失败（后端可能在重启），10s 后重试"
    sleep 10
    continue
  fi

  if [ "$STATUS" = "done" ] || [ "$STATUS" = "failed" ]; then
    log "任务已结束：status=$STATUS"
    break
  fi

  SEGS=$(ls /workspace/content-workbench-pro/backend/data/outputs/work_"${JOB_ID}"/clone_seg_*.wav 2>/dev/null | wc -l)
  log "status=$STATUS 已合成 ${SEGS} 段，继续等"
  sleep "$INTERVAL"
done

log "重启后端 cwb-backend ..."
# 项目由平台 supervisord 托管，控制入口不是裸 supervisorctl，
# 而是 $SV_DIR/bin/supervisord ctl（见 deploy/install-autostart.sh 第 28 行）
SV_DIR="${IDE_EDITOR_SERVER_DIR:-/.PlnPyKFp4CRfFtgC1}"
CTL=("$SV_DIR/bin/supervisord" -c "$SV_DIR/supervisord-conf/supervisord.conf")

if "${CTL[@]}" ctl restart cwb-backend >/dev/null 2>&1; then
  log "supervisord 重启指令已下发"
else
  log "supervisord 指令失败，回退到直接 kill（autorestart 会拉起来）"
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
fi

# 等新进程起来
for i in $(seq 1 30); do
  CODE=$(curl -s -m 3 -o /dev/null -w "%{http_code}" \
    "http://127.0.0.1:8000/api/video/jobs/${JOB_ID}/progress" 2>/dev/null || echo "000")
  if [ "$CODE" = "200" ]; then
    log "✅ 后端已重启，新接口 /progress 返回 200"
    log "可以重新渲染了（页面上点「重试」）"
    exit 0
  fi
  sleep 2
done

log "⚠️ 重启后 60s 内 /progress 仍未就绪，请手动检查"
exit 1
