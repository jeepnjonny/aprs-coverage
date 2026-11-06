#!/usr/bin/env bash
# APRS Coverage: deploy the current checkout to /srv/aprs-coverage and restart.
# Run from the git checkout: bash update.sh
set -euo pipefail

INSTALL_DIR="/srv/aprs-coverage"
SERVICE_USER="www-data"
SRC="$(cd "$(dirname "$(realpath "$0")")" && pwd)"

echo "=== Deploying ${SRC} -> ${INSTALL_DIR} ==="
sudo mkdir -p "${INSTALL_DIR}/data"
sudo rsync -a --delete \
  --exclude='.git' --exclude='.venv' --exclude='data' --exclude='.env' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='tests' \
  "${SRC}/" "${INSTALL_DIR}/"

if [ ! -f "${INSTALL_DIR}/.env" ]; then
  sudo cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  echo "  Created ${INSTALL_DIR}/.env from .env.example. Edit APRS_LOGIN!"
fi
sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
sudo chmod 640 "${INSTALL_DIR}/.env"

if [ ! -x "${INSTALL_DIR}/.venv/bin/python" ]; then
  sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
fi
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"

sudo systemctl restart aprs-coverage
echo "  aprs-coverage restarted"

# ── nginx location snippet (included from the apps.k7swi.org server block) ──
SNIPPET=/etc/nginx/snippets/aprs-coverage.conf
if [ -d /etc/nginx ] && ! sudo cmp -s "${INSTALL_DIR}/nginx-aprs-coverage.conf" "${SNIPPET}"; then
  sudo mkdir -p /etc/nginx/snippets
  [ -f "${SNIPPET}" ] && sudo cp "${SNIPPET}" "${SNIPPET}.bak"
  sudo cp "${INSTALL_DIR}/nginx-aprs-coverage.conf" "${SNIPPET}"
  if sudo nginx -t 2>/dev/null; then
    sudo systemctl reload nginx
    echo "  nginx snippet updated and reloaded"
  else
    echo "  WARNING: nginx -t failed with the new snippet; restoring the previous one"
    if [ -f "${SNIPPET}.bak" ]; then sudo mv "${SNIPPET}.bak" "${SNIPPET}"; else sudo rm -f "${SNIPPET}"; fi
    sudo nginx -t
  fi
fi
