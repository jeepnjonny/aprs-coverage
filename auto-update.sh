#!/usr/bin/env bash
# APRS Coverage: cron auto-updater (no GitHub Actions needed).
# Pulls new commits on the current branch and runs update.sh when there are any.
# Run it from the git checkout, not from /srv/aprs-coverage. Example crontab entry:
#   */10 * * * * /usr/bin/flock -n /tmp/aprs-coverage-update.lock bash /path/to/aprs-coverage/auto-update.sh >> /var/log/aprs-coverage-update.log 2>&1
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
cd "${REPO_DIR}"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

if [ -n "$(git status --porcelain)" ]; then
  log "ERROR: uncommitted changes in ${REPO_DIR}, refusing to pull."
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch --quiet origin "${BRANCH}"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/${BRANCH}")"

if [ "${LOCAL}" = "${REMOTE}" ]; then
  log "Up to date (${LOCAL:0:7})."
  exit 0
fi

log "Updating ${LOCAL:0:7} -> ${REMOTE:0:7}"
git merge --ff-only "origin/${BRANCH}"
bash update.sh
log "Deployed $(git rev-parse --short HEAD)."
