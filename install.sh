#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
#  WolfBet AutoBot — Server Installer v3.1
#  Sets up: CLI bot + Telegram bot, venv, systemd, wolfctl
# ─────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="$HOME/wolfbet-bot"
SERVICE_NAME="wolfbet"
TG_SERVICE_NAME="wolfbet-tg"
SYSTEMD_DIR="$HOME/.config/systemd/user"
WOLFCTL="$HOME/.local/bin/wolfctl"
ENV_FILE="$INSTALL_DIR/.env"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   WolfBet AutoBot — Server Installer v3.1         ║"
echo "║   CLI Bot + Telegram Bot                          ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ─────────────────────────────
echo "[1/7] Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    sqlite3 tmux curl

# ── 2. Bot directory ───────────────────────────────────
echo "[2/7] Setting up bot directory…"
mkdir -p "$INSTALL_DIR"
cp wolfbet.py       "$INSTALL_DIR/"
cp requirements.txt "$INSTALL_DIR/"

# Copy core/ shared package
mkdir -p "$INSTALL_DIR/core"
cp core/__init__.py  "$INSTALL_DIR/core/"
cp core/strategy.py  "$INSTALL_DIR/core/"
cp core/database.py  "$INSTALL_DIR/core/"
cp core/engine.py    "$INSTALL_DIR/core/"

# Copy tg/ Telegram bot package
mkdir -p "$INSTALL_DIR/tg"
cp tg/__init__.py   "$INSTALL_DIR/tg/"
cp tg/config.py     "$INSTALL_DIR/tg/"
cp tg/database.py   "$INSTALL_DIR/tg/"
cp tg/engine.py     "$INSTALL_DIR/tg/"
cp tg/strategy.py   "$INSTALL_DIR/tg/"
cp tg/formatter.py  "$INSTALL_DIR/tg/"
cp tg/handlers.py   "$INSTALL_DIR/tg/"
cp tg/bot.py        "$INSTALL_DIR/tg/"

# ── 3. Environment file ───────────────────────────────
echo "[3/7] Setting up environment…"
if [ ! -f "$ENV_FILE" ]; then
    if [ -f ".env" ]; then
        cp .env "$ENV_FILE"
        echo "   Copied .env to $ENV_FILE"
    elif [ -f ".env.example" ]; then
        cp .env.example "$ENV_FILE"
        echo "   Created $ENV_FILE from .env.example"
        echo "   ⚠ EDIT THIS FILE: nano $ENV_FILE"
    else
        cat > "$ENV_FILE" << 'ENVEOF'
# WolfBet AutoBot — Environment
# Get your Telegram bot token from @BotFather
WOLFBET_TG_TOKEN=
WOLFBET_TG_DATA=~/.wolfbot_tg
ENVEOF
        echo "   Created $ENV_FILE"
        echo "   ⚠ EDIT THIS FILE: nano $ENV_FILE"
    fi
else
    echo "   $ENV_FILE already exists (kept)"
fi

# ── 4. Python virtual environment ─────────────────────
echo "[4/7] Creating Python virtual environment…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 5. Systemd services ───────────────────────────────
echo "[5/7] Setting up systemd services…"
mkdir -p "$SYSTEMD_DIR"

# CLI bot service
cat > "$SYSTEMD_DIR/${SERVICE_NAME}.service" << SERVICEEOF
[Unit]
Description=WolfBet AutoBot — Dice Auto-Betting Engine (CLI)
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

KillSignal=SIGTERM
TimeoutStopSec=15

Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-${ENV_FILE}

[Install]
WantedBy=default.target
SERVICEEOF

# Telegram bot service
cat > "$SYSTEMD_DIR/${TG_SERVICE_NAME}.service" << TGSERVICEEOF
[Unit]
Description=WolfBet AutoBot — Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 -m tg.bot
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

KillSignal=SIGTERM
TimeoutStopSec=15

Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=${ENV_FILE}

[Install]
WantedBy=default.target
TGSERVICEEOF

# Enable lingering so user services run after logout
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true

systemctl --user daemon-reload
echo "   Services installed:"
echo "     ${SERVICE_NAME}.service     (CLI bot)"
echo "     ${TG_SERVICE_NAME}.service  (Telegram bot)"

# ── 6. Management CLI ─────────────────────────────────
echo "[6/7] Creating wolfctl management command…"
mkdir -p "$HOME/.local/bin"

cat > "$WOLFCTL" << 'CTLEOF'
#!/usr/bin/env bash
# WolfBet AutoBot — Unified Management Command v3.1
set -euo pipefail

