#!/usr/bin/env bash
# WolfBet Admin Web App — Server Installer
set -euo pipefail

APP_NAME="wolfbet-web"
INSTALL_DIR="$HOME/wolfbet-web"
REPO_DIR="$HOME/wolfbet"
BOT_INSTALL_DIR="$HOME/wolfbet-bot"
PORT=8002
DOMAIN=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain) DOMAIN="$2"; shift 2 ;;
        --port)   PORT="$2"; shift 2 ;;
        --repo)   REPO_DIR="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   WolfBet Admin Web App — Installer        ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. System deps ──
echo "[1/7] Checking system dependencies…"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx > /dev/null 2>&1
echo "  ✓ System packages installed"

# ── 2. Create install dir ──
echo "[2/7] Setting up install directory…"
mkdir -p "$INSTALL_DIR"

if [ -d "$REPO_DIR/web" ]; then
    # Copy as 'web' package so "from web.x import y" works
    mkdir -p "$INSTALL_DIR/web"
    cp -r "$REPO_DIR/web/"* "$INSTALL_DIR/web/"
    # Keep requirements.txt at top level for pip
    [ -f "$INSTALL_DIR/web/requirements.txt" ] && cp "$INSTALL_DIR/web/requirements.txt" "$INSTALL_DIR/requirements.txt"
    echo "  ✓ Files copied from $REPO_DIR/web/"
else
    echo "  ✗ $REPO_DIR/web/ not found."
    exit 1
fi

# Copy shared/ library (cross-bot shared modules)
SHARED_DIR="$REPO_DIR/../shared"
if [ -d "$SHARED_DIR" ]; then
    rm -rf "$INSTALL_DIR/shared"
    cp -r "$SHARED_DIR" "$INSTALL_DIR/"
    echo "  ✓ shared/ library copied"
else
    echo "  ⚠ $SHARED_DIR not found — shared imports may fail"
fi

# ── 3. Python venv + deps ──
echo "[3/7] Setting up Python environment…"
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "  ✓ Dependencies installed"

# ── 4. Generate .env ──
echo "[4/7] Configuring environment…"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
    cat > "$INSTALL_DIR/.env" << EOF
# WolfBet Admin Web App — Configuration
# Generated on $(date -u +%Y-%m-%dT%H:%M:%SZ)

WOLFBET_WEB_SECRET_KEY=$SECRET_KEY
WOLFBET_WEB_ADMIN_USER=admin
WOLFBET_WEB_ADMIN_PASS=changeme123
WOLFBET_WEB_HOST=$([ -n "$DOMAIN" ] && echo "127.0.0.1" || echo "0.0.0.0")
WOLFBET_WEB_PORT=$PORT
WOLFBET_WEB_DEBUG=false

WOLFBET_WEB_DB_PATH=$HOME/.wolfbet_web.db
WOLFBET_WEB_BOT_DATA_DIR=$HOME/.wolfbot_tg
WOLFBET_WEB_INSTALL_DIR=$BOT_INSTALL_DIR
WOLFBET_WEB_REPO_DIR=$REPO_DIR
EOF
    chmod 600 "$INSTALL_DIR/.env"
    echo "  ✓ .env created (secret key auto-generated)"
    echo "  ⚠ Change the admin password: nano $INSTALL_DIR/.env"
else
    echo "  ✓ .env already exists (keeping existing)"
fi

# ── 5. Systemd service ──
echo "[5/7] Creating systemd service…"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$APP_NAME.service" << EOF
[Unit]
Description=WolfBet Admin Web App
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/uvicorn web.app:app --host $([ -n "$DOMAIN" ] && echo "127.0.0.1" || echo "0.0.0.0") --port $PORT --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$APP_NAME"
echo "  ✓ Systemd service created and enabled"

# ── 6. Nginx config ──
echo "[6/7] Configuring Nginx…"
if [ -n "$DOMAIN" ]; then
    sudo tee "/etc/nginx/sites-available/$APP_NAME" > /dev/null << EOF
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }

    location /static/ {
        alias $INSTALL_DIR/web/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
EOF
    sudo ln -sf "/etc/nginx/sites-available/$APP_NAME" "/etc/nginx/sites-enabled/$APP_NAME"
    sudo nginx -t && sudo systemctl reload nginx
    echo "  ✓ Nginx configured for $DOMAIN"
    echo ""
    echo "  To add HTTPS:"
    echo "    sudo certbot --nginx -d $DOMAIN"
else
    echo "  ⏭ Skipped (no --domain provided)"
fi

# ── 7. Start ──
echo "[7/7] Starting service…"
systemctl --user start "$APP_NAME"
sleep 2
if systemctl --user is-active --quiet "$APP_NAME"; then
    echo "  ✓ $APP_NAME is running on port $PORT"
else
    echo "  ✗ Failed to start. Check:"
    echo "    journalctl --user -u $APP_NAME -n 50 --no-pager"
    exit 1
fi

echo ""
echo "════════════════════════════════════════════"
echo "  ✅ WolfBet Admin Web App installed!"
echo ""
echo "  Local:   http://127.0.0.1:$PORT"
if [ -n "$DOMAIN" ]; then
echo "  Domain:  http://$DOMAIN"
fi
echo ""
echo "  Login:   admin / changeme123"
echo "  Config:  $INSTALL_DIR/.env"
echo ""
echo "  Commands:"
echo "    wolfctl web start    Start web app"
echo "    wolfctl web stop     Stop web app"
echo "    wolfctl web restart  Restart web app"
echo "    wolfctl web status   Show status"
echo "    wolfctl web logs     Stream logs"
echo "    wolfctl web update   Update web app files"
echo "════════════════════════════════════════════"
