from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterable

from mailtriage.core.db import Database
from mailtriage.core.extract import (
    extract_attachment_names,
    extract_new_text,
    select_body,
)

# --- Secrets ---------------------------------------------------------------


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
    """
    Uses Bitwarden CLI. Expects the user to have already authenticated/unlocked
    in the current shell environment.

    `reference` should be the Bitwarden item id (or any identifier accepted by bw).
    We extract:
      - login.username
      - login.password
    """

    def __init__(self, bw_bin: str = "bw") -> None:
        self._bw_bin = bw_bin

    def resolve(self, reference: str) -> ResolvedSecrets:
        try:
            proc = subprocess.run(
                [self._bw_bin, "get", "item", reference],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise SecretProviderError("Bitwarden CLI 'bw' not found in PATH") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise SecretProviderError(
                "Bitwarden CLI failed. Ensure you're logged in/unlocked.\n"
                f"bw error: {stderr}"
            ) from e

        try:
            item = json.loads(proc.stdout)
            login = item["login"]
            username = str(login.get("username", "")).strip()
            password = str(login.get("password", "")).strip()
        except Exception as e:
            raise SecretProviderError("Failed to parse Bitwarden item JSON") from e

        if not username or not password:
            raise SecretProviderError(
                "Bitwarden item is missing login.username or login.password"
            )

        return ResolvedSecrets(username=username, password=password)


def _secret_provider(provider_name: str) -> SecretProvider:
    p = provider_name.lower()
    if p == "bitwarden":
        return BitwardenSecretProvider()
    if p == "env":
        return EnvSecretProvider()
    raise SecretProviderError(f"Unsupported secrets provider: {provider_name}")


class EnvSecretProvider(SecretProvider):
    """
    Reads secrets from environment variables.

    reference: logical account name, e.g. "WORK_IMAP"
    variables:
      MAILTRIAGE_<REFERENCE>_USERNAME
      MAILTRIAGE_<REFERENCE>_PASSWORD
    """

    def resolve(self, reference: str) -> ResolvedSecrets:
        key = reference.upper()
        user_var = f"MAILTRIAGE_{key}_USERNAME"
        pass_var = f"MAILTRIAGE_{key}_PASSWORD"

        try:
            username = os.environ[user_var]
            password = os.environ[pass_var]
        except KeyError as e:
            raise SecretProviderError(
                f"Missing environment variable: {e.args[0]}"
            ) from e

        if not username or not password:
            raise SecretProviderError("Empty username or password in environment")

        return ResolvedSecrets(username=username, password=password)


# --- IMAP account ----------------------------------------------------------


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

    folders = account_cfg.imap.folders if account_cfg.imap.folders else ["INBOX"]

    return ImapAccount(
        host=account_cfg.imap.host,
        port=account_cfg.imap.port,
        ssl=account_cfg.imap.ssl,
        folders=folders,
        username=creds.username,
        password=creds.password,
    )


# --- IMAP fetch helpers ----------------------------------------------------


@dataclass(frozen=True)
class FetchedMessage:
    uid: str
    raw_rfc822: bytes
    internaldate_utc: datetime


def _connect_imap(acct: ImapAccount) -> imaplib.IMAP4_SSL:
    if not acct.ssl:
        raise RuntimeError("IMAP without SSL is not allowed")
    conn = imaplib.IMAP4_SSL(acct.host, acct.port)
    conn.login(acct.username, acct.password)
    return conn


def _select_readonly(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    typ, _ = conn.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Cannot open folder read-only: {folder}")


def _search_since(conn: imaplib.IMAP4_SSL, since_date: str) -> list[str]:
    # since_date format: "16-Dec-2025" (day-monthname-year)
    typ, data = conn.search(None, "SINCE", since_date)
    if typ != "OK":
        raise RuntimeError("IMAP search failed")
    raw = data[0].strip()
    if not raw:
        return []
    return [x.decode("ascii", "replace") for x in raw.split()]


_INTERNALDATE_RE = re.compile(r'INTERNALDATE "([^"]+)"')


def _parse_internaldate_to_utc(fetch_meta: bytes) -> datetime:
    """
    Example INTERNALDATE: 16-Dec-2025 11:27:30 -0500
    Convert to UTC.
    """
    m = _INTERNALDATE_RE.search(fetch_meta.decode("utf-8", "replace"))
    if not m:
        # fallback: now UTC, but this should be rare
        return datetime.now(timezone.utc)
    s = m.group(1)
    # parsedate_to_datetime expects RFC2822-ish; INTERNALDATE is close enough
    dt = parsedate_to_datetime(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_rfc822_and_internaldate(
    conn: imaplib.IMAP4_SSL, uids: Iterable[str]
) -> dict[str, FetchedMessage]:
    """
    Fetch RFC822 body + INTERNALDATE without marking as read.
    Uses RFC822.PEEK to avoid setting \\Seen.
    """
    out: dict[str, FetchedMessage] = {}
    uid_list = list(uids)
    if not uid_list:
        return out

    # Fetch in chunks to avoid massive fetches
    chunk_size = 50
    for i in range(0, len(uid_list), chunk_size):
        chunk = uid_list[i : i + chunk_size]
        seq = ",".join(chunk).encode("ascii", "ignore")

        typ, data = conn.fetch(seq, "(BODY.PEEK[] INTERNALDATE)")
        if typ != "OK":
            raise RuntimeError("IMAP BODY.PEEK[] fetch failed")

        # data is a list of tuples + b')' separators
        for item in data:
            if not item or not isinstance(item, tuple):
                continue
            meta, raw = item
            # meta begins with the uid number as bytes, e.g. b'123 (RFC822 {..} ...'
            uid = meta.decode("ascii", "replace").split()[0]
            internal_utc = _parse_internaldate_to_utc(meta)
            out[uid] = FetchedMessage(
                uid=uid, raw_rfc822=raw, internaldate_utc=internal_utc
            )

    return out


# --- Message parsing helpers -----------------------------------------------

_MSGID_RE = re.compile(r"<[^>]+>")


def resolve_timestamp_utc(msg: Message, internaldate_utc: datetime) -> datetime:
    """
    Timestamp priority:
      1) Date header
      2) earliest Received (not implemented in v0.1; Date headers usually OK)
      3) INTERNALDATE
    """
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    return internaldate_utc


def _normalize_addr(s: str) -> str:
    return s.strip().lower()


def extract_participants(msg: Message) -> list[str]:
    addrs = []
    addrs.extend(getaddresses([msg.get("From", "")]))
    addrs.extend(getaddresses([msg.get("To", "")]))
    addrs.extend(getaddresses([msg.get("Cc", "")]))

    # take email portion only
    emails = [_normalize_addr(a[1]) for a in addrs if a and a[1]]
    # stable unique
    seen: set[str] = set()
    out: list[str] = []
    for e in emails:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def is_outbound(msg: Message, primary: str, aliases: list[str]) -> bool:
    from_addr = getaddresses([msg.get("From", "")])
    from_email = (
        _normalize_addr(from_addr[0][1]) if from_addr and from_addr[0][1] else ""
    )
    ids = {_normalize_addr(primary), *(_normalize_addr(a) for a in aliases)}
    return from_email in ids


def compute_message_id(msg: Message, account_id: str, folder: str, uid: str) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    if mid and _MSGID_RE.fullmatch(mid):
        return mid
    # synthetic fallback
    return f"synthetic:{account_id}:{folder}:{uid}"


def _canonical_root_reference(msg: Message) -> str | None:
    refs = (msg.get("References") or "").strip()
    if refs:
        ids = _MSGID_RE.findall(refs)
        if ids:
            return ids[0]
    irt = (msg.get("In-Reply-To") or "").strip()
    if irt:
        ids = _MSGID_RE.findall(irt)
        if ids:
            return ids[0]
    return None


_SUBJ_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.IGNORECASE)


def _normalize_subject(subject: str) -> str:
    s = subject.strip()
    while True:
        ns = _SUBJ_PREFIX_RE.sub("", s)
        if ns == s:
            break
        s = ns
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def compute_thread_id(msg: Message) -> str:
    root = _canonical_root_reference(msg)
    if root:
        basis = f"ref:{root}"
    else:
        subj = _normalize_subject(msg.get("Subject", ""))
        basis = f"subj:{subj}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# --- DB writes -------------------------------------------------------------


def ensure_account_row(
    db: Database, *, account_id: str, primary: str, aliases: list[str]
) -> None:
    aliases_json = json.dumps([a.strip() for a in aliases], ensure_ascii=False)
    db.exec(
        "INSERT OR IGNORE INTO accounts (id, primary_address, aliases, created_at_utc) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (account_id, primary, aliases_json),
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
            message_id, account_id, folder,
            date_utc,
            sender, recipients_to, recipients_cc, subject,
            inbound, outbound,
            extracted_new_text, has_attachments, attachment_names,
            thread_id,
            created_at_utc
        ) VALUES (
            ?, ?, ?,
            ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?,
            strftime('%Y-%m-%dT%H:%M:%SZ','now')
        )
        """.strip(),
        (
            message_id,
            account_id,
            folder,
            date_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            sender,
            json.dumps(to_addrs, ensure_ascii=False),
            json.dumps(cc_addrs, ensure_ascii=False),
            subject,
            1 if inbound else 0,
            1 if outbound else 0,
            extracted_text,
            1 if has_attachments else 0,
            json.dumps(attachment_names, ensure_ascii=False)
            if attachment_names
            else None,
            thread_id,
        ),
    )


def upsert_thread(
    db: Database,
    *,
    thread_id: str,
    participants: list[str],
    msg_date_utc: datetime,
    inbound: bool,
    outbound: bool,
) -> None:
    # read existing
    row = db.query_one(
        "SELECT participants, last_inbound_at_utc, last_outbound_at_utc FROM threads WHERE thread_id=?",
        (thread_id,),
    )
    pset: set[str] = set(participants)

    last_in = None
    last_out = None
    if row:
        try:
            existing = json.loads(str(row["participants"]))
            if isinstance(existing, list):
                pset |= {str(x).strip().lower() for x in existing if x}
        except Exception:
            pass
        last_in = row["last_inbound_at_utc"]
        last_out = row["last_outbound_at_utc"]

    def newer(old_iso: str | None, new_dt: datetime) -> bool:
        if not old_iso:
            return True
        try:
            odt = datetime.fromisoformat(str(old_iso).replace("Z", "+00:00"))
            if odt.tzinfo is None:
                odt = odt.replace(tzinfo=timezone.utc)
            return new_dt > odt.astimezone(timezone.utc)
        except Exception:
            return True

    new_iso = msg_date_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    new_last_in = last_in
    new_last_out = last_out

    if inbound and newer(last_in, msg_date_utc):
        new_last_in = new_iso
    if outbound and newer(last_out, msg_date_utc):
        new_last_out = new_iso

    if row is None:
        db.exec(
            "INSERT INTO threads (thread_id, participants, last_inbound_at_utc, last_outbound_at_utc, created_at_utc) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (
                thread_id,
                json.dumps(sorted(pset), ensure_ascii=False),
                new_last_in,
                new_last_out,
            ),
        )
    else:
        db.exec(
            "UPDATE threads SET participants=?, last_inbound_at_utc=?, last_outbound_at_utc=? WHERE thread_id=?",
            (
                json.dumps(sorted(pset), ensure_ascii=False),
                new_last_in,
                new_last_out,
                thread_id,
            ),
        )


# --- Main ingestion --------------------------------------------------------


def ingest_account(
    *,
    db: Database,
    account_cfg,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> None:
    """
    Ingest messages for ONE account for ONE time window.
    Idempotent: messages.message_id is the PK, inserts are INSERT OR IGNORE.
    """
    imap_acct = build_imap_account(account_cfg=account_cfg)

    ensure_account_row(
        db,
        account_id=account_cfg.id,
        primary=account_cfg.identity.primary_address,
        aliases=account_cfg.identity.aliases,
    )

    conn: imaplib.IMAP4_SSL | None = None
    try:
        conn = _connect_imap(imap_acct)

        # Superset search uses date only. Use the window start in UTC (OK for superset).
        since_str = window_start_utc.strftime("%d-%b-%Y")

        for folder in imap_acct.folders:
            _select_readonly(conn, folder)
            uids = _search_since(conn, since_str)
            if not uids:
                continue

            fetched = _fetch_rfc822_and_internaldate(conn, uids)

            for uid, fm in fetched.items():
                msg = message_from_bytes(fm.raw_rfc822)

                msg_date_utc = resolve_timestamp_utc(msg, fm.internaldate_utc)
                if not (window_start_utc <= msg_date_utc < window_end_utc):
                    continue

                # basic headers
                subject = (msg.get("Subject") or "").strip()
                sender = (msg.get("From") or "").strip()

                to_addrs = [
                    a[1].strip().lower()
                    for a in getaddresses([msg.get("To", "")])
                    if a and a[1]
                ]
                cc_addrs = [
                    a[1].strip().lower()
                    for a in getaddresses([msg.get("Cc", "")])
                    if a and a[1]
                ]

                outbound = is_outbound(
                    msg,
                    primary=account_cfg.identity.primary_address,
                    aliases=account_cfg.identity.aliases,
                )
                inbound = not outbound

                message_id = compute_message_id(msg, account_cfg.id, folder, uid)
                thread_id = compute_thread_id(msg)

                # body selection + extraction
                body, _is_html = select_body(msg)
                if "<html" in body.lower():
                    raise RuntimeError("HTML leaked past select_body")
                extracted = extract_new_text(subject=subject, body=body)

                # attachments
                attachment_names = extract_attachment_names(msg)
                has_attachments = bool(attachment_names)

                insert_message(
                    db,
                    message_id=message_id,
                    account_id=account_cfg.id,
                    folder=folder,
                    date_utc=msg_date_utc,
                    sender=sender,
                    to_addrs=to_addrs,
                    cc_addrs=cc_addrs,
                    subject=subject,
                    inbound=inbound,
                    outbound=outbound,
                    extracted_text=extracted.text,
                    has_attachments=has_attachments,
                    attachment_names=attachment_names,
                    thread_id=thread_id,
                )

                participants = extract_participants(msg)
                upsert_thread(
                    db,
                    thread_id=thread_id,
                    participants=participants,
                    msg_date_utc=msg_date_utc,
                    inbound=inbound,
                    outbound=outbound,
                )

    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
