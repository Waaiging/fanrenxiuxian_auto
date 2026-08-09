#!/bin/bash
# ============================================================
# 状态文件一键回滚脚本 (修仙脚本)
# 用法:
#   rollback.sh                          # 列出所有可回滚的状态文件
#   rollback.sh state_main.json          # 列出该文件的可用快照
#   rollback.sh state_main.json 20260803_205512   # 回滚到指定快照
#
# 回滚前自动把当前(可能已污染)状态备份到 backup/rollback/，可追溯
# 注意: 回滚文件后，运行中的脚本内存里仍是旧状态，需重启对应 tmux 窗口生效
# ============================================================
set -euo pipefail

DEPLOY_DIR="${HOME}/deploy"
BACKUP_DIR="${DEPLOY_DIR}/backup/state_snapshots"
ROLLBACK_DIR="${DEPLOY_DIR}/backup/rollback"

# 状态文件 -> tmux 窗口映射（用于回滚后提示）
declare -A WINDOW_MAP=(
    [state_main.json]="0 (凌霄宫主魂 intelligent_cultivator.py)"
    [state_sub.json]="1 (星宫化身 sub_cultivator.py)"
    [state_xiaohao.json]="2 (万灵宗 cultivator_xiaohao.py)"
    [state_waaiging.json]="3 (Waaiging cultivator_waaiging.py)"
)

file="${1:-}"
[ -n "$file" ] || {
    echo "可用状态文件:"
    for f in "${!WINDOW_MAP[@]}"; do echo "  $f"; done
    echo "用法: rollback.sh <状态文件> [快照时间戳]"
    exit 0
}

# 安全校验：只允许白名单内的状态文件
[ -n "${WINDOW_MAP[$file]:-}" ] || { echo "✗ 非法文件: $file（不在状态文件白名单内）"; exit 1; }

mapfile -t snapshots < <(ls -1t "$BACKUP_DIR"/"${file}".* 2>/dev/null | grep -v '\.md5$' || true)
if [ ${#snapshots[@]} -eq 0 ]; then
    echo "✗ 无可用快照（snapshot.sh 还没生成过快照）"; exit 1
fi

ts="${2:-}"
if [ -z "$ts" ]; then
    echo "可用快照（最新在前）:"
    for s in "${snapshots[@]}"; do
        echo "  $(basename "$s")  ($(stat -c '%y' "$s" | cut -d. -f1))"
    done
    echo "用法: rollback.sh $file <时间戳>"
    exit 1
fi

target="$BACKUP_DIR/${file}.${ts}"
[ -f "$target" ] || { echo "✗ 快照不存在: $target"; exit 1; }

# 回滚前备份当前状态（保留污染现场，可追溯）
mkdir -p "$ROLLBACK_DIR"
now=$(date +%Y%m%d_%H%M%S)
if [ -f "$DEPLOY_DIR/$file" ]; then
    cp -p "$DEPLOY_DIR/$file" "$ROLLBACK_DIR/${file}.${now}"
    echo "[rollback] 当前状态已备份: backup/rollback/${file}.${now}"
fi

cp -p "$target" "$DEPLOY_DIR/$file"
echo "[rollback] ✅ $file 已回滚到快照 ${ts}"

echo ""
echo "⚠️  运行中的脚本内存里仍是旧状态，重启对应窗口后才生效:"
echo "    tmux respawn-window -k -t xiuxian:${WINDOW_MAP[$file]%% *}"
