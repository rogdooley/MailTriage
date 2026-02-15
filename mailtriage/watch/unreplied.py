from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class UnrepliedThread:
    thread_id: str
    date_utc: str
    sender: str
    subject: str


def _like_json_contains(addr: str) -> str:
    # recipients are stored as a JSON array string, e.g. ["a@b.com", "c@d.com"]
    # This LIKE is crude but effective for normalized lowercased addresses.
    return f'%"{addr}"%'


def find_unreplied_threads(
    *,
    db,
    target_addresses: list[str],
    lookback_days: int,
    unreplied_after_minutes: int,
    now_utc: datetime | None = None,
) -> list[UnrepliedThread]:
    if not target_addresses:
        return []
    if lookback_days <= 0:
        return []
    if unreplied_after_minutes <= 0:
        return []

    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    cutoff = now_utc - timedelta(days=lookback_days)

    addrs = [a.strip().lower() for a in target_addresses if a and a.strip()]
    if not addrs:
        return []

    conds: list[str] = []
    params: list[object] = []
    for a in addrs:
        conds.append("m.recipients_to LIKE ?")
        params.append(_like_json_contains(a))
        conds.append("m.recipients_cc LIKE ?")
        params.append(_like_json_contains(a))

    addr_where = " OR ".join(conds)

    # Find the earliest inbound message in each thread addressed to target_addresses
    # (within lookback), then consider it "unreplied" only if it is also the
    # newest message in the entire thread (within lookback).
    cutoff_z = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    sql = f"""
    WITH req AS (
      SELECT
        m.thread_id,
        m.message_id,
        m.date_utc,
        m.sender,
        m.subject,
        ROW_NUMBER() OVER (
          PARTITION BY m.thread_id
          ORDER BY m.date_utc ASC, m.message_id ASC
        ) AS rn
      FROM messages m
      WHERE m.date_utc >= ?
        AND m.inbound = 1
        AND ({addr_where})
    ),
    maxes AS (
      SELECT thread_id, MAX(date_utc) AS max_date
      FROM messages
      WHERE date_utc >= ?
      GROUP BY thread_id
    )
    SELECT r.thread_id, r.date_utc, r.sender, r.subject
    FROM req r
    JOIN maxes mx ON mx.thread_id = r.thread_id
    WHERE r.rn = 1
      AND mx.max_date = r.date_utc
    ORDER BY r.date_utc ASC
    """.strip()

    rows = db.conn.execute(sql, (cutoff_z, *params, cutoff_z)).fetchall()

    out: list[UnrepliedThread] = []
    seen: set[str] = set()
    for r in rows:
        tid = str(r[0])
        if tid in seen:
            continue
        seen.add(tid)
        dt = datetime.fromisoformat(str(r[1]).replace("Z", "+00:00"))
        if now_utc - dt >= timedelta(minutes=unreplied_after_minutes):
            out.append(
                UnrepliedThread(
                    thread_id=tid,
                    date_utc=str(r[1]),
                    sender=str(r[2]),
                    subject=str(r[3]),
                )
            )

    return out
