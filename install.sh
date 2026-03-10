#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
#  WolfBet AutoBot — Ubuntu VPS Installer v2.0
#  Tested on Ubuntu 22.04 / 24.04
#  Sets up: venv, systemd service, management commands
# ─────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="$HOME/wolfbet-bot"
SERVICE_NAME="wolfbet"
SYSTEMD_DIR="$HOME/.config/systemd/user"
WOLFCTL="$HOME/.local/bin/wolfctl"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   WolfBet AutoBot — VPS Installer v2.0       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ─────────────────────────────
echo "[1/6] Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    sqlite3 tmux curl

# ── 2. Bot directory ───────────────────────────────────
echo "[2/6] Setting up bot directory…"
mkdir -p "$INSTALL_DIR"
cp wolfbet.py       "$INSTALL_DIR/"
cp requirements.txt "$INSTALL_DIR/"

# ── 3. Python virtual environment ─────────────────────
echo "[3/6] Creating Python virtual environment…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 4. Systemd user service (runs as your user, no root) ─
echo "[4/6] Setting up systemd service…"
mkdir -p "$SYSTEMD_DIR"

cat > "$SYSTEMD_DIR/${SERVICE_NAME}.service" << SERVICEEOF
[Unit]
Description=WolfBet AutoBot — Dice Auto-Betting Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/wolfbet.py --resume --daemon
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Graceful shutdown
KillSignal=SIGTERM
TimeoutStopSec=15

# Environment
Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SERVICEEOF

# Enable lingering so user services run after logout
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true

systemctl --user daemon-reload
echo "   Service installed: ${SERVICE_NAME}.service"

# ── 5. Management scripts ─────────────────────────────
echo "[5/6] Creating management commands…"
mkdir -p "$HOME/.local/bin"

# wolfctl — single command to manage everything
cat > "$WOLFCTL" << 'CTLEOF'
#!/usr/bin/env bash
# WolfBet AutoBot — Management Command
set -euo pipefail

INSTALL_DIR="$HOME/wolfbet-bot"
SERVICE="wolfbet"
PYTHON="$INSTALL_DIR/venv/bin/python3"
BOT="$INSTALL_DIR/wolfbet.py"

usage() {
    echo ""
    echo "  wolfctl — WolfBet AutoBot Manager"
    echo ""
    echo "  USAGE:"
    echo "    wolfctl setup       Run wizard to configure (API key, strategy, etc.)"
    echo "    wolfctl start       Start bot as background service"
    echo "    wolfctl stop        Stop the bot"
    echo "    wolfctl restart     Restart the bot"
    echo "    wolfctl status      Show bot status and session stats"
    echo "    wolfctl logs        Stream live logs"
    echo "    wolfctl logs-full   Show last 200 log lines"
    echo "    wolfctl interactive Run with live TUI dashboard (foreground)"
    echo "    wolfctl tmux        Run TUI in a tmux session (detachable)"
    echo "    wolfctl stats       Show all-time session statistics"
    echo "    wolfctl presets     List saved presets"
    echo "    wolfctl update      Update bot from current directory"
    echo ""
}

cmd_setup() {
    echo "Running setup wizard…"
    "$PYTHON" "$BOT"
    echo ""
    echo "Config saved. Start the bot with: wolfctl start"
}

cmd_start() {
    # check if config exists
    if [ ! -f "$HOME/.wolfbet_autobot.json" ]; then
        echo "No config found. Run 'wolfctl setup' first."
        exit 1
    fi
    systemctl --user start "$SERVICE"
    sleep 1
    if systemctl --user is-active --quiet "$SERVICE"; then
        echo "Bot started successfully."
        echo "  Check status:  wolfctl status"
        echo "  View logs:     wolfctl logs"
    else
        echo "Failed to start. Check logs: wolfctl logs-full"
        exit 1
    fi
}

cmd_stop() {
    systemctl --user stop "$SERVICE"
    echo "Bot stopped."
    "$PYTHON" "$BOT" --status 2>/dev/null || true
}

cmd_restart() {
    systemctl --user restart "$SERVICE"
    sleep 1
    echo "Bot restarted."
    "$PYTHON" "$BOT" --status 2>/dev/null || true
}