INSTALL_DIR="$HOME/wolfbet-bot"
SERVICE="wolfbet"
TG_SERVICE="wolfbet-tg"
PYTHON="$INSTALL_DIR/venv/bin/python3"
BOT="$INSTALL_DIR/wolfbet.py"
ENV_FILE="$INSTALL_DIR/.env"

usage() {
    echo ""
    echo "  wolfctl — WolfBet AutoBot Manager v3.1"
    echo ""
    echo "  CLI BOT:"
    echo "    wolfctl setup       Run wizard to configure"
    echo "    wolfctl start       Start CLI bot as daemon"
    echo "    wolfctl stop        Stop CLI bot"
    echo "    wolfctl restart     Restart CLI bot"
    echo "    wolfctl status      Show CLI bot status"
    echo "    wolfctl monitor     Attach live TUI to running daemon"
    echo "    wolfctl logs        Stream CLI bot logs"
    echo "    wolfctl logs-full   Show last 200 log lines"
    echo ""
    echo "  TELEGRAM BOT:"
    echo "    wolfctl tg start    Start Telegram bot"
    echo "    wolfctl tg stop     Stop Telegram bot"
    echo "    wolfctl tg restart  Restart Telegram bot"
    echo "    wolfctl tg status   Show Telegram bot status"
    echo "    wolfctl tg logs     Stream Telegram bot logs"
    echo "    wolfctl tg env      Edit .env file (token, config)"
    echo ""
    echo "  ALL SERVICES:"
    echo "    wolfctl start-all   Start both CLI + Telegram bots"
    echo "    wolfctl stop-all    Stop both bots"
    echo "    wolfctl status-all  Show status of all services"
    echo ""
    echo "  SHORTCUTS:"
    echo "    wolfctl interactive Start CLI daemon + attach monitor"
    echo "    wolfctl tmux        Monitor in detachable tmux session"
    echo ""
    echo "  DATA:"
    echo "    wolfctl stats       Show all-time session statistics"
    echo "    wolfctl session ID  Full stats + streak distribution"
    echo "    wolfctl presets     List saved presets"
    echo "    wolfctl update      Update bot from current directory"
    echo ""
}

# ── CLI bot commands ──────────────────────────────────
cmd_setup() {
    echo "Running setup wizard…"
    "$PYTHON" "$BOT" --setup-only
}

cmd_start() {
    if [ ! -f "$HOME/.wolfbet_autobot.json" ]; then
        echo "No config found. Run 'wolfctl setup' first."
        exit 1
    fi
    systemctl --user start "$SERVICE"
    sleep 1
    if systemctl --user is-active --quiet "$SERVICE"; then
        echo "CLI bot started."
        echo "  Check: wolfctl status"
        echo "  Logs:  wolfctl logs"
    else
        echo "Failed to start. Check: wolfctl logs-full"
        exit 1
    fi
}

cmd_stop() {
    systemctl --user stop "$SERVICE"
    echo "CLI bot stopped."
    "$PYTHON" "$BOT" --status 2>/dev/null || true
}

cmd_restart() {
    systemctl --user restart "$SERVICE"
    sleep 1
    echo "CLI bot restarted."
    "$PYTHON" "$BOT" --status 2>/dev/null || true
}

