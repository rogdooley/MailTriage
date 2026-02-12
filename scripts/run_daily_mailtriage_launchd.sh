#!/bin/zsh
set -euo pipefail

REPO="${MAILTRIAGE_REPO:-/Users/dooley/Documents/GithubClone/MailTriage}"
CONFIG_PATH="${MAILTRIAGE_CONFIG:-$REPO/config.yml}"
POLICY_PATH="${MAILTRIAGE_POLICY:-$REPO/daily.policy.yml}"

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
print(str(cfg.rootdir))
print(str(cfg.time.timezone))
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
