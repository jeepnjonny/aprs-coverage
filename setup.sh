#!/usr/bin/env bash
# APRS Coverage: first-time server setup (Debian/Ubuntu, nginx, systemd)
# Serves the app at https://apps.k7swi.org/aprs-coverage/ through the existing
# apps.k7swi.org nginx site (which already has its TLS certificate).
# Run from the git checkout as a user with sudo:  bash setup.sh
set -euo pipefail

INSTALL_DIR="/srv/aprs-coverage"
SERVICE_USER="www-data"
HOSTNAME="apps.k7swi.org"
URL_PATH="/aprs-coverage/"
PORT=8077   # also in nginx-aprs-coverage.conf
SRC="$(cd "$(dirname "$(realpath "$0")")" && pwd)"

echo "=== APRS Coverage setup ==="

# ── packages ─────────────────────────────────────────────────────────────────
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv rsync nginx

# ── systemd service ──────────────────────────────────────────────────────────
# One worker only: the APRS-IS reader runs inside the web process.
sudo tee /etc/systemd/system/aprs-coverage.service > /dev/null <<EOF
[Unit]
Description=APRS Coverage map (web + APRS-IS ingest)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log --timeout-graceful-shutdown 10
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable aprs-coverage

# ── deploy code ──────────────────────────────────────────────────────────────
bash "${SRC}/update.sh"

# ── admin password ───────────────────────────────────────────────────────────
if [ -t 0 ]; then
  if ! sudo -u "${SERVICE_USER}" bash -c "cd ${INSTALL_DIR} && .venv/bin/python -c \"from app import db; db.init(); import sys; sys.exit(0 if db.get_setting('admin_password') else 1)\""; then
    echo "Set the admin password:"
    sudo -u "${SERVICE_USER}" bash -c "cd ${INSTALL_DIR} && .venv/bin/python -m app.manage set-password"
  fi
fi

# ── nginx ────────────────────────────────────────────────────────────────────
# update.sh has already copied the location snippet to /etc/nginx/snippets.
# It only takes effect once the apps.k7swi.org server block includes it.
INCLUDE_LINE="include snippets/aprs-coverage.conf;"
NGINX_OK=false
if grep -rqsF "snippets/aprs-coverage.conf" /etc/nginx/sites-enabled /etc/nginx/conf.d; then
  sudo nginx -t && sudo systemctl reload nginx
  NGINX_OK=true
  echo "  nginx: snippet is included, reloaded"
else
  SITE_FILE="$(grep -rlsE "server_name[^;]*${HOSTNAME}" /etc/nginx/sites-enabled /etc/nginx/conf.d | head -1 || true)"
  echo ""
  echo "  nginx: one manual step left. Add this line inside the HTTPS (listen 443)"
  echo "  server block for ${HOSTNAME}${SITE_FILE:+ in ${SITE_FILE}}:"
  echo ""
  echo "      ${INCLUDE_LINE}"
  echo ""
  echo "  then run:  sudo nginx -t && sudo systemctl reload nginx"
fi

echo ""
echo "=== Setup complete ==="
echo "  Edit config:  sudo nano ${INSTALL_DIR}/.env   (then: sudo systemctl restart aprs-coverage)"
echo "  Status:       sudo systemctl status aprs-coverage"
echo "  Logs:         sudo journalctl -u aprs-coverage -f"
echo "  Map:          https://${HOSTNAME}${URL_PATH}"
echo "  Admin:        https://${HOSTNAME}${URL_PATH}admin"
[ "${NGINX_OK}" = true ] || echo "  (after adding the nginx include line above)"
