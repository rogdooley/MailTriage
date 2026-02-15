#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${MAILTRIAGE_REPO:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"

CONFIG_PATH="${MAILTRIAGE_CONFIG:-$REPO/config.yml}"
POLICY_PATH="${MAILTRIAGE_POLICY:-$REPO/daily.policy.yml}"

if [[ $# -ge 1 && -n "${1:-}" ]]; then
  CONFIG_PATH="$1"
fi
if [[ $# -ge 2 && -n "${2:-}" ]]; then
  POLICY_PATH="$2"
fi

cd "$REPO"

exec uv run python -m mailtriage.automation.daily_runner --config "$CONFIG_PATH" --policy "$POLICY_PATH"