cmd_status() {
    echo ""
    echo "── Service Status ──"
    systemctl --user status "$SERVICE" --no-pager 2>/dev/null || echo "  Service not running"
    echo ""
    echo "── Session Status ──"
    "$PYTHON" "$BOT" --status 2>/dev/null || echo "  No active session"
}

cmd_logs() {
    journalctl --user -u "$SERVICE" -f --no-pager
}

cmd_logs_full() {
    journalctl --user -u "$SERVICE" -n 200 --no-pager
}

cmd_interactive() {
    if [ ! -f "$HOME/.wolfbet_autobot.json" ]; then
        echo "No config found. Running setup wizard…"
        "$PYTHON" "$BOT"
    else
        "$PYTHON" "$BOT" --resume
    fi
}

cmd_tmux() {
    SESSION="wolfbet"
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "tmux session '$SESSION' already running."
        echo "  Attach:  tmux attach -t $SESSION"
    else
        if [ -f "$HOME/.wolfbet_autobot.json" ]; then
            tmux new-session -d -s "$SESSION" "$PYTHON $BOT --resume"
        else
            tmux new-session -d -s "$SESSION" "$PYTHON $BOT"
        fi
        echo "Started in tmux session '$SESSION'"
        echo "  Attach:  tmux attach -t $SESSION"
        echo "  Detach:  Ctrl+B then D"
    fi
}

cmd_stats() {
    "$PYTHON" "$BOT" --stats
}

cmd_presets() {
    "$PYTHON" "$BOT" --list-presets
}

cmd_update() {
    if [ -f "wolfbet.py" ]; then
        cp wolfbet.py "$INSTALL_DIR/wolfbet.py"
        echo "Bot updated. Restart with: wolfctl restart"
    else
        echo "No wolfbet.py in current directory."
        exit 1
    fi
}

# ── Dispatch ──────────────────────────────────────────
case "${1:-}" in
    setup)       cmd_setup ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_restart ;;
    status)      cmd_status ;;
    logs)        cmd_logs ;;
    logs-full)   cmd_logs_full ;;
    interactive) cmd_interactive ;;
    tmux)        cmd_tmux ;;
    stats)       cmd_stats ;;
    presets)     cmd_presets ;;
    update)      cmd_update ;;
    *)           usage ;;
esac
CTLEOF
chmod +x "$WOLFCTL"

# ── 6. Quick-run scripts (kept for compatibility) ─────
echo "[6/6] Creating helper scripts…"

cat > "$INSTALL_DIR/run.sh" << 'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/venv/bin/python3" "$DIR/wolfbet.py" "$@"
EOF
chmod +x "$INSTALL_DIR/run.sh"

# ── Done ──────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════"
echo "  Installation complete!"
echo "════════════════════════════════════════════════════"
echo ""
echo "  FIRST TIME SETUP:"
echo "    wolfctl setup          Configure API key, strategy, etc."
echo ""
echo "  DAILY USE:"
echo "    wolfctl start          Start bot in background"
echo "    wolfctl stop           Stop the bot"
echo "    wolfctl status         Check session status"
echo "    wolfctl logs           Stream live logs"
echo "    wolfctl stats          View all-time statistics"
echo ""
echo "  INTERACTIVE (with live dashboard):"
echo "    wolfctl interactive    Run in foreground with TUI"
echo "    wolfctl tmux           Run TUI in detachable tmux session"
echo ""
echo "  The bot runs as a systemd user service — it will:"
echo "    - Auto-restart on crashes (after 10s)"
echo "    - Keep running after you log out (lingering enabled)"
echo "    - Log to journalctl (wolfctl logs)"
echo ""
echo "  Files:"
echo "    Bot:      $INSTALL_DIR/"
echo "    Config:   ~/.wolfbet_autobot.json"
echo "    Database: ~/.wolfbet_autobot.db"
echo "    Logs:     ~/.wolfbet_logs/wolfbet.log"
echo "    Service:  $SYSTEMD_DIR/${SERVICE_NAME}.service"
echo ""
