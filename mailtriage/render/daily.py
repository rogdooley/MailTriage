from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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


def _fmt_time(iso_utc: str, tz_name: str) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")


def _match_any(patterns: list[str], value: str) -> bool:
    value = value.lower()
    for pat in patterns:
        if pat.lower() in value:
            return True
    return False


def format_sender(display: str | None, email: str) -> str:
    if display:
        return f"{display} <{email}>"
    return f"<{email}>"


def classify_message(msg: dict[str, Any], rules) -> str:
    """
    Returns one of:
      suppress | arrival_only | high_priority | normal
    """
    sender = msg["sender"].lower()
    subject = msg["subject"].lower()

    # suppress rules
    if any(pat.lower() in sender for pat in rules.suppress.senders) or any(
        pat.lower() in subject for pat in rules.suppress.subjects
    ):
        return "suppress"

    # arrival-only rules
    if any(pat.lower() in sender for pat in rules.arrival_only.senders) or any(
        pat.lower() in subject for pat in rules.arrival_only.subjects
    ):
        return "arrival_only"

    # HIGH PRIORITY: substring match against sender
    for hp in rules.high_priority_senders:
        if hp.lower() in sender:
            return "high_priority"

    return "normal"


def suppress_replied_inbound(messages: list[dict], me_addrs: set[str]) -> list[dict]:
    last_inbound: datetime | None = None
    last_outbound: datetime | None = None

    for m in messages:
        ts = datetime.fromisoformat(m["date_utc"].replace("Z", "+00:00"))
        if m.get("is_outbound"):
            last_outbound = max(last_outbound or ts, ts)
        else:
            last_inbound = max(last_inbound or ts, ts)

    if last_outbound and last_inbound and last_outbound >= last_inbound:
        return []

    return messages


def build_high_priority_groups(
    messages: list[dict],
    rules,
) -> dict[str, dict]:
    """
    Group high-priority inbound messages by sender.
    Suppress groups where the last inbound has already been replied to.
    """
    me_addrs = {a.lower() for a in getattr(rules, "me_addresses", [])}
    hp_senders = {s.lower() for s in rules.high_priority_senders}

    grouped: dict[str, list[dict]] = defaultdict(list)

    for msg in messages:
        sender = msg["sender"].lower()
        if sender in hp_senders:
            grouped[sender].append(msg)

    result: dict[str, dict] = {}

    for sender, msgs in grouped.items():
        msgs = sorted(
            msgs,
            key=lambda m: datetime.fromisoformat(m["date_utc"].replace("Z", "+00:00")),
        )

        msgs = suppress_replied_inbound(msgs, me_addrs)
        if not msgs:
            continue

        result[sender] = {
            "sender_email": sender,
            "sender_display": msgs[0].get("sender_display"),
            "messages": msgs,
        }

    return dict(sorted(result.items()))


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


def render_high_priority(
    groups: dict[str, dict],
    tz_name: str,
) -> list[str]:
    tz = ZoneInfo(tz_name)
    lines: list[str] = []

    if not groups:
        return lines

    lines.append("## High Priority")
    lines.append("")

    for sender_email, block in groups.items():
        sender_label = format_sender(
            block.get("sender_display"),
            sender_email,
        )

        msgs = block["messages"]

        lines.append(f"## {sender_label}")
        lines.append(f"_Messages today: {len(msgs)}_")
        lines.append("")

        for m in msgs:
            dt = datetime.fromisoformat(m["date_utc"].replace("Z", "+00:00"))
            local_time = dt.astimezone(tz).strftime("%H:%M")

            to_addrs = json.loads(m.get("to_addrs") or "[]")
            cc_addrs = json.loads(m.get("cc_addrs") or "[]")

            header_bits = []
            if to_addrs:
                header_bits.append(f"To: {', '.join(to_addrs)}")
            if cc_addrs:
                header_bits.append(f"CC: {', '.join(cc_addrs)}")
            header_bits.append(local_time)

            lines.append(f"**{m['subject'] or '(no subject)'}**")
            lines.append(f"_{' • '.join(header_bits)}_")

            excerpt = normalize_excerpt(m["extracted_new_text"])
            if excerpt:
                for ln in excerpt.splitlines():
                    lines.append(f"- {ln}")

            lines.append("")

        lines.append("---")
        lines.append("")

    return lines


