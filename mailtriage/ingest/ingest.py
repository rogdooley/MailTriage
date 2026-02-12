from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterable

from mailtriage.core.db import Database
from mailtriage.core.extract import (
    extract_attachment_names,
    extract_new_text,
    select_body,
)

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


class SecretProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedSecrets:
    username: str
    password: str


class SecretProvider:
    def resolve(self, reference: str) -> ResolvedSecrets:
        raise NotImplementedError


class BitwardenSecretProvider(SecretProvider):
    def __init__(self, bw_bin: str = "bw") -> None:
        self._bw_bin = bw_bin

    def _debug(self, msg: str) -> None:
        if os.environ.get("MAILTRIAGE_DEBUG"):
            sys.stderr.write(f"[mailtriage][bitwarden] {msg}\n")

    def resolve(self, reference: str) -> ResolvedSecrets:
        try:
            # Avoid hanging on interactive prompts. If `BW_SESSION` is present,
            # do NOT use `bw status` as a gate: some installations report
            # "locked" even when a valid session token is set.
            has_session = bool(os.environ.get("BW_SESSION"))
            self._debug("BW_SESSION present: " + ("yes" if has_session else "no"))

            if not has_session:
                status = subprocess.run(
                    [self._bw_bin, "status"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                try:
                    st = json.loads(status.stdout)
                    s = str(st.get("status", "")).lower()
                    self._debug(f"bw status: {s!r}")
                    if s in {"locked", "unauthenticated"}:
                        raise SecretProviderError(
                            f"Bitwarden status is '{s}'. Unlock/login Bitwarden before running."
                        )
                except json.JSONDecodeError:
                    # Older bw versions may not return JSON; proceed and rely on timeout/error.
                    pass

            self._debug(f"Fetching item {reference!r}")
            proc = subprocess.run(
                [self._bw_bin, "get", "item", reference],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except FileNotFoundError as e:
            raise SecretProviderError("Bitwarden CLI not found") from e
        except subprocess.TimeoutExpired as e:
            raise SecretProviderError(
                "Bitwarden CLI timed out (is it waiting for unlock/login?)"
            ) from e
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or "Bitwarden error").strip()
            low = err.lower()
            if "locked" in low or "unlock" in low or "session" in low:
                raise SecretProviderError(
                    "Bitwarden vault is locked or session is missing/invalid. "
                    "Run `bw unlock --raw` and set BW_SESSION (or refresh the saved session file)."
                ) from e
            raise SecretProviderError(err) from e

        item = json.loads(proc.stdout)
        login = item.get("login", {})
        username = str(login.get("username", "")).strip()
        password = str(login.get("password", "")).strip()

        if not username or not password:
            raise SecretProviderError("Missing username/password")

        return ResolvedSecrets(username=username, password=password)


class EnvSecretProvider(SecretProvider):
    def resolve(self, reference: str) -> ResolvedSecrets:
        key = reference.upper()
        u = os.environ.get(f"MAILTRIAGE_{key}_USERNAME")
        p = os.environ.get(f"MAILTRIAGE_{key}_PASSWORD")
        if not u or not p:
            raise SecretProviderError("Missing env secrets")
        return ResolvedSecrets(username=u, password=p)


def _secret_provider(name: str) -> SecretProvider:
    if name.lower() == "bitwarden":
        return BitwardenSecretProvider()
    if name.lower() == "env":
        return EnvSecretProvider()
    raise SecretProviderError(f"Unknown provider {name}")


# ---------------------------------------------------------------------------
# IMAP account
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImapAccount:
    host: str
    port: int
    ssl: bool
    folders: list[str]
    username: str
    password: str


def build_imap_account(*, account_cfg) -> ImapAccount:
    provider = _secret_provider(account_cfg.secrets.provider)
    creds = provider.resolve(account_cfg.secrets.reference)

    return ImapAccount(
        host=account_cfg.imap.host,
        port=account_cfg.imap.port,
        ssl=account_cfg.imap.ssl,
        folders=account_cfg.imap.folders or ["INBOX"],
        username=creds.username,
        password=creds.password,
    )


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchedMessage:
    uid: str
    raw_rfc822: bytes
    internaldate_utc: datetime


_INTERNALDATE_RE = re.compile(r'INTERNALDATE "([^"]+)"')


def _debug(msg: str) -> None:
    if os.environ.get("MAILTRIAGE_DEBUG"):
        sys.stderr.write(f"[mailtriage] {msg}\n")


def _call_with_alarm(seconds: int, fn, *args, **kwargs):
    # Use a hard timeout so network/DNS/login issues don't hang forever.
    if seconds <= 0:
        return fn(*args, **kwargs)

    def _handler(_signum, _frame):
        raise TimeoutError(f"Timed out after {seconds}s")

    old = signal.signal(signal.SIGALRM, _handler)
    try:
        signal.alarm(seconds)
        return fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _connect_imap(acct: ImapAccount) -> imaplib.IMAP4_SSL:
    if not acct.ssl:
        raise RuntimeError("SSL required")

    def _connect_and_login() -> imaplib.IMAP4_SSL:
        # Best-effort socket timeout; the alarm above is the real safety net.
        prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(20)
        try:
            _debug(f"IMAP connect {acct.host}:{acct.port}")
            conn = imaplib.IMAP4_SSL(acct.host, acct.port)
            _debug("IMAP login")
            conn.login(acct.username, acct.password)
            _debug("IMAP login OK")
            return conn
        finally:
            socket.setdefaulttimeout(prev)

    return _call_with_alarm(45, _connect_and_login)


def _select_readonly(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    typ, _ = conn.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Cannot open folder {folder}")


def _search_since(conn: imaplib.IMAP4_SSL, since_date: str) -> list[str]:
    typ, data = conn.search(None, "SINCE", since_date)
    if typ != "OK":
        return []
    return data[0].decode().split()


def _parse_internaldate_to_utc(meta: bytes) -> datetime:
    m = _INTERNALDATE_RE.search(meta.decode(errors="replace"))
    if not m:
        return datetime.now(timezone.utc)
    dt = parsedate_to_datetime(m.group(1))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_rfc822_and_internaldate(
    conn: imaplib.IMAP4_SSL, uids: Iterable[str]
) -> dict[str, FetchedMessage]:
    out: dict[str, FetchedMessage] = {}

    uids = list(uids)
    if not uids:
        return out

    for i in range(0, len(uids), 50):
        chunk = uids[i : i + 50]
        seq = ",".join(chunk).encode()

        typ, data = conn.fetch(seq, "(BODY.PEEK[] INTERNALDATE)")
        if typ != "OK":
            raise RuntimeError("IMAP fetch failed")

        for item in data:
            if not isinstance(item, tuple):
                continue
            meta, raw = item
            uid = meta.decode(errors="replace").split()[0]
            out[uid] = FetchedMessage(
                uid=uid,
                raw_rfc822=raw,
                internaldate_utc=_parse_internaldate_to_utc(meta),
            )

    return out


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------


_MSGID_RE = re.compile(r"<[^>]+>")


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def resolve_timestamp_utc(msg: Message, fallback: datetime) -> datetime:
    hdr = msg.get("Date")
    if hdr:
        try:
            dt = parsedate_to_datetime(hdr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return fallback


def compute_message_id(msg: Message, account_id: str, folder: str, uid: str) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    if mid and _MSGID_RE.fullmatch(mid):
        return mid
    return f"synthetic:{account_id}:{folder}:{uid}"


_SUBJ_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.I)


def _normalize_subject(s: str) -> str:
    while True:
        ns = _SUBJ_PREFIX_RE.sub("", s)
        if ns == s:
            break
        s = ns
    return re.sub(r"\s+", " ", s).strip().lower()


def compute_thread_id(msg: Message) -> str:
    refs = msg.get("References") or ""
    m = _MSGID_RE.search(refs)
    if m:
        basis = f"ref:{m.group(0)}"
    else:
        subj = _normalize_subject(decode_mime_header(msg.get("Subject")))
        basis = f"subj:{subj}"
    return hashlib.sha256(basis.encode()).hexdigest()


def extract_sender(msg: Message) -> tuple[str, str | None]:
    addrs = getaddresses([msg.get("From", "")])
    if not addrs:
        return "", None
    name, email = addrs[0]
    return email.lower().strip(), name.strip() or None


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def ensure_account(
    db: Database,
    *,
    account_id: str,
    primary_address: str,
    aliases: list[str],
) -> None:
    # Idempotent: required to satisfy messages.account_id foreign key.
    db.exec(
        """
        INSERT OR IGNORE INTO accounts (
            id, primary_address, aliases, created_at_utc
        ) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (account_id, primary_address, json.dumps(aliases)),
    )


def insert_message(
    db: Database,
    *,
    message_id: str,
    account_id: str,
    folder: str,
    date_utc: datetime,
    sender: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    subject: str,
    inbound: bool,
    outbound: bool,
    extracted_text: str,
    has_attachments: bool,
    attachment_names: list[str],
    thread_id: str,
) -> None:
    db.exec(
        """
        INSERT OR IGNORE INTO messages (
            message_id, account_id, folder, date_utc,
            sender,
            recipients_to, recipients_cc,
            subject, inbound, outbound,
            extracted_new_text,
            has_attachments, attachment_names,
            thread_id, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (
            message_id,
            account_id,
            folder,
            date_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            sender,
            json.dumps(to_addrs),
            json.dumps(cc_addrs),
            subject,
            1 if inbound else 0,
            1 if outbound else 0,
            extracted_text,
            1 if has_attachments else 0,
            json.dumps(attachment_names) if attachment_names else None,
            thread_id,
        ),
    )


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------


def ingest_account(
    *,
    db: Database,
    account_cfg,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> None:
    ensure_account(
        db,
        account_id=account_cfg.id,
        primary_address=account_cfg.identity.primary_address.lower(),
        aliases=[a.lower() for a in account_cfg.identity.aliases],
    )

    acct = build_imap_account(account_cfg=account_cfg)
    conn: imaplib.IMAP4_SSL | None = None

    try:
        conn = _connect_imap(acct)
        since_str = window_start_utc.strftime("%d-%b-%Y")

        for folder in acct.folders:
            _select_readonly(conn, folder)
            uids = _search_since(conn, since_str)
            fetched = _fetch_rfc822_and_internaldate(conn, uids)

            for uid, fm in fetched.items():
                msg = message_from_bytes(fm.raw_rfc822)

                ts = resolve_timestamp_utc(msg, fm.internaldate_utc)
                if not (window_start_utc <= ts < window_end_utc):
                    continue

                sender, _sender_display = extract_sender(msg)
                subject = decode_mime_header(msg.get("Subject"))

                to_addrs = [
                    a[1].lower() for a in getaddresses([msg.get("To", "")]) if a[1]
                ]
                cc_addrs = [
                    a[1].lower() for a in getaddresses([msg.get("Cc", "")]) if a[1]
                ]

                outbound = sender in {
                    account_cfg.identity.primary_address.lower(),
                    *(a.lower() for a in account_cfg.identity.aliases),
                }
                inbound = not outbound

                message_id = compute_message_id(msg, account_cfg.id, folder, uid)
                thread_id = compute_thread_id(msg)

                body, _ = select_body(msg)
                extracted = extract_new_text(subject=subject, body=body)

                insert_message(
                    db,
                    message_id=message_id,
                    account_id=account_cfg.id,
                    folder=folder,
                    date_utc=ts,
                    sender=sender,
                    to_addrs=to_addrs,
                    cc_addrs=cc_addrs,
                    subject=subject,
                    inbound=inbound,
                    outbound=outbound,
                    extracted_text=extracted.text,
                    has_attachments=bool(extract_attachment_names(msg)),
                    attachment_names=extract_attachment_names(msg),
                    thread_id=thread_id,
                )

    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass
