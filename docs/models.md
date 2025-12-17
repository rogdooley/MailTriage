# MailTriage Data Models

This document defines the core data models used throughout MailTriage.
These definitions are language-agnostic and treated as stable contracts.

---

## Message (Normalized)

Represents a single email message after parsing and normalization.

Fields:

- message_id: globally unique message identifier
- account_id: configured account identifier
- folder: IMAP folder name
- date_utc: message timestamp in UTC
- date_local: message timestamp in configured local timezone
- from: parsed sender address
- to: list of recipient addresses
- cc: list of CC addresses
- subject: decoded subject line
- inbound: boolean (true if not sent by the user)
- outbound: boolean (true if sent by the user)
- extracted_new_text: text added by this message, excluding quoted content
- has_attachments: boolean
- thread_id: identifier of the conversation this message belongs to

---

## Thread

Represents a conversation derived from standard email headers.

Fields:

- thread_id: stable conversation identifier
- participants: set of involved addresses
- last_inbound_at: timestamp of the most recent inbound message
- last_outbound_at: timestamp of the most recent outbound message
- unresolved: derived boolean indicating whether action is required

A thread is considered resolved when the latest outbound message
occurs after the latest inbound message.

---

## TriageState

Represents user-controlled triage metadata.

Fields:

- entity_id: message_id or thread_id
- state: one of [open, done, ignored]
- updated_at: timestamp of last state change
