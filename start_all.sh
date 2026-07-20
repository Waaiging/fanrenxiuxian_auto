#!/bin/bash
# 凡人修仙 AWS 启动管理脚本 (V3 - 全量优化版)
# 使用 tmux 管理五个独立窗口

SESSION_NAME="xiuxian"
DEPLOY_DIR="$HOME/deploy"
VENV_PATH="$DEPLOY_DIR/venv/bin/activate"

run_window() {
    local index="$1"
    local name="$2"
    local script="$3"
    local launch="cd \"$DEPLOY_DIR\" && source \"$VENV_PATH\" && exec python3 \"$script\""
    if [ "$index" = "0" ]; then
        tmux new-session -d -s "$SESSION_NAME" -n "$name" "$launch"
        tmux setw -g remain-on-exit on
        tmux setw -t "$SESSION_NAME:$index" remain-on-exit on
    else
        tmux new-window -t "$SESSION_NAME:$index" -n "$name" "$launch"
        tmux setw -t "$SESSION_NAME:$index" remain-on-exit on
    fi
}

# 检查并清理旧会话
tmux has-session -t "$SESSION_NAME" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "⚠️ 正在清理旧会话 $SESSION_NAME..."
    tmux kill-session -t "$SESSION_NAME"
fi

# 1. 创建新会话并启动主脚本
echo "[1/5] 启动 万灵宗 (Main)..."
run_window 0 "Main" "intelligent_cultivator.py"

# 2. 创建其他窗口
echo "[2/5] 启动 星宫 (Sub)..."
run_window 1 "Sub" "sub_cultivator.py"

echo "[3/5] 启动 万灵宗 (Xiaohao)..."
run_window 2 "Xiaohao" "cultivator_xiaohao.py"

echo "[4/5] 启动 天星宗 (Waaiging)..."
run_window 3 "Waaiging" "cultivator_waaiging.py"

echo "[5/5] 启动 云端监控台 (Dashboard)..."
run_window 4 "Dashboard" "dashboard_server.py"

echo "======================================"
echo "✅ 修仙大阵已重组完毕！"
echo "👉 监控地址: http://$(curl -s ifconfig.me):8000"
echo "======================================"
