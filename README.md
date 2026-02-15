# MailTriage

MailTriage is a local, batch-oriented IMAP email triage tool.

It ingests email in read-only mode, stores normalized state in SQLite, and produces
daily Markdown, HTML, and JSON summaries grouped by priority and thread.

There is no server and no daemon. Output is written to local files.

---

## Requirements

- Python 3.11+
- uv
- IMAP account access
- Optional: Bitwarden CLI (`bw`)
- Optional (macOS notifications): `terminal-notifier`

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

watch:
  ingest_lookback_days: 7
  unreplied:
    enabled: false
    rules: []
```

---

## Bitwarden Notes

MailTriage uses the Bitwarden CLI (`bw`) only to retrieve credentials.

The referenced Bitwarden item must contain:

- `login.username`
- `login.password`

No custom fields are required.

MailTriage does not store Bitwarden data.

### Interactive setup (once)

```bash
bw login
```

### Non-interactive unlock (recommended on macOS)

If `bw` is locked, MailTriage can auto-unlock using your OS secret store.

macOS Keychain (service is `mailtriage/bitwarden`, account is your macOS username):

```bash
security add-generic-password -U -s "mailtriage/bitwarden" -a "$USER" -w
security find-generic-password -w -s "mailtriage/bitwarden" -a "$USER" >/dev/null && echo OK
```

Optional overrides:

- `MAILTRIAGE_BW_STORE_SERVICE` (default `mailtriage/bitwarden`)
- `MAILTRIAGE_BW_STORE_USER` (default `$USER`)

---

## Output Layout

All output is written under `output.root`.

```text
<root>/
├── YYYY/
│   └── MM/
│       ├── DD.md
│       ├── DD.html
│       └── DD.json
├── index.html
├── latest.md
├── watch/
│   └── unreplied.html
└── .mailtriage/
    └── state.db
```

- `state.db` persists ingestion state.
- Reports are overwritten for the same day/window.

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

## Static Viewer (No Server)

After generating reports, open:

- `<output.root>/index.html` (sidebar of days, newest-first)

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

See `docs/daily-runner.md` for launchd setup, Bitwarden unlock options, and logging paths.

## Watch Mode (Hourly Checks)

Watch mode ingests a rolling lookback window (configurable) and runs watchers (no reports).

```bash
uv run mailtriage watch --config config.yml
```

If `watch.unreplied` is enabled, the hourly watcher writes:

- `<output.root>/watch/unreplied.html`

---

## Design Constraints

- IMAP is accessed read-only
- Messages are never marked as read
- State is local-only
- SQLite schema is fixed
