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

    sql = f"""
    SELECT m.thread_id, m.date_utc, m.sender, m.subject
    FROM messages m
    JOIN (
      SELECT thread_id, MAX(date_utc) AS max_date
      FROM messages
      WHERE date_utc >= ?
      GROUP BY thread_id
    ) t
      ON t.thread_id = m.thread_id AND t.max_date = m.date_utc
    WHERE m.inbound = 1
      AND ({addr_where})
    ORDER BY m.date_utc ASC
    """.strip()

    rows = db.conn.execute(
        sql,
        (
            cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            *params,
        ),
    ).fetchall()

    out: list[UnrepliedThread] = []
    for r in rows:
        dt = datetime.fromisoformat(str(r[1]).replace("Z", "+00:00"))
        if now_utc - dt >= timedelta(minutes=unreplied_after_minutes):
            out.append(
                UnrepliedThread(
                    thread_id=str(r[0]),
                    date_utc=str(r[1]),
                    sender=str(r[2]),
                    subject=str(r[3]),
                )
            )

    return out