cmd_status() {
    echo ""
    echo "── CLI Bot Service ──"
    systemctl --user status "$SERVICE" --no-pager 2>/dev/null || echo "  Not running"
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

cmd_monitor() {
    "$PYTHON" "$BOT" --monitor
}

cmd_interactive() {
    if [ ! -f "$HOME/.wolfbet_autobot.json" ]; then
        echo "No config found. Run 'wolfctl setup' first."
        exit 1
    fi
    if ! systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
        systemctl --user start "$SERVICE"
        sleep 1
        if ! systemctl --user is-active --quiet "$SERVICE"; then
            echo "Failed to start daemon. Check: wolfctl logs-full"
            exit 1
        fi
        echo "Daemon started."
    fi
    "$PYTHON" "$BOT" --monitor
}

cmd_tmux() {
    SESSION="wolfbet"
    if ! systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
        if [ ! -f "$HOME/.wolfbet_autobot.json" ]; then
            echo "No config found. Run 'wolfctl setup' first."
            exit 1
        fi
        systemctl --user start "$SERVICE"
        sleep 1
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "tmux session '$SESSION' already running."
        echo "  Attach:  tmux attach -t $SESSION"
    else
        tmux new-session -d -s "$SESSION" "$PYTHON $BOT --monitor"
        echo "Started monitor in tmux session '$SESSION'"
        echo "  Attach:  tmux attach -t $SESSION"
        echo "  Detach:  Ctrl+B then D"
    fi
}

cmd_stats() {
    "$PYTHON" "$BOT" --stats
}

cmd_session() {
    local sid="${1:-}"
    if [ -z "$sid" ]; then
        echo "Usage: wolfctl session <ID>"
        exit 1
    fi
    "$PYTHON" "$BOT" --session-bets "$sid"
}

cmd_presets() {
    "$PYTHON" "$BOT" --list-presets
}

cmd_update() {
    # Pull latest from git if inside a repo
    if git rev-parse --is-inside-work-tree &>/dev/null; then
        echo "Pulling latest from git…"
        git pull --ff-only || { echo "Git pull failed. Resolve manually."; exit 1; }
    fi
    if [ ! -f "wolfbet.py" ]; then
        echo "No wolfbet.py in current directory."
        exit 1
    fi

    cp wolfbet.py         "$INSTALL_DIR/wolfbet.py"
    cp requirements.txt   "$INSTALL_DIR/" 2>/dev/null
    [ -f ".env.example" ] && cp .env.example "$INSTALL_DIR/"
    # Update deps if requirements changed
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" 2>/dev/null || true
    # Update TG bot files if present
    if [ -d "core" ] && [ -d "tg" ]; then
        mkdir -p "$INSTALL_DIR/core" "$INSTALL_DIR/tg"
        cp core/*.py "$INSTALL_DIR/core/"
        cp tg/*.py   "$INSTALL_DIR/tg/"
        echo "Bot + TG bot files updated."
        # Auto-restart TG bot gracefully (sessions auto-resume)
        if systemctl --user is-active --quiet "$TG_SERVICE" 2>/dev/null; then
            echo "Restarting Telegram bot (sessions will auto-resume)…"
            systemctl --user restart "$TG_SERVICE"
            sleep 2
            if systemctl --user is-active --quiet "$TG_SERVICE"; then
                echo "Telegram bot restarted. Active sessions resumed."
            else
                echo "WARNING: TG bot failed to restart. Check: wolfctl tg logs-full"
            fi
        fi
        # CLI bot still needs manual restart
        if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
            echo "CLI bot still running. Restart with: wolfctl restart"
        fi
    else
        echo "Bot updated. Restart with: wolfctl restart"
    fi
}

# ── Telegram bot commands ────────────────────────────
cmd_tg() {
    local action="${1:-status}"
    case "$action" in
        start)
            if [ -f "$ENV_FILE" ]; then
                TOKEN=$(grep -E "^WOLFBET_TG_TOKEN=" "$ENV_FILE" | cut -d= -f2- | tr -d ' ')
                if [ -z "$TOKEN" ]; then
                    echo "Telegram token not set. Edit: wolfctl tg env"
                    exit 1
                fi
            else
                echo "No .env file found. Run installer or create: $ENV_FILE"
                exit 1
            fi
            systemctl --user start "$TG_SERVICE"
            sleep 1
            if systemctl --user is-active --quiet "$TG_SERVICE"; then
                echo "Telegram bot started."
                echo "  Logs: wolfctl tg logs"
            else
                echo "Failed to start. Check: wolfctl tg logs"
                systemctl --user status "$TG_SERVICE" --no-pager 2>/dev/null || true
                exit 1
            fi
            ;;
        stop)
            systemctl --user stop "$TG_SERVICE"
            echo "Telegram bot stopped."
            ;;
        restart)
            systemctl --user restart "$TG_SERVICE"
            sleep 1
            if systemctl --user is-active --quiet "$TG_SERVICE"; then
                echo "Telegram bot restarted."
            else
                echo "Failed to restart. Check: wolfctl tg logs"
                exit 1
            fi
            ;;
        status)
            echo ""
            echo "── Telegram Bot Service ──"
            systemctl --user status "$TG_SERVICE" --no-pager 2>/dev/null || echo "  Not running"
            echo ""
            if [ -f "$ENV_FILE" ]; then
                TOKEN=$(grep -E "^WOLFBET_TG_TOKEN=" "$ENV_FILE" | cut -d= -f2- | tr -d ' ')
                if [ -n "$TOKEN" ]; then
                    echo "  Token: ${TOKEN:0:10}…(set)"
                else
                    echo "  Token: NOT SET — run: wolfctl tg env"
                fi
            else
                echo "  .env: NOT FOUND"
            fi
            ;;
        logs)
            journalctl --user -u "$TG_SERVICE" -f --no-pager
            ;;
        logs-full)
            journalctl --user -u "$TG_SERVICE" -n 200 --no-pager
            ;;
        env)
            if [ ! -f "$ENV_FILE" ]; then
                cat > "$ENV_FILE" << 'INNEREOF'
# WolfBet AutoBot — Environment
WOLFBET_TG_TOKEN=
WOLFBET_TG_DATA=~/.wolfbot_tg
INNEREOF
            fi
            "${EDITOR:-nano}" "$ENV_FILE"
            echo "Restart Telegram bot to apply: wolfctl tg restart"
            ;;
        *)
            echo "Usage: wolfctl tg [start|stop|restart|status|logs|logs-full|env]"
            ;;
    esac
}

# ── All-services commands ────────────────────────────
cmd_start_all() {
    echo "Starting all services…"
    cmd_start 2>/dev/null || true
    cmd_tg start 2>/dev/null || true
    echo ""
    cmd_status_all
}

cmd_stop_all() {
    echo "Stopping all services…"
    systemctl --user stop "$SERVICE" 2>/dev/null || true
    systemctl --user stop "$TG_SERVICE" 2>/dev/null || true
    echo "All services stopped."
}

cmd_status_all() {
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║   WolfBet AutoBot — Service Status        ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""

    if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
        echo "  CLI Bot:      ● RUNNING"
    else
        echo "  CLI Bot:      ○ stopped"
    fi

    if systemctl --user is-active --quiet "$TG_SERVICE" 2>/dev/null; then
        echo "  Telegram Bot: ● RUNNING"
    else
        echo "  Telegram Bot: ○ stopped"
    fi

    echo ""
}

# ── Dispatch ─────────────────────────────────────────
case "${1:-}" in
    setup)       cmd_setup ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_restart ;;
    status)      cmd_status ;;
    monitor)     cmd_monitor ;;
    logs)        cmd_logs ;;
    logs-full)   cmd_logs_full ;;
    interactive) cmd_interactive ;;
    tmux)        cmd_tmux ;;
    stats)       cmd_stats ;;
    session)     cmd_session "${2:-}" ;;
    presets)     cmd_presets ;;
    update)      cmd_update ;;
    tg)          cmd_tg "${2:-}" ;;
    start-all)   cmd_start_all ;;
    stop-all)    cmd_stop_all ;;
    status-all)  cmd_status_all ;;
    *)           usage ;;
esac
CTLEOF
chmod +x "$WOLFCTL"

# ── 7. Quick-run scripts ──────────────────────────────
echo "[7/7] Creating helper scripts…"

cat > "$INSTALL_DIR/run.sh" << 'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/venv/bin/python3" "$DIR/wolfbet.py" "$@"
EOF
chmod +x "$INSTALL_DIR/run.sh"

cat > "$INSTALL_DIR/run-tg.sh" << 'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; source "$DIR/.env" 2>/dev/null; set +a
cd "$DIR" && "$DIR/venv/bin/python3" -m tg.bot "$@"
EOF
chmod +x "$INSTALL_DIR/run-tg.sh"

# ── Done ──────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Installation complete!"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  PROJECT STRUCTURE:"
echo "    $INSTALL_DIR/"
echo "    ├── wolfbet.py          CLI bot (TUI dashboard)"
echo "    ├── core/               Shared: strategy, DB schema"
echo "    ├── tg/                 Telegram bot package"
echo "    ├── .env                Environment config (tokens)"
echo "    └── venv/               Python virtual environment"
echo ""
echo "  ── STEP 1: CLI BOT SETUP ──"
echo "    wolfctl setup           Configure API key, strategy"
echo "    wolfctl start           Start CLI bot daemon"
echo "    wolfctl monitor         Attach live TUI"
echo ""
echo "  ── STEP 2: TELEGRAM BOT SETUP ──"
echo "    1. Open Telegram → search @BotFather"
echo "    2. Send /newbot → choose name → get token"
echo "    3. wolfctl tg env       Paste your token"
echo "    4. wolfctl tg start     Start Telegram bot"
echo "    5. Open your bot in Telegram → /start"
echo ""
echo "  ── DAILY USE ──"
echo "    wolfctl start-all       Start both bots"
echo "    wolfctl stop-all        Stop both bots"
echo "    wolfctl status-all      Check all services"
echo ""
echo "    wolfctl tg status       Telegram bot status"
echo "    wolfctl tg logs         Telegram bot logs"
echo "    wolfctl tg restart      Restart after config change"
echo ""
echo "  ── FILES ──"
echo "    Bot:        $INSTALL_DIR/"
echo "    CLI Config: ~/.wolfbet_autobot.json"
echo "    CLI DB:     ~/.wolfbet_autobot.db"
echo "    TG Data:    ~/.wolfbot_tg/<user_id>/"
echo "    Env:        $INSTALL_DIR/.env"
echo "    Logs:       wolfctl logs / wolfctl tg logs"
echo ""
