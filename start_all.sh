#!/bin/bash
# Build the complete tmux layout before launching workers. This prevents the
# visibility controller from racing with missing restricted-account windows.

set -euo pipefail

SESSION_NAME="xiuxian"
DEPLOY_DIR="$HOME/deploy"
VENV_PATH="$DEPLOY_DIR/venv/bin/activate"

if [ -f "$DEPLOY_DIR/.env" ]; then
    set -a
    # Runtime credentials stay outside Git and are inherited by tmux workers.
    source "$DEPLOY_DIR/.env"
    set +a
fi

create_placeholder_window() {
    local index="$1"
    local name="$2"
    tmux new-window -d -t "$SESSION_NAME:$index" -n "$name" "exec sleep infinity"
    tmux set-window-option -t "$SESSION_NAME:$index" remain-on-exit on
}

launch_window() {
    local index="$1"
    local script="$2"
    shift 2
    local launch="cd \"$DEPLOY_DIR\" && source \"$VENV_PATH\" && exec python3 \"$script\""
    local argument
    for argument in "$@"; do
        printf -v launch '%s %q' "$launch" "$argument"
    done
    tmux respawn-window -k -t "$SESSION_NAME:$index" "$launch"
}

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Stopping old tmux session $SESSION_NAME..."
    # Killing the final session can make the tmux server return nonzero.
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
fi

# Keep a placeholder alive while the tmux server and all six windows are built.
if ! tmux new-session -d -s "$SESSION_NAME" -n "Main" "exec sleep infinity"; then
    sleep 1
    tmux new-session -d -s "$SESSION_NAME" -n "Main" "exec sleep infinity"
fi
tmux set-window-option -g remain-on-exit on
tmux set-window-option -t "$SESSION_NAME:0" remain-on-exit on
create_placeholder_window 1 "Sub"
create_placeholder_window 2 "Xiaohao"
create_placeholder_window 3 "Waaiging"
create_placeholder_window 4 "Dashboard"
create_placeholder_window 5 "BossVerify"

# Start managed windows first and the main visibility controller last.
echo "[1/6] Starting the shared World Boss browser verifier..."
launch_window 5 "world_boss_browser.py" "--no-sandbox"

echo "[2/6] Starting Sub..."
launch_window 1 "sub_cultivator.py"

echo "[3/6] Starting Xiaohao..."
launch_window 2 "cultivator_xiaohao.py"

echo "[4/6] Starting Waaiging..."
launch_window 3 "cultivator_waaiging.py"

echo "[5/6] Starting Dashboard..."
launch_window 4 "dashboard_server.py"

echo "[6/6] Starting Main..."
launch_window 0 "intelligent_cultivator.py"

PUBLIC_IP="$(curl -fsS --max-time 5 ifconfig.me 2>/dev/null || true)"
echo "======================================"
echo "All tmux workers started."
if [ -n "$PUBLIC_IP" ]; then
    echo "Dashboard: http://$PUBLIC_IP:8000"
fi
echo "======================================"
