#!/usr/bin/env bash
set -euo pipefail

REPO="${MAILTRIAGE_REPO:-/Users/dooley/Documents/GithubClone/MailTriage}"
CONFIG_PATH="${MAILTRIAGE_CONFIG:-$REPO/config.yml}"
POLICY_PATH="${MAILTRIAGE_POLICY:-$REPO/daily.policy.yml}"

# Allow launchd/cron to pass explicit config/policy paths as args.
if [[ $# -ge 1 && -n "${1:-}" ]]; then
  CONFIG_PATH="$1"
fi
if [[ $# -ge 2 && -n "${2:-}" ]]; then
  POLICY_PATH="$2"
fi

cd "$REPO"

# Resolve output.root and timezone from config.
export MAILTRIAGE_CONFIG_PATH="$CONFIG_PATH"
read -r ROOTDIR TZNAME <<EOF
$(uv run python - <<'PY'
import os
from pathlib import Path
from mailtriage.core.config import load_config

cfg_path = Path(os.environ["MAILTRIAGE_CONFIG_PATH"])
cfg = load_config(cfg_path)
print(f"{cfg.rootdir}\t{cfg.time.timezone}")
PY
)
EOF

if [[ -z "${ROOTDIR}" ]]; then
  echo "Failed to resolve output.root from config: ${CONFIG_PATH}" >&2
  exit 2
fi
if [[ -z "${TZNAME}" ]]; then
  TZNAME="UTC"
fi

DATE_STR="$(uv run python - <<PY
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo("${TZNAME}")).strftime("%Y-%m-%d"))
PY
)"

LOGDIR="${ROOTDIR}/.mailtriage/logs"
mkdir -p "$LOGDIR"

# Unlock/lock Bitwarden per-run using OS secret store.
BW_STORE_SERVICE="${MAILTRIAGE_BW_STORE_SERVICE:-mailtriage/bitwarden}"
BW_STORE_USER="${MAILTRIAGE_BW_STORE_USER:-${USER}}"

get_bw_password() {
  if command -v security >/dev/null 2>&1; then
    security find-generic-password -w -s "${BW_STORE_SERVICE}" -a "${BW_STORE_USER}" 2>/dev/null || return 1
    return 0
  fi
  if command -v secret-tool >/dev/null 2>&1; then
    secret-tool lookup service "${BW_STORE_SERVICE}" user "${BW_STORE_USER}" 2>/dev/null || return 1
    return 0
  fi
  return 1
}

unlock_bw_session() {
  local pw session
  pw="$(get_bw_password)" || return 1
  [[ -n "${pw}" ]] || return 1
  session="$(BW_PASSWORD="${pw}" bw unlock --passwordenv BW_PASSWORD --raw 2>/dev/null)" || return 1
  [[ -n "${session}" ]] || return 1
  export BW_SESSION="${session}"
}

lock_bw() {
  bw lock >/dev/null 2>&1 || true
  unset BW_SESSION || true
}
trap lock_bw EXIT

if [[ -z "${BW_SESSION:-}" ]]; then
  unlock_bw_session || true
fi

# Prune logs older than 7 days.
find "$LOGDIR" -type f -name '*.log' -mtime +7 -print -delete >/dev/null 2>&1 || true

OUT_LOG="${LOGDIR}/watch-${DATE_STR}.out.log"
ERR_LOG="${LOGDIR}/watch-${DATE_STR}.err.log"

{
  echo "==== mailtriage-watch ${DATE_STR} start ===="
  date
  echo "repo=${REPO}"
  echo "config=${CONFIG_PATH}"
  echo "policy=${POLICY_PATH}"
  echo "output_root=${ROOTDIR}"
  if ! command -v terminal-notifier >/dev/null 2>&1; then
    echo "warning: terminal-notifier not found; macOS notifications may be suppressed"
  fi
  echo
} >>"$OUT_LOG"

# Watch mode ingests a rolling lookback and notifies only when a rule triggers.
uv run mailtriage watch --config "$CONFIG_PATH" >>"$OUT_LOG" 2>>"$ERR_LOG"

{
  echo
  echo "==== mailtriage-watch ${DATE_STR} end ===="
  date
  echo
} >>"$OUT_LOG"
