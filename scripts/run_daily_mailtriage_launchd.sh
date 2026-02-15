#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${MAILTRIAGE_REPO:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
CONFIG_PATH="${MAILTRIAGE_CONFIG:-$REPO/config.yml}"
POLICY_PATH="${MAILTRIAGE_POLICY:-$REPO/daily.policy.yml}"

# Allow launchd/cron to pass explicit paths as args.
if [[ $# -ge 1 && -n "${1:-}" ]]; then
  CONFIG_PATH="$1"
fi
if [[ $# -ge 2 && -n "${2:-}" ]]; then
  POLICY_PATH="$2"
fi

cd "$REPO"

# Resolve output.root and timezone from config, then compute log directory under output.root.
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

# launchd/cron won't inherit your terminal exports. Prefer unlocking Bitwarden
# each run using a master password stored in OS Keychain/Secret Service.
#
# Defaults:
# - macOS Keychain generic password: service=mailtriage/bitwarden, account=$USER
# - Linux secret-tool: service=mailtriage/bitwarden, user=$USER
BW_STORE_SERVICE="${MAILTRIAGE_BW_STORE_SERVICE:-mailtriage/bitwarden}"
BW_STORE_USER="${MAILTRIAGE_BW_STORE_USER:-${USER}}"

get_bw_password() {
  if command -v security >/dev/null 2>&1; then
    # macOS Keychain
    security find-generic-password -w -s "${BW_STORE_SERVICE}" -a "${BW_STORE_USER}" 2>/dev/null || return 1
    return 0
  fi
  if command -v secret-tool >/dev/null 2>&1; then
    # Linux Secret Service
    secret-tool lookup service "${BW_STORE_SERVICE}" user "${BW_STORE_USER}" 2>/dev/null || return 1
    return 0
  fi
  return 1
}

unlock_bw_session() {
  local pw session
  pw="$(get_bw_password)" || return 1
  if [[ -z "${pw}" ]]; then
    return 1
  fi
  # Capture session token without logging it.
  session="$(BW_PASSWORD="${pw}" bw unlock --passwordenv BW_PASSWORD --raw 2>/dev/null)" || return 1
  if [[ -z "${session}" ]]; then
    return 1
  fi
  export BW_SESSION="${session}"
  return 0
}

lock_bw() {
  # Always re-lock; ignore failures.
  bw lock >/dev/null 2>&1 || true
  unset BW_SESSION || true
}

trap lock_bw EXIT

# If BW_SESSION is already set (manual runs), respect it. Otherwise try keychain unlock.
if [[ -z "${BW_SESSION:-}" ]]; then
  if ! unlock_bw_session; then
    # Fallback: if a saved session token exists, use it.
    BW_SESSION_FILE="${ROOTDIR}/.mailtriage/bw_session"
    if [[ -f "${BW_SESSION_FILE}" ]]; then
      BW_SESSION="$(tr -d '\n' < "${BW_SESSION_FILE}" 2>/dev/null || true)"
      if [[ -n "${BW_SESSION}" ]]; then
        export BW_SESSION
      fi
    fi
  fi
fi

# Prune logs older than 7 days.
find "$LOGDIR" -type f -name '*.log' -mtime +7 -print -delete >/dev/null 2>&1 || true

OUT_LOG="${LOGDIR}/${DATE_STR}.out.log"
ERR_LOG="${LOGDIR}/${DATE_STR}.err.log"

{
  echo "==== mailtriage-daily ${DATE_STR} start ===="
  date
  echo "repo=${REPO}"
  echo "config=${CONFIG_PATH}"
  echo "policy=${POLICY_PATH}"
  echo "output_root=${ROOTDIR}"
  echo
} >>"$OUT_LOG"

uv run python -m mailtriage.automation.daily_runner --config "$CONFIG_PATH" --policy "$POLICY_PATH" >>"$OUT_LOG" 2>>"$ERR_LOG"

{
  echo
  echo "==== mailtriage-daily ${DATE_STR} end ===="
  date
  echo
} >>"$OUT_LOG"
