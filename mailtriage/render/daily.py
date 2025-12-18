from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ----------------------------
# Rule helpers
# ----------------------------


def _query_all(db, sql: str, params: tuple = ()) -> list[dict]:
    """
    Execute a SELECT and return all rows as dicts.
    Compatible with the existing Database abstraction.
    """
    cur = db.conn.execute(sql, params)
    rows = cur.fetchall()
    return [dict(row) for row in rows]


def _fmt_time(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%H:%M")


def _match_any(patterns: list[str], value: str) -> bool:
    value = value.lower()
    for pat in patterns:
        if re.search(pat, value):
            return True
    return False


def classify_message(msg: dict[str, Any], rules) -> str:
    """
    Returns one of:
      suppress | arrival_only | high_priority | normal
    """
    sender = msg["sender"].lower()
    subject = msg["subject"].lower()

    if _match_any(rules.suppress.senders, sender) or _match_any(
        rules.suppress.subjects, subject
    ):
        return "suppress"

    if _match_any(rules.arrival_only.senders, sender) or _match_any(
        rules.arrival_only.subjects, subject
    ):
        return "arrival_only"

    if sender in {s.lower() for s in rules.high_priority_senders}:
        return "high_priority"

    return "normal"


# ----------------------------
# DB loading
# ----------------------------


def load_messages_for_day(db, day: date) -> list[dict[str, Any]]:
    start = f"{day.isoformat()}T00:00:00Z"
    end = f"{day.isoformat()}T23:59:59Z"

    rows = _query_all(
        db,
        """
        SELECT *
        FROM messages
        WHERE date_utc BETWEEN ? AND ?
        ORDER BY date_utc ASC
        """,
        (start, end),
    )

    return [dict(row) for row in rows]


def load_threads_for_messages(db, messages: list[dict[str, Any]]) -> dict[str, dict]:
    thread_ids = {m["thread_id"] for m in messages if m["thread_id"]}
    if not thread_ids:
        return {}

    placeholders = ",".join("?" for _ in thread_ids)
    rows = _query_all(
        db,
        f"""
        SELECT *
        FROM threads
        WHERE thread_id IN ({placeholders})
        """,
        tuple(thread_ids),
    )

    return {row["thread_id"]: dict(row) for row in rows}


# ----------------------------
# Rendering
# ----------------------------


def render_day(
    *,
    db,
    day: date,
    rootdir: Path,
    rules,
    explain: bool = False,
) -> None:
    messages = load_messages_for_day(db, day)
    threads = load_threads_for_messages(db, messages)

    buckets: dict[str, list[dict[str, Any]]] = {
        "high_priority": [],
        "normal": [],
        "arrival_only": [],
    }

    explain_map: dict[str, str] = {}

    for msg in messages:
        classification = classify_message(msg, rules)

        if explain:
            explain_map[msg["message_id"]] = classification

        if classification == "suppress":
            continue

        buckets[classification].append(msg)

    # ----------------------------
    # Thread grouping for normal messages
    # ----------------------------

    threads_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for msg in buckets["normal"]:
        threads_grouped[msg["thread_id"]].append(msg)

    # Suppress threads already replied to
    actionable_threads = {}
    for tid, msgs in threads_grouped.items():
        t = threads.get(tid)
        if not t:
            actionable_threads[tid] = msgs
            continue

        last_in = t.get("last_inbound_at_utc")
        last_out = t.get("last_outbound_at_utc")

        if last_out and last_in and last_out >= last_in:
            continue  # already replied

        actionable_threads[tid] = msgs

    # ----------------------------
    # Build JSON
    # ----------------------------

    json_out: dict[str, Any] = {
        "date": day.isoformat(),
        "summary": {
            "total_messages": len(messages),
            "actionable_messages": (
                len(buckets["high_priority"])
                + sum(len(v) for v in actionable_threads.values())
            ),
            "threads": len(actionable_threads),
        },
        "high_priority": [],
        "threads": [],
        "arrival_only": [],
    }

    if explain:
        json_out["explain"] = explain_map

    for msg in buckets["high_priority"]:
        json_out["high_priority"].append(
            {
                "message_id": msg["message_id"],
                "from": msg["sender"],
                "subject": msg["subject"],
                "excerpt": msg["extracted_new_text"],
                "timestamp_utc": msg["date_utc"],
                "has_attachments": bool(msg["has_attachments"]),
                "attachments": json.loads(msg["attachment_names"] or "[]"),
            }
        )

    for tid, msgs in actionable_threads.items():
        t = threads.get(tid, {})
        json_out["threads"].append(
            {
                "thread_id": tid,
                "participants": json.loads(t.get("participants", "[]")),
                "messages": [
                    {
                        "message_id": m["message_id"],
                        "from": m["sender"],
                        "subject": m.get("subject") or "",
                        "excerpt": m["extracted_new_text"],
                        "timestamp_utc": m["date_utc"],
                    }
                    for m in msgs
                ],
            }
        )

    for msg in buckets["arrival_only"]:
        json_out["arrival_only"].append(
            {
                "from": msg["sender"],
                "subject": msg["subject"],
                "timestamp_utc": msg["date_utc"],
            }
        )

    # ----------------------------
    # Write files
    # ----------------------------

    outdir = rootdir / f"{day.year:04d}" / f"{day.month:02d}"
    outdir.mkdir(parents=True, exist_ok=True)

    json_path = outdir / f"{day.day:02d}.json"
    md_path = outdir / f"{day.day:02d}.md"

    json_path.write_text(json.dumps(json_out, indent=2), encoding="utf-8")

    md_path.write_text(render_markdown(json_out, explain), encoding="utf-8")


# ----------------------------
# Markdown rendering
# ----------------------------


def normalize_excerpt(
    s: str,
    *,
    max_lines: int = 12,
    max_chars: int = 1500,
) -> str:
    if not s:
        return ""

    s = s.strip()

    # Cap characters first (prevents pathological HTML dumps)
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "…"

    # Normalize lines
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append("…")

    return "\n".join(lines)


def render_markdown(data: dict[str, Any], explain: bool) -> str:
    lines: list[str] = []

    lines.append(f"# MailTriage — {data['date']}")
    lines.append("_Timezone: America/New_York_")
    lines.append("")
    lines.append("---")
    lines.append("")

    # High Priority
    if data["high_priority"]:
        lines.append("## High Priority")
        lines.append("")
        for m in data["high_priority"]:
            lines.append(f"### {m['from']}")
            lines.append(f"**{m['subject']}**")
            lines.append("")
            excerpt = normalize_excerpt(m["excerpt"])
            if excerpt:
                for ln in excerpt.splitlines():
                    lines.append(f"  - {ln}")
            if m["has_attachments"]:
                lines.append(f"- Attachments: {', '.join(m['attachments'])}")
            lines.append(f"- Time: {_fmt_time(m['timestamp_utc'])}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # Threads
    if data["threads"]:
        lines.append("## Other Messages")
        lines.append("")
        for t in data["threads"]:
            subject = t["messages"][0].get("subject") or "(no subject)"
            lines.append(f"### {subject}")
            if t["participants"]:
                lines.append(f"Participants: {', '.join(t['participants'])}")
            lines.append("")
            for m in t["messages"]:
                lines.append(f"- **{_fmt_time(m['timestamp_utc'])} — {m['from']}**")
                excerpt = normalize_excerpt(m["excerpt"])
                if excerpt:
                    for ln in excerpt.splitlines():
                        lines.append(f"  - {ln}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # Arrival only
    if data["arrival_only"]:
        lines.append("## Arrivals (No Action Needed)")
        lines.append("")
        for m in data["arrival_only"]:
            lines.append(f"- {_fmt_time(m['timestamp_utc'])} — {m['subject']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Summary
    s = data["summary"]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total messages ingested: {s['total_messages']}")
    lines.append(f"- Actionable messages: {s['actionable_messages']}")
    lines.append(f"- Threads requiring response: {s['threads']}")
    lines.append("")

    return "\n".join(lines)
