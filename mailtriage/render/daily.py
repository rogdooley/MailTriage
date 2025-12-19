# mailtriage/render/daily.py
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import getaddresses
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ----------------------------
# DB helpers
# ----------------------------


def _query_all(db, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = db.conn.execute(sql, params)
    rows = cur.fetchall()
    return [dict(row) for row in rows]


# ----------------------------
# Time helpers
# ----------------------------


def _parse_utc_iso(iso_utc: str) -> datetime:
    # Stored as "YYYY-MM-DDTHH:MM:SSZ"
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_time(iso_utc: str, tz_name: str) -> str:
    dt = _parse_utc_iso(iso_utc)
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")


# ----------------------------
# Sender/Subject normalization
# ----------------------------

_EMAIL_IN_ANGLE_RE = re.compile(r"<([^>]+)>")


def normalize_sender_email(sender: str) -> str:
    """
    Accepts:
      - "Name <email@x>"
      - "<email@x>"
      - "email@x"
    Returns the lowercased email if we can extract it, else a lowercased fallback.
    """
    sender = (sender or "").strip()
    if not sender:
        return ""
    m = _EMAIL_IN_ANGLE_RE.search(sender)
    if m:
        return m.group(1).strip().lower()

    # email.utils can parse a lot of weirdness
    parsed = getaddresses([sender])
    if parsed and parsed[0][1]:
        return parsed[0][1].strip().lower()

    return sender.lower()


def parse_sender_display_email(sender: str) -> tuple[str | None, str]:
    sender = (sender or "").strip()
    if not sender:
        return None, ""
    parsed = getaddresses([sender])
    if parsed and parsed[0][1]:
        name = (parsed[0][0] or "").strip() or None
        email = parsed[0][1].strip().lower()
        return name, email
    # fallback
    return None, normalize_sender_email(sender)


def format_sender(display: str | None, email: str) -> str:
    if display:
        return f"{display} <{email}>"
    return f"<{email}>"


def decode_and_normalize_subject(value: Any) -> str:
    """
    Decodes RFC 2047 encoded-words like:
      =?UTF-8?B?...?=
      =?UTF-8?Q?...?=
    Then collapses whitespace.
    """
    if not value:
        return ""

    if not isinstance(value, str):
        value = str(value)

    parts: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            try:
                parts.append(part.decode(charset or "utf-8", errors="replace"))
            except Exception:
                parts.append(part.decode("utf-8", errors="replace"))
        else:
            parts.append(part)

    s = "".join(parts)
    s = " ".join(s.split())
    return s.strip()


# ----------------------------
# Rules helpers
# ----------------------------


def _contains_any(substrs: list[str], value: str) -> bool:
    v = (value or "").lower()
    for s in substrs:
        if (s or "").lower() in v:
            return True
    return False


def classify_message(msg: dict[str, Any], rules) -> str:
    """
    Returns one of:
      suppress | arrival_only | high_priority | normal
    """
    sender_raw = msg.get("sender") or ""
    sender_email = normalize_sender_email(sender_raw)

    subject = decode_and_normalize_subject(msg.get("subject") or "")
    subject_l = subject.lower()

    # suppress
    if _contains_any(getattr(rules.suppress, "senders", []), sender_raw):
        return "suppress"
    if _contains_any(getattr(rules.suppress, "subjects", []), subject_l):
        return "suppress"

    # arrival_only
    if _contains_any(getattr(rules.arrival_only, "senders", []), sender_raw):
        return "arrival_only"
    if _contains_any(getattr(rules.arrival_only, "subjects", []), subject_l):
        return "arrival_only"

    # high priority (email match, not full "Name <email>")
    hp = {s.lower() for s in getattr(rules, "high_priority_senders", [])}
    if sender_email in hp:
        return "high_priority"

    return "normal"


# ----------------------------
# Loading
# ----------------------------


def load_messages_for_window(
    db,
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> list[dict[str, Any]]:
    if window_start_utc.tzinfo is None:
        window_start_utc = window_start_utc.replace(tzinfo=timezone.utc)
    if window_end_utc.tzinfo is None:
        window_end_utc = window_end_utc.replace(tzinfo=timezone.utc)

    rows = _query_all(
        db,
        """
        SELECT *
        FROM messages
        WHERE date_utc >= ?
          AND date_utc < ?
        ORDER BY date_utc ASC
        """,
        (
            window_start_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_end_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )

    msgs = [dict(r) for r in rows]

    # Normalize subjects in-memory (no DB mutation)
    for m in msgs:
        m["subject"] = decode_and_normalize_subject(m.get("subject") or "")

    return msgs


def load_threads_for_messages(
    db, messages: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    thread_ids = {m.get("thread_id") for m in messages if m.get("thread_id")}
    thread_ids.discard(None)
    if not thread_ids:
        return {}

    placeholders = ",".join("?" for _ in thread_ids)
    rows = _query_all(
        db,
        f"SELECT * FROM threads WHERE thread_id IN ({placeholders})",
        tuple(thread_ids),
    )
    return {r["thread_id"]: dict(r) for r in rows}


# ----------------------------
# Thread suppression logic
# ----------------------------


def _msg_is_outbound(m: dict[str, Any]) -> bool:
    # support either schema
    if "outbound" in m:
        return bool(m.get("outbound"))
    if "is_outbound" in m:
        return bool(m.get("is_outbound"))
    return False


def suppress_replied_inbound(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    If the last outbound message timestamp >= last inbound timestamp, suppress the whole group.
    """
    last_inbound: datetime | None = None
    last_outbound: datetime | None = None

    for m in messages:
        ts = _parse_utc_iso(str(m["date_utc"]))
        if _msg_is_outbound(m):
            last_outbound = max(last_outbound or ts, ts)
        else:
            last_inbound = max(last_inbound or ts, ts)

    if last_outbound and last_inbound and last_outbound >= last_inbound:
        return []

    return messages


def build_high_priority_groups(
    messages: list[dict[str, Any]], rules
) -> dict[str, dict[str, Any]]:
    """
    Group high-priority messages by sender_email. Suppress groups already replied to.
    """
    hp = {s.lower() for s in getattr(rules, "high_priority_senders", [])}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for msg in messages:
        sender_raw = msg.get("sender") or ""
        sender_email = normalize_sender_email(sender_raw)
        if sender_email in hp:
            grouped[sender_email].append(msg)

    result: dict[str, dict[str, Any]] = {}

    for sender_email, msgs in grouped.items():
        msgs = sorted(msgs, key=lambda m: _parse_utc_iso(str(m["date_utc"])))
        msgs = suppress_replied_inbound(msgs)
        if not msgs:
            continue

        display, _ = parse_sender_display_email(msgs[0].get("sender") or "")
        result[sender_email] = {
            "sender_email": sender_email,
            "sender_display": display,
            "messages": msgs,
        }

    return dict(sorted(result.items(), key=lambda kv: kv[0]))


# ----------------------------
# Excerpt normalization
# ----------------------------

_SIGNATURE_STOP = {"thanks,", "thank you,", "best,", "regards,"}


def normalize_excerpt(s: str) -> str:
    """
    Your rule:
      - first blank line OR
      - at most 3 lines
      - drop quoted history + common signature starters
    """
    if not s:
        return ""

    lines_out: list[str] = []

    for raw in str(s).splitlines():
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
        if low in _SIGNATURE_STOP:
            break

        lines_out.append(ln)
        if len(lines_out) >= 3:
            break

    return "\n".join(lines_out)


# ----------------------------
# Markdown rendering
# ----------------------------


def _load_to_cc(m: dict[str, Any]) -> tuple[list[str], list[str]]:
    # Support both column name styles you've had in-flight
    to_raw = m.get("recipients_to") or m.get("to_addrs") or "[]"
    cc_raw = m.get("recipients_cc") or m.get("cc_addrs") or "[]"
    try:
        to_addrs = json.loads(to_raw) if isinstance(to_raw, str) else list(to_raw)
    except Exception:
        to_addrs = []
    try:
        cc_addrs = json.loads(cc_raw) if isinstance(cc_raw, str) else list(cc_raw)
    except Exception:
        cc_addrs = []
    return [str(x) for x in to_addrs if x], [str(x) for x in cc_addrs if x]


def render_high_priority(groups: dict[str, dict[str, Any]], tz_name: str) -> list[str]:
    tz = ZoneInfo(tz_name)
    lines: list[str] = []

    if not groups:
        return lines

    lines.append("## High Priority")
    lines.append("")

    for sender_email, block in groups.items():
        sender_label = format_sender(block.get("sender_display"), sender_email)
        msgs: list[dict[str, Any]] = block["messages"]

        lines.append(f"### {sender_label}")
        lines.append(f"_Messages: {len(msgs)}_")
        lines.append("")

        for m in msgs:
            dt = _parse_utc_iso(str(m["date_utc"]))
            local_time = dt.astimezone(tz).strftime("%H:%M")

            to_addrs, cc_addrs = _load_to_cc(m)

            header_bits: list[str] = []
            if to_addrs:
                header_bits.append(f"To: {', '.join(to_addrs)}")
            if cc_addrs:
                header_bits.append(f"CC: {', '.join(cc_addrs)}")
            header_bits.append(local_time)

            subj = m.get("subject") or "(no subject)"
            lines.append(f"**{subj}**")
            lines.append(f"_{' • '.join(header_bits)}_")

            excerpt = normalize_excerpt(m.get("extracted_new_text") or "")
            if excerpt:
                for ln in excerpt.splitlines():
                    lines.append(f"- {ln}")

            lines.append("")

        lines.append("---")
        lines.append("")

    return lines


def render_markdown(
    data: dict[str, Any],
    *,
    tz_name: str,
    window_label: str,
    rules,
    all_messages: list[dict[str, Any]],
) -> str:
    lines: list[str] = []

    lines.append(f"# MailTriage — {window_label}")
    lines.append(f"_Timezone: {tz_name}_")
    lines.append("")
    lines.append("---")
    lines.append("")

    # High Priority
    hp_groups = build_high_priority_groups(all_messages, rules)
    lines.extend(render_high_priority(hp_groups, tz_name))

    # Other Messages
    if data["threads"]:
        lines.append("## Other Messages")
        lines.append("")

        for t in data["threads"]:
            subject = t["messages"][0].get("subject") or "(no subject)"
            lines.append(f"### {subject}")

            if t.get("participants"):
                lines.append(f"Participants: {', '.join(t['participants'])}")

            lines.append("")

            for m in t["messages"]:
                sender_display = m.get("from") or ""
                lines.append(
                    f"- **{_fmt_time(m['timestamp_utc'], tz_name)} — {sender_display}**"
                )

                excerpt = normalize_excerpt(m.get("excerpt") or "")
                if excerpt:
                    for ln in excerpt.splitlines():
                        lines.append(f"  - {ln}")

            lines.append("")

        lines.append("---")
        lines.append("")

    # Arrivals
    if data["arrival_only"]:
        lines.append("## Arrivals (No Action Needed)")
        lines.append("")
        for m in data["arrival_only"]:
            lines.append(f"- {_fmt_time(m['timestamp_utc'], tz_name)} — {m['subject']}")
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


# ----------------------------
# Main entry: render a window
# ----------------------------


@dataclass(frozen=True)
class RenderOutputs:
    json_path: Path
    md_path: Path


def render_window(
    *,
    db,
    window_start_utc: datetime,
    window_end_utc: datetime,
    rootdir: Path,
    rules,
    timezone: str,
    explain: bool = False,
) -> RenderOutputs:
    """
    Renders ONE rolling time window (e.g. 09:00 → 09:00 local) into:
      rootdir/YYYY/MM/DD.json
      rootdir/YYYY/MM/DD.md

    The date used for the filename is the *local date of window_start*.
    """
    tz = ZoneInfo(timezone)

    messages = load_messages_for_window(
        db,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
    )
    threads = load_threads_for_messages(db, messages)

    # Determine which threads are high-priority (thread-wide)
    hp = {s.lower() for s in getattr(rules, "high_priority_senders", [])}
    high_priority_thread_ids: set[str] = {
        str(m["thread_id"])
        for m in messages
        if m.get("thread_id") and normalize_sender_email(m.get("sender") or "") in hp
    }

    high_priority_msgs: list[dict[str, Any]] = []
    arrival_only_msgs: list[dict[str, Any]] = []
    normal_msgs: list[dict[str, Any]] = []

    explain_map: dict[str, str] = {}

    for msg in messages:
        cls = classify_message(msg, rules)
        if explain:
            explain_map[str(msg.get("message_id"))] = cls

        if cls == "suppress":
            continue
        if cls == "high_priority":
            high_priority_msgs.append(msg)
        elif cls == "arrival_only":
            arrival_only_msgs.append(msg)
        else:
            normal_msgs.append(msg)

    # Group normal messages into threads, but HARD-EXCLUDE any thread that contains a high-priority sender
    threads_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for msg in normal_msgs:
        tid = msg.get("thread_id")
        if not tid:
            continue
        if tid in high_priority_thread_ids:
            continue
        threads_grouped[str(tid)].append(msg)

    # Suppress threads already replied to (thread table)
    actionable_threads: dict[str, list[dict[str, Any]]] = {}
    for tid, msgs in threads_grouped.items():
        t = threads.get(tid)
        if not t:
            actionable_threads[tid] = msgs
            continue

        last_in = t.get("last_inbound_at_utc")
        last_out = t.get("last_outbound_at_utc")

        if last_out and last_in and str(last_out) >= str(last_in):
            continue  # already replied

        actionable_threads[tid] = msgs

    # High-priority groups are sender-grouped and reply-suppressed
    hp_groups = build_high_priority_groups(high_priority_msgs, rules)

    # Build JSON
    json_out: dict[str, Any] = {
        "window_start_utc": window_start_utc.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "window_end_utc": window_end_utc.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
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

    # Threads
    for tid, msgs in actionable_threads.items():
        t = threads.get(tid, {})
        json_out["threads"].append(
            {
                "thread_id": tid,
                "participants": json.loads(t.get("participants", "[]"))
                if t.get("participants")
                else [],
                "messages": [
                    {
                        "message_id": m.get("message_id"),
                        "from": m.get("sender") or "",
                        "subject": m.get("subject") or "",
                        "excerpt": m.get("extracted_new_text") or "",
                        "timestamp_utc": m.get("date_utc"),
                    }
                    for m in msgs
                ],
            }
        )

    # Arrival-only
    for msg in arrival_only_msgs:
        json_out["arrival_only"].append(
            {
                "from": msg.get("sender") or "",
                "subject": msg.get("subject") or "",
                "timestamp_utc": msg.get("date_utc"),
            }
        )

    # Output paths based on local start date
    local_start = window_start_utc.astimezone(tz)
    window_label = f"{local_start.strftime('%Y-%m-%d')} ({local_start.strftime('%H:%M')}–{window_end_utc.astimezone(tz).strftime('%H:%M')})"

    outdir = rootdir / f"{local_start.year:04d}" / f"{local_start.month:02d}"
    outdir.mkdir(parents=True, exist_ok=True)

    json_path = outdir / f"{local_start.day:02d}.json"
    md_path = outdir / f"{local_start.day:02d}.md"

    json_path.write_text(json.dumps(json_out, indent=2), encoding="utf-8")

    md_path.write_text(
        render_markdown(
            json_out,
            tz_name=timezone,
            window_label=window_label,
            rules=rules,
            all_messages=messages,
        ),
        encoding="utf-8",
    )

    return RenderOutputs(json_path=json_path, md_path=md_path)
