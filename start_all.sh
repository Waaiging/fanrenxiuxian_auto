#!/bin/bash
# 凡人修仙 AWS 启动管理脚本
# 先建立稳定的 tmux 结构，再启动进程，避免可见性控制器遇到缺失窗口。

set -euo pipefail

SESSION_NAME="xiuxian"
DEPLOY_DIR="$HOME/deploy"
VENV_PATH="$DEPLOY_DIR/venv/bin/activate"

create_placeholder_window() {
    local index="$1"
    local name="$2"
    tmux new-window -d -t "$SESSION_NAME:$index" -n "$name" "exec sleep infinity"
    tmux set-window-option -t "$SESSION_NAME:$index" remain-on-exit on
}

launch_window() {
    local index="$1"
    local script="$2"
    local launch="cd \"$DEPLOY_DIR\" && source \"$VENV_PATH\" && exec python3 \"$script\""
    tmux respawn-window -k -t "$SESSION_NAME:$index" "$launch"
}

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "正在清理旧会话 $SESSION_NAME..."
    tmux kill-session -t "$SESSION_NAME"
fi

# 窗口 0 先以占位进程建立，使 tmux server 不会因业务脚本瞬时退出而消失。
tmux new-session -d -s "$SESSION_NAME" -n "Main" "exec sleep infinity"
tmux set-window-option -g remain-on-exit on
tmux set-window-option -t "$SESSION_NAME:0" remain-on-exit on
create_placeholder_window 1 "Sub"
create_placeholder_window 2 "Xiaohao"
create_placeholder_window 3 "Waaiging"
create_placeholder_window 4 "Dashboard"

# 先启动被管理窗口，最后启动带可见性控制器的主号。
echo "[1/5] 启动 星宫 (Sub)..."
launch_window 1 "sub_cultivator.py"

echo "[2/5] 启动 万灵宗 (Xiaohao)..."
launch_window 2 "cultivator_xiaohao.py"

echo "[3/5] 启动 天星宗 (Waaiging)..."
launch_window 3 "cultivator_waaiging.py"

echo "[4/5] 启动 云端监控台 (Dashboard)..."
launch_window 4 "dashboard_server.py"

echo "[5/5] 启动 万灵宗 (Main)..."
launch_window 0 "intelligent_cultivator.py"

PUBLIC_IP="$(curl -fsS --max-time 5 ifconfig.me 2>/dev/null || true)"
echo "======================================"
echo "修仙大阵已重组完毕。"
if [ -n "$PUBLIC_IP" ]; then
    echo "监控地址: http://$PUBLIC_IP:8000"
fi
echo "======================================"
