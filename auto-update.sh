#!/usr/bin/env bash
# APRS Coverage: cron auto-updater (no GitHub Actions needed).
# Pulls new commits on the current branch and runs update.sh when there are any.
# Run it from the git checkout, not from /srv/aprs-coverage. Example crontab entry:
#   */10 * * * * /usr/bin/flock -n /run/lock/aprs-coverage-update.lock bash /path/to/aprs-coverage/auto-update.sh 2>&1 | systemd-cat -t aprs-coverage-update
# (Output goes to the journal: journalctl -t aprs-coverage-update)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
cd "${REPO_DIR}"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

if [ -n "$(git status --porcelain)" ]; then
  log "ERROR: uncommitted changes in ${REPO_DIR}, refusing to pull."
  exit 1
fi

INSTALL_DIR="/srv/aprs-coverage"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch --quiet origin "${BRANCH}"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/${BRANCH}")"
# What update.sh last deployed successfully. Compared instead of the checkout,
# so if a deploy fails after the merge, the next run tries it again.
# (Before this file existed, assume the checkout is what's live.)
DEPLOYED="$(cat "${INSTALL_DIR}/.deployed-rev" 2>/dev/null || echo "${LOCAL}")"

if [ "${DEPLOYED}" = "${REMOTE}" ]; then
  log "Up to date (${REMOTE:0:7})."
  exit 0
fi

# A commit whose deploy keeps failing (update.sh rolls it back each time) is
# tried MAX_TRIES times, not every 10 minutes forever. A new commit resets it.
MAX_TRIES=3
FAILED_FILE="${INSTALL_DIR}/.failed-rev"
read -r FAILED_REV FAILED_N 2>/dev/null < "${FAILED_FILE}" || { FAILED_REV=""; FAILED_N=0; }
if [ "${FAILED_REV}" = "${REMOTE}" ] && [ "${FAILED_N:-0}" -ge "${MAX_TRIES}" ]; then
  log "Not retrying ${REMOTE:0:7}: its deploy failed ${FAILED_N} times. Push a fix, or run update.sh by hand."
  exit 1
fi

if [ "${LOCAL}" != "${REMOTE}" ]; then
  log "Updating ${LOCAL:0:7} -> ${REMOTE:0:7}"
  git merge --ff-only "origin/${BRANCH}"
else
  log "Retrying deploy of ${REMOTE:0:7} (last successful deploy: ${DEPLOYED:0:7})"
fi
if ! bash update.sh; then
  [ "${FAILED_REV}" = "${REMOTE}" ] || FAILED_N=0
  echo "${REMOTE} $((FAILED_N + 1))" | sudo tee "${FAILED_FILE}" >/dev/null
  log "ERROR: deploy of ${REMOTE:0:7} failed (attempt $((FAILED_N + 1)) of ${MAX_TRIES})."
  exit 1
fi
sudo rm -f "${FAILED_FILE}"
log "Deployed $(git rev-parse --short HEAD)."
