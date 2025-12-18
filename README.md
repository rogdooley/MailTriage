Below is a ready-to-paste README.md, verbatim.
It includes a concise troubleshooting section and a single canonical config example, as requested.

⸻

# MailTriage

MailTriage ingests email into a local SQLite database and produces **daily summaries**
(Markdown + JSON) that highlight what actually needs attention.

It is **not** an email client, archive browser, or search engine.  
It is a daily triage tool.

---

## What MailTriage Does

For a given day, MailTriage:

- Ingests messages from IMAP
- Normalizes headers and bodies
- Groups messages into threads
- Applies user-defined rules to classify messages as:
  - **High priority**
  - **Arrival only** (acknowledge, don’t read)
  - **Normal**
  - **Suppressed** (ignored)
- Produces:
  - A human-readable Markdown summary
  - A machine-readable JSON file with the same data

The output is designed for **quick daily review**, not long-term storage.

---

## Requirements

- Python **3.12+**
- [`uv`](https://github.com/astral-sh/uv)
- IMAP access to an email account (read-only is sufficient)

---

## Installation

```bash
git clone https://github.com/yourorg/MailTriage.git
cd MailTriage
uv sync
uv pip install -e .
```

This creates a virtual environment under .venv/ and installs all dependencies.

⸻

Configuration

MailTriage uses a single YAML configuration file.

Canonical config.yml

```yaml
imap:
  host: imap.example.com
  port: 993
  username: you@example.com
  password: yourpassword
  mailbox: INBOX

storage:
  database: data/mailtriage.db
  output_dir: output/

rules:
  high_priority_senders:
    - boss@example.com
    - alerts@example.com

  arrival_only:
    senders:
      - noreply@example.com
    subjects:
      - Daily Dashboard
      - Backup completed

  suppress:
    senders:
      - cron@example.com
    subjects:
      - Cron
      - Automated Report
```

⸻

Rule Semantics (Important)

Sender rules
• Case-insensitive
• Substring match

Subject rules
• Case-insensitive
• Substring match
• Matches anywhere in the subject

Example:

subjects:

- Daily Dashboard

Matches:

[RT] Daily Dashboard — Tickets Updated

Regex is supported internally, but simple substrings are sufficient.

⸻

Running MailTriage

Process the last N days

```bash
uv run mailtriage run --config config.yml --days 1
```

This will: 1. Re-ingest email from IMAP 2. Normalize subjects and bodies 3. Apply classification rules 4. Write daily output files

MailTriage re-ingests on every run by design.
The database is treated as disposable during development.

⸻

Output Layout

```
output/
└── 2025/
    └── 12/
        ├── 18.md
        └── 18.json
```

Markdown (.md)
• Plaintext only
• No HTML
• Optimized for human reading

JSON (.json)
• Same data as Markdown
• Intended for automation or future tooling

⸻

Subject Handling

MailTriage decodes and normalizes subjects at ingest time.

This includes:
• RFC 2047 encoded headers (=?UTF-8?B?...)
• UTF-8 decoding
• Whitespace normalization

The database stores only decoded subjects.

If you see encoded subjects in output, it indicates an ingest bug.

⸻

Body Handling (Intentional Constraints)

MailTriage is not a viewer. Body text is aggressively reduced:
• Plaintext only
• HTML is stripped and converted
• Quoted history is removed
• Output is limited to:
• At most 3 lines, or
• Up to the first paragraph break

This keeps summaries actionable and prevents HTML or boilerplate noise.

⸻

Development Model
• The SQLite database is disposable
• No migrations are provided
• If output looks wrong: 1. Fix ingest 2. Re-run
• This is intentional and simplifies iteration

⸻

Troubleshooting

“I ran it but see no output”
• Check storage.output_dir
• Ensure messages exist for the selected day
• Verify IMAP credentials and mailbox name

“Subjects are not matching rules”
• Rules are substring-based, not exact
• Matching is case-insensitive
• Ensure subjects are decoded (they should be)

“HTML is appearing in Markdown”
• This indicates an ingest or decoding issue
• MailTriage does not render HTML by design
• Re-run after fixing body extraction

“Too much body text is shown”
• Body output is intentionally capped
• Adjust normalize_excerpt() limits if needed
• Avoid increasing limits unless absolutely necessary

⸻

Non-Goals
• Full email rendering
• Long-term archival
• UI or web interface
• Search
• Attachment extraction
