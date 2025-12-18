from __future__ import annotations

import imaplib
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ImapAccount:
    host: str
    port: int
    ssl: bool
    folders: list[str]
    username: str
    password: str


class ImapFetcher:
    def __init__(self, acct: ImapAccount) -> None:
        self.acct = acct
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapFetcher":
        if not self.acct.ssl:
            raise RuntimeError("IMAP without SSL is not allowed")
        self.conn = imaplib.IMAP4_SSL(self.acct.host, self.acct.port)
        self.conn.login(self.acct.username, self.acct.password)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.conn:
                self.conn.logout()
        finally:
            self.conn = None

    def select_readonly(self, folder: str) -> None:
        assert self.conn
        typ, _ = self.conn.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Cannot open folder read-only: {folder}")

    def search_since(self, since_date: str) -> list[str]:
        # since_date: e.g. "16-Dec-2025"
        assert self.conn
        typ, data = self.conn.search(None, "SINCE", since_date)
        if typ != "OK":
            raise RuntimeError("IMAP search failed")
        return data[0].split()

    def fetch_headers(self, uids: Iterable[str]) -> dict[str, bytes]:
        assert self.conn
        if not uids:
            return {}
        seq = b",".join(uids)
        typ, data = self.conn.fetch(seq, "(BODY.PEEK[HEADER])")
        if typ != "OK":
            raise RuntimeError("IMAP header fetch failed")
        out: dict[str, bytes] = {}
        for i in range(0, len(data), 2):
            if not data[i]:
                continue
            uid = data[i][0].split()[0].decode()
            out[uid] = data[i][1]
        return out
