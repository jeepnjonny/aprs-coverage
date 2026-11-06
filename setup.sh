#!/usr/bin/env bash
# APRS Coverage: first-time server setup (Debian/Ubuntu, nginx, certbot)
# Run from the git checkout as a user with sudo:  bash setup.sh [--ssl]
set -euo pipefail

INSTALL_DIR="/srv/aprs-coverage"
SERVICE_USER="www-data"
HOSTNAME="apps.k7swi.org"
PORT=8077
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"   # Let's Encrypt notices; prompted for if unset
SSL=${1:-""}
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
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
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

# ── nginx + TLS ──────────────────────────────────────────────────────────────
NGINX_AVAILABLE=/etc/nginx/sites-available/aprs-coverage
NGINX_ENABLED=/etc/nginx/sites-enabled/aprs-coverage
HTTP_ONLY_CONF="server {
    listen 80;
    listen [::]:80;
    server_name ${HOSTNAME};
    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}"

CERT_EXISTS=false
[ -f "/etc/letsencrypt/live/${HOSTNAME}/fullchain.pem" ] && CERT_EXISTS=true

if [ "${CERT_EXISTS}" = true ]; then
  sudo cp "${INSTALL_DIR}/nginx-aprs-coverage.conf" "${NGINX_AVAILABLE}"
  sudo ln -sf "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"
  sudo nginx -t && sudo systemctl reload nginx
  echo "  nginx: SSL config deployed for https://${HOSTNAME}/"
elif [ "${SSL}" = "--ssl" ]; then
  echo "${HTTP_ONLY_CONF}" | sudo tee "${NGINX_AVAILABLE}" > /dev/null
  sudo ln -sf "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"
  sudo nginx -t && sudo systemctl reload nginx
  command -v certbot &>/dev/null || sudo apt-get install -y -qq certbot python3-certbot-nginx
  if [ -z "${CERTBOT_EMAIL}" ]; then
    read -rp "Email for Let's Encrypt expiry notices: " CERTBOT_EMAIL
  fi
  sudo certbot certonly --nginx -d "${HOSTNAME}" --non-interactive --agree-tos -m "${CERTBOT_EMAIL}"
  sudo cp "${INSTALL_DIR}/nginx-aprs-coverage.conf" "${NGINX_AVAILABLE}"
  sudo nginx -t && sudo systemctl reload nginx
  echo "  SSL configured: https://${HOSTNAME}/"
elif [ -f "${NGINX_AVAILABLE}" ]; then
  echo "  nginx: ${NGINX_AVAILABLE} exists, leaving it alone. Run with --ssl for HTTPS."
else
  echo "${HTTP_ONLY_CONF}" | sudo tee "${NGINX_AVAILABLE}" > /dev/null
  sudo ln -sf "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"
  sudo nginx -t && sudo systemctl reload nginx
  echo "  nginx: HTTP only. Re-run with --ssl for HTTPS (COOKIE_SECURE=1 needs HTTPS to log in)."
fi

echo ""
echo "=== Setup complete ==="
echo "  Edit config:  sudo nano ${INSTALL_DIR}/.env   (then: sudo systemctl restart aprs-coverage)"
echo "  Status:       sudo systemctl status aprs-coverage"
echo "  Logs:         sudo journalctl -u aprs-coverage -f"
echo "  Admin:        https://${HOSTNAME}/admin"
