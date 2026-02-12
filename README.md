# MailTriage

MailTriage is a local, batch-oriented IMAP email triage tool.

It ingests email in read-only mode, stores normalized state in SQLite, and produces
daily Markdown and JSON summaries grouped by priority and thread.

There is no server, no daemon, and no UI.

---

## Requirements

- Python 3.11+
- uv
- IMAP account access
- Optional: Bitwarden CLI (`bw`)

---

## Installation

```bash
git clone <repo-url>
cd mailtriage
uv sync
```

---

## Configuration

MailTriage is configured using a single YAML file.

- Unknown keys are rejected
- Paths must be absolute
- All output is local

### Example configuration

```yaml
output:
  root: /absolute/path/to/mailtriage-output

time:
  timezone: America/New_York
  workday_start: "09:00"

accounts:
  - id: work
    imap:
      host: imap.example.com
      port: 993
      ssl: true
      folders: ["INBOX"]
    identity:
      primary_address: user@example.com
      aliases: []
    secrets:
      provider: bitwarden
      reference: <bitwarden-item-id>

rules:
  high_priority_senders:
    - boss@example.com
  collapse_automated: true
  suppress:
    senders: []
    subjects: []
  arrival_only:
    senders: []
    subjects: []

tickets:
  enabled: false
  plugins: []
```

---

## Bitwarden Notes

MailTriage uses the Bitwarden CLI (`bw`) only to retrieve credentials.

The referenced Bitwarden item must contain:

- `login.username`
- `login.password`

No custom fields are required.

MailTriage does not store Bitwarden data.

Before running MailTriage:

```bash
bw login
bw unlock
```

---

## Output Layout

All output is written under `output.root`.

```text
<root>/
├── YYYY/
│   └── MM/
│       ├── DD.md
│       └── DD.json
└── .mailtriage/
    └── state.db
```

- Reports are overwritten on each run
- `state.db` persists ingestion state

---

## Workday Window Semantics

MailTriage does not use calendar days.

A “day” is defined by the configured workday start time.

```yaml
time:
  timezone: America/New_York
  workday_start: "09:00"
```

This defines a rolling window:

```text
09:00 local time → 09:00 local time (next day)
```

### Examples

```text
--date 2025-01-15

covers:

2025-01-15 09:00 local
→ 2025-01-16 09:00 local
```

```text
--days 3

covers three consecutive workday windows,
each rendered as a separate report
```

---

## Running MailTriage

```bash
uv run mailtriage run --config config.yml --days 1
```

or

```bash
uv run mailtriage run --config config.yml --date 2025-01-15
```

## Background Daily Run (No Server)

Use the daily runner for scheduled execution, holiday-aware notification suppression,
and a `latest.md` pointer to the newest report:

```bash
uv run mailtriage-daily --config config.yml --policy daily.policy.yml
```

Use the redacted sample to create a local policy:

```bash
cp daily.policy.example.yml daily.policy.yml
```

See `/Users/dooley/Documents/GithubClone/MailTriage/docs/daily-runner.md` for launchd setup and policy options.

---

## Design Constraints

- IMAP is accessed read-only
- Messages are never marked as read
- State is local-only
- SQLite schema is fixed
