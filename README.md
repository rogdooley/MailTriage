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
    - email: rt@example.com
      name_regex: "\\bvia\\b" # optional display-name regex for this sender only
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

`mailtriage run` and `mailtriage watch` load `.env` from the current working directory
automatically (without overriding variables already exported in your shell).

## LLM Todo Extraction (Optional)

MailTriage can extract actionable tasks from messages sent by `rules.high_priority_senders`
and maintain a markdown todo workflow.

Set these in `.env`:

```bash
MAILTRIAGE_TODO_ROOT=/absolute/path/to/todos
MAILTRIAGE_LITELLM_API_BASE=https://your-litellm-host/v1
MAILTRIAGE_LITELLM_MODEL=your-model-name
MAILTRIAGE_LITELLM_API_KEY=
MAILTRIAGE_LITELLM_TIMEOUT_SEC=20
MAILTRIAGE_LITELLM_MAX_THREADS=20
MAILTRIAGE_LITELLM_MAX_TASKS_PER_THREAD=5
MAILTRIAGE_LITELLM_MAX_MESSAGES_PER_THREAD=3
MAILTRIAGE_LITELLM_MAX_CHARS_PER_MESSAGE=450
MAILTRIAGE_LITELLM_MAX_OUTPUT_TOKENS=280
MAILTRIAGE_LITELLM_RETRIES=1
MAILTRIAGE_LITELLM_RETRY_BACKOFF_SEC=1.2

# Optional TLS controls (enterprise cert chains)
MAILTRIAGE_LITELLM_CA_BUNDLE=/path/to/company-ca.pem
MAILTRIAGE_LITELLM_INSECURE_SKIP_VERIFY=0
```

If your LiteLLM endpoint uses an internal/self-signed certificate chain, set
`MAILTRIAGE_LITELLM_CA_BUNDLE` to your org CA bundle file. Only use
`MAILTRIAGE_LITELLM_INSECURE_SKIP_VERIFY=1` as a temporary fallback.

If you see frequent timeout errors, increase `MAILTRIAGE_LITELLM_TIMEOUT_SEC`,
reduce `MAILTRIAGE_LITELLM_MAX_THREADS`, reduce `MAILTRIAGE_LITELLM_MAX_MESSAGES_PER_THREAD`,
and/or lower `MAILTRIAGE_LITELLM_MAX_CHARS_PER_MESSAGE`.

If important threads are missing from todo extraction, increase `MAILTRIAGE_LITELLM_MAX_THREADS`
or run with `MAILTRIAGE_DEBUG=1` to see when thread capping is applied.

Behavior on each `mailtriage run` window:

- Reads `<MAILTRIAGE_TODO_ROOT>/running.md` (creates it if missing)
- Moves done-marked items (`- DONE: ...` or `- done: ...`) to `<MAILTRIAGE_TODO_ROOT>/done/YYYY/MM/DD.md`
- Preserves the original markdown line content when moving items (notes/tags stay intact)
- Appends LLM-generated summary+action entries as plain bullets (no checkbox) grouped under `## YYYY-MM-DD` in `running.md`

Entry style:

- `<summary>. Action: <todo>`
- `<summary>. Action: No action required`

If the model returns no parseable todo entries for a thread, MailTriage adds a
subject-based fallback entry so subject-only messages are still captured.

If any required LiteLLM env var is missing, this feature is skipped and normal report generation continues.

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
