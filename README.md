# MailTriage

MailTriage is a local-first email triage tool designed to reduce inbox overload without sacrificing security or privacy.

It connects to one or more IMAP accounts in **read-only mode**, extracts only new and actionable content, and produces **daily summaries** that suppress conversations you have already replied to while resurfacing new replies automatically.

MailTriage is built for users who want deterministic behavior, auditable output, and full control over their data.

## What MailTriage Is

- A read-only IMAP client that never marks messages as read
- A conversation-aware triage system (thread-based, not sender-based)
- A daily report generator producing Markdown and JSON artifacts
- A local-only tool with no required network services beyond IMAP
- A security-conscious utility with pluggable secret providers

## What MailTriage Is Not

- A mail client
- A replacement for your IMAP server or webmail
- A cloud service
- An AI-first summarization engine (AI may be added later, optionally)

## Security Model

- IMAP access is read-only using `BODY.PEEK`
- Messages are never modified or flagged on the server
- Credentials are not stored on disk by default
- Secrets are retrieved via pluggable providers (Bitwarden supported out of the box)
- All processing occurs locally
- Output files contain only normalized message data, not raw mail blobs

## How It Works (High-Level)

1. Connects to configured IMAP accounts and folders
2. Fetches messages for selected calendar days without marking them read
3. Normalizes and parses messages into conversations (threads)
4. Suppresses threads where the user has already replied
5. Extracts only newly added text from replies
6. Applies prioritization and de-duplication rules
7. Writes one Markdown and one JSON summary per day

## Outputs

For each processed day:
Each file represents exactly one calendar day.

## Non-Goals

- Automatic email sending or replying
- Server-side components
- Centralized storage
- Implicit message deletion or archival