def render_day(
    *,
    db,
    day: date,
    rootdir: Path,
    rules,
    timezone: str,
    explain: bool = False,
) -> None:
    messages = load_messages_for_day(db, day)
    threads = load_threads_for_messages(db, messages)

    explain_map: dict[str, str] = {}

    high_priority_msgs: list[dict] = []
    arrival_only_msgs: list[dict] = []
    normal_msgs: list[dict] = []

    for msg in messages:
        cls = classify_message(msg, rules)

        if cls == "suppress":
            continue
        elif cls == "high_priority":
            high_priority_msgs.append(msg)
        elif cls == "arrival_only":
            arrival_only_msgs.append(msg)
        else:
            normal_msgs.append(msg)

    # ---- Thread grouping (normal only) ----

    high_priority_thread_ids: set[str] = {
        msg["thread_id"] for msg in high_priority_msgs if msg.get("thread_id")
    }

    threads_grouped = defaultdict(list)
    for msg in normal_msgs:
        tid = msg.get("thread_id")
        if not tid:
            continue

        # HARD EXCLUSION: threads with any high-priority sender
        if tid in high_priority_thread_ids:
            continue

        threads_grouped[tid].append(msg)

    actionable_threads = {}
    for tid, msgs in threads_grouped.items():
        t = threads.get(tid)
        if not t:
            actionable_threads[tid] = msgs
            continue

        last_in = t.get("last_inbound_at_utc")
        last_out = t.get("last_outbound_at_utc")

        if last_out and last_in and last_out >= last_in:
            continue

        actionable_threads[tid] = msgs

    # ---- High-priority groups (correct count) ----

    hp_groups = build_high_priority_groups(
        messages=high_priority_msgs,
        rules=rules,
    )

    json_out: dict[str, Any] = {
        "date": day.isoformat(),
        "summary": {
            "total_messages": len(messages),
            "actionable_messages": len(hp_groups) + len(actionable_threads),
            "threads": len(actionable_threads),
        },
        "threads": [],
        "arrival_only": [],
    }

    if explain:
        json_out["explain"] = explain_map

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

    for msg in arrival_only_msgs:
        json_out["arrival_only"].append(
            {
                "from": msg["sender"],
                "subject": msg["subject"],
                "timestamp_utc": msg["date_utc"],
            }
        )

    outdir = rootdir / f"{day.year:04d}" / f"{day.month:02d}"
    outdir.mkdir(parents=True, exist_ok=True)

    (outdir / f"{day.day:02d}.json").write_text(
        json.dumps(json_out, indent=2), encoding="utf-8"
    )

    (outdir / f"{day.day:02d}.md").write_text(
        render_markdown(
            json_out,
            explain,
            tz_name=timezone,
            rules=rules,
            all_messages=messages,
        ),
        encoding="utf-8",
    )


# ----------------------------
# Markdown rendering
# ----------------------------


def normalize_excerpt(s: str) -> str:
    if not s:
        return ""

    lines_out: list[str] = []
    for raw in s.splitlines():
        ln = raw.strip()
        if not ln:
            break  # stop at first blank line

        low = ln.lower()

        # quoted history
        if ln.startswith(">"):
            break
        if low.startswith("on ") and "wrote:" in low:
            break

        # signatures
        if ln == "--":
            break
        if low in {"thanks,", "thank you,", "best,", "regards,"}:
            break

        lines_out.append(ln)

        if len(lines_out) == 3:
            break

    return "\n".join(lines_out)


def render_markdown(
    data: dict[str, Any],
    explain: bool,
    *,
    tz_name: str,
    rules,
    all_messages: list[dict],
) -> str:
    lines: list[str] = []

    lines.append(f"# MailTriage — {data['date']}")
    lines.append(f"_Timezone: {tz_name}_")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- High Priority ----

    hp_groups = build_high_priority_groups(all_messages, rules)
    lines.extend(render_high_priority(hp_groups, tz_name))

    # ---- Other Threads ----

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
                lines.append(
                    f"- **{_fmt_time(m['timestamp_utc'], tz_name)} — {m['from']}**"
                )
                excerpt = normalize_excerpt(m["excerpt"])
                if excerpt:
                    for ln in excerpt.splitlines():
                        lines.append(f"  - {ln}")

            lines.append("")
        lines.append("---")
        lines.append("")

    # ---- Arrival Only ----

    if data["arrival_only"]:
        lines.append("## Arrivals (No Action Needed)")
        lines.append("")
        for m in data["arrival_only"]:
            lines.append(f"- {_fmt_time(m['timestamp_utc'], tz_name)} — {m['subject']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ---- Summary ----

    s = data["summary"]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total messages ingested: {s['total_messages']}")
    lines.append(f"- Actionable messages: {s['actionable_messages']}")
    lines.append(f"- Threads requiring response: {s['threads']}")
    lines.append("")

    return "\n".join(lines)
