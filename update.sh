#!/usr/bin/env bash
# APRS Coverage: deploy the current checkout to /srv/aprs-coverage and restart.
# Run from the git checkout: bash update.sh
#
# If the app doesn't come back healthy after the restart, the previous code
# (kept in /srv/aprs-coverage.prev) is put back and restarted, and this exits
# non-zero so auto-update.sh logs the failure.
set -euo pipefail

INSTALL_DIR="/srv/aprs-coverage"
PREV_DIR="${INSTALL_DIR}.prev"
SERVICE_USER="www-data"
PORT=8077   # also in setup.sh and nginx-aprs-coverage.conf
HEALTH_TIMEOUT_S=120   # the first start after an upgrade may build database indexes
SRC="$(cd "$(dirname "$(realpath "$0")")" && pwd)"

# Paths in the install that a deploy never touches. Leading "/" anchors them
# to the top, so e.g. a future app/data/ directory is still deployed.
KEEP=(--exclude='/.git' --exclude='/.venv' --exclude='/data' --exclude='/.env' --exclude='/.deployed-rev' --exclude='/.failed-rev'
      --exclude='__pycache__' --exclude='/.pytest_cache' --exclude='/.ruff_cache')

pip_install() {
  sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
}

healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_S))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if sudo systemctl is-active --quiet aprs-coverage \
       && "${INSTALL_DIR}/.venv/bin/python" -c \
          "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/api/config', timeout=3)" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# Put the previous code back (and its packages) and restart it.
rollback() {
  if [ "${HAVE_PREV}" != true ]; then
    echo "  No previous version to roll back to."
    return
  fi
  echo "  Rolling back to the previous version."
  sudo rsync -a --delete "${KEEP[@]}" "${PREV_DIR}/" "${INSTALL_DIR}/"
  sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
  pip_install || true
  sudo systemctl restart aprs-coverage
  if healthy; then echo "  Previous version restored and running."; else echo "  The previous version did not come back either; check: journalctl -u aprs-coverage"; fi
}

echo "=== Deploying ${SRC} -> ${INSTALL_DIR} ==="
sudo mkdir -p "${INSTALL_DIR}/data"

# Keep a copy of what's running now, to roll back to.
HAVE_PREV=false
if [ -d "${INSTALL_DIR}/app" ]; then
  sudo rsync -a --delete "${KEEP[@]}" "${INSTALL_DIR}/" "${PREV_DIR}/"
  HAVE_PREV=true
fi

sudo rsync -a --delete "${KEEP[@]}" \
  --exclude='/tests' --exclude='/docs' --exclude='/.github' \
  "${SRC}/" "${INSTALL_DIR}/"

if [ ! -f "${INSTALL_DIR}/.env" ]; then
  sudo cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  echo "  Created ${INSTALL_DIR}/.env from .env.example. Edit APRS_LOGIN!"
fi
sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
sudo chmod 640 "${INSTALL_DIR}/.env"

if [ ! -x "${INSTALL_DIR}/.venv/bin/python" ]; then
  sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
  sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade pip
fi
# requirements.txt pins (and hashes) every package, so this changes nothing
# unless a commit did. The running app still has the old code loaded, but the
# files on disk are new, so a failure here rolls back too.
if ! pip_install; then
  echo "  ERROR: installing Python packages failed."
  rollback
  exit 1
fi

sudo systemctl restart aprs-coverage
if healthy; then
  echo "  aprs-coverage restarted and answering on port ${PORT}"
else
  echo "  ERROR: aprs-coverage did not come back within ${HEALTH_TIMEOUT_S} s."
  sudo journalctl -u aprs-coverage -n 30 --no-pager || true
  rollback
  exit 1
fi

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

# Record what is now live. auto-update.sh compares against this rather than the
# checkout, so a deploy that failed part-way is tried again on its next run.
if git -C "${SRC}" rev-parse HEAD >/dev/null 2>&1; then
  git -C "${SRC}" rev-parse HEAD | sudo tee "${INSTALL_DIR}/.deployed-rev" >/dev/null
fi
echo "=== Deploy complete ==="
