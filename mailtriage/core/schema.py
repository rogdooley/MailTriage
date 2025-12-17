from __future__ import annotations

import hashlib

from mailtriage.core.db import Database, DatabaseError

SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    primary_address TEXT NOT NULL,
    aliases TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    folder TEXT NOT NULL,

    date_utc TEXT NOT NULL,

    sender TEXT NOT NULL,
    recipients_to TEXT NOT NULL,
    recipients_cc TEXT NOT NULL,
    subject TEXT NOT NULL,

    inbound INTEGER NOT NULL,
    outbound INTEGER NOT NULL,

    extracted_new_text TEXT,
    has_attachments INTEGER NOT NULL,
    attachment_names TEXT,

    thread_id TEXT NOT NULL,

    created_at_utc TEXT NOT NULL,

    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_utc);
CREATE INDEX IF NOT EXISTS idx_messages_account ON messages(account_id);

CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    participants TEXT NOT NULL,
    last_inbound_at_utc TEXT,
    last_outbound_at_utc TEXT,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triage_state (
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (entity_id, entity_type)
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_key TEXT PRIMARY KEY,
    system TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL,
    last_activity_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_ticket_map (
    message_id TEXT NOT NULL,
    ticket_key TEXT NOT NULL,
    PRIMARY KEY (message_id, ticket_key),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (ticket_key) REFERENCES tickets(ticket_key)
);

CREATE TABLE IF NOT EXISTS run_log (
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL
);
""".strip()


def schema_hash() -> str:
    h = hashlib.sha256()
    h.update(SCHEMA_V1_SQL.encode("utf-8"))
    return h.hexdigest()


def ensure_schema_v1(db: Database, timezone: str, workday_start: str) -> None:
    # Create tables (idempotent) and set meta keys if absent
    db.conn.executescript(SCHEMA_V1_SQL)

    _set_meta_if_missing(db, "schema_version", "1")
    _set_meta_if_missing(db, "schema_hash", schema_hash())
    _set_meta_if_missing(db, "timezone", timezone)
    _set_meta_if_missing(db, "workday_start", workday_start)


def verify_schema_hash(db: Database) -> None:
    stored = db.query_value("SELECT value FROM meta WHERE key='schema_hash'")
    if stored is None:
        raise DatabaseError("Database missing schema_hash meta key")
    if stored != schema_hash():
        raise DatabaseError(
            "Database schema hash mismatch. Refusing to run.\n"
            "This build expects a different frozen schema.\n"
            "Create a new DB or use a build matching this DB."
        )


def _set_meta_if_missing(db: Database, key: str, value: str) -> None:
    row = db.query_one("SELECT value FROM meta WHERE key=?", (key,))
    if row is None:
        db.exec("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))
