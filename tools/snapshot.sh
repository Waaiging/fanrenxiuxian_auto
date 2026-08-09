#!/bin/bash
# ============================================================
# 状态文件自动快照脚本 (修仙脚本)
# cron 每 5 分钟调用一次：md5 变化检测 + 节流，保留最近 KEEP 份
# 状态污染后可用 rollback.sh 一键回滚
# ============================================================
set -euo pipefail

DEPLOY_DIR="${HOME}/deploy"
BACKUP_DIR="${DEPLOY_DIR}/backup/state_snapshots"
STATE_FILES=(state_main.json state_sub.json state_waaiging.json state_xiaohao.json)
KEEP=100            # 每个状态文件保留的快照份数（5分钟粒度 ≈ 8小时回滚窗口）
MIN_INTERVAL=600    # 同一文件两次快照最小间隔（秒）= 10 分钟
LOG_FILE="${DEPLOY_DIR}/logs/snapshot.log"

mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"
cd "$DEPLOY_DIR"

now=$(date +%s)
changed=0

for f in "${STATE_FILES[@]}"; do
    [ -f "$f" ] || continue
    md5=$(md5sum "$f" | awk '{print $1}')
    record="$BACKUP_DIR/${f}.md5"
    last_md5=""
    last_ts=0
    if [ -f "$record" ]; then
        read -r last_md5 last_ts < "$record" || true
    fi
    if [ "$md5" != "$last_md5" ] && [ $((now - last_ts)) -ge "$MIN_INTERVAL" ]; then
        ts=$(date +%Y%m%d_%H%M%S)
        cp -p "$f" "$BACKUP_DIR/${f}.${ts}"
        echo "$md5 $now" > "$record"
        echo "[$(date '+%m-%d %H:%M:%S')] snapshot: ${f} -> ${f}.${ts} ($(du -h "$BACKUP_DIR/${f}.${ts}" | cut -f1))" | tee -a "$LOG_FILE"
        changed=1
    fi
done

# 清理：每个状态文件保留最近 KEEP 份（排除 .md5 记录文件）
if [ "$changed" = "1" ]; then
    for f in "${STATE_FILES[@]}"; do
        ls -1t "$BACKUP_DIR"/"${f}".* 2>/dev/null | grep -v '\.md5$' | tail -n +$((KEEP+1)) | xargs -r rm -f
    done
fi
