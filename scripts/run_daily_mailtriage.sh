#!/bin/zsh
set -euo pipefail

REPO="/Users/dooley/Documents/GithubClone/MailTriage"
cd "$REPO"

exec uv run python -m mailtriage.automation.daily_runner --config config.yml --policy daily.policy.yml
