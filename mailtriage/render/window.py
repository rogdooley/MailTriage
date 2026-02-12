from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from email.utils import getaddresses
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from mailtriage.render.md_to_html import write_report_html

# ----------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------


def _query_all(db, sql: str, params: tuple = ()) -> list[dict]:
    cur = db.conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def load_messages_for_window(
    db,
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> list[dict]:
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
            window_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    return rows


def load_threads(db, messages: list[dict]) -> dict[str, dict]:
    tids = {m["thread_id"] for m in messages if m.get("thread_id")}
    if not tids:
        return {}

    qs = ",".join("?" for _ in tids)
    rows = _query_all(
        db,
        f"SELECT * FROM threads WHERE thread_id IN ({qs})",
        tuple(tids),
    )
    return {r["thread_id"]: r for r in rows}


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------


def _norm_email(addr: str) -> str:
    return addr.strip().lower()


def classify(msg: dict, rules) -> str:
    sender = msg.get("sender", "").lower()
    subject = (msg.get("subject") or "").lower()

    for p in rules.suppress.senders:
        if p.lower() in sender:
            return "suppress"
    for p in rules.suppress.subjects:
        if p.lower() in subject:
            return "suppress"

    for p in rules.arrival_only.senders:
        if p.lower() in sender:
            return "arrival_only"
    for p in rules.arrival_only.subjects:
        if p.lower() in subject:
            return "arrival_only"

    for hp in rules.high_priority_senders:
        if _norm_email(hp) == _norm_email(sender):
            return "high_priority"

    return "normal"


# ----------------------------------------------------------------------
# High priority grouping (INBOUND ONLY)
# ----------------------------------------------------------------------


def _parse_sender(sender: str) -> tuple[str | None, str]:
    parsed = getaddresses([sender])
    if not parsed:
        return None, sender
    name, email = parsed[0]
    return name or None, email.lower()


def build_high_priority_groups(messages: list[dict], rules) -> dict[str, dict]:
    hp_set = {_norm_email(x) for x in rules.high_priority_senders}

    grouped: dict[str, list[dict]] = defaultdict(list)

    for m in messages:
        if not m.get("inbound"):
            continue

        _, email = _parse_sender(m["sender"])
        if email in hp_set:
            grouped[email].append(m)

    out: dict[str, dict] = {}

    for email, msgs in grouped.items():
        msgs.sort(key=lambda m: m["date_utc"])
        display, _ = _parse_sender(msgs[-1]["sender"])

        out[email] = {
            "email": email,
            "display": display,
            "messages": msgs,
        }

    return out


# ----------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------


def _fmt_time(iso_utc: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(tz).strftime("%H:%M")


def normalize_excerpt(text: str) -> str:
    if not text:
        return ""

    lines = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            break
        if ln.startswith(">"):
            break
        if ln.lower().startswith("on ") and "wrote:" in ln.lower():
            break
        lines.append(ln)
        if len(lines) == 3:
            break

    return "\n".join(lines)


def format_sender(display: str | None, email: str) -> str:
    return f"{display} <{email}>" if display else f"<{email}>"

def _parse_json_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str):
        try:
            data = json.loads(value)
            if isinstance(data, list):
                return [str(x) for x in data if x]
        except Exception:
            return []
    return []


def _fmt_addrs(addrs: list[str], *, max_items: int = 4) -> str:
    if not addrs:
        return ""
    shown = addrs[:max_items]
    extra = len(addrs) - len(shown)
    if extra > 0:
        return ", ".join(shown) + f" (+{extra})"
    return ", ".join(shown)


# ----------------------------------------------------------------------
# MAIN ENTRYPOINT
# ----------------------------------------------------------------------


def render_window(
    *,
    db,
    window_start_utc: datetime,
    window_end_utc: datetime,
    rootdir: Path,
    rules,
    timezone: str,
) -> None:
    tz = ZoneInfo(timezone)

    # ------------------------------------------------------------
    # Load messages strictly within window
    # ------------------------------------------------------------

    messages = load_messages_for_window(
        db,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
    )
    threads = load_threads(db, messages)

    hp_msgs: list[dict] = []
    normal_msgs: list[dict] = []
    arrival_msgs: list[dict] = []

    for m in messages:
        cls = classify(m, rules)
        if cls == "suppress":
            continue
        if cls == "high_priority":
            hp_msgs.append(m)
        elif cls == "arrival_only":
            arrival_msgs.append(m)
        else:
            normal_msgs.append(m)

    # ------------------------------------------------------------
    # Thread handling
    # ------------------------------------------------------------

    # Any thread containing HP senders is excluded from "Other"
    hp_thread_ids = {m["thread_id"] for m in hp_msgs if m.get("thread_id")}

    grouped_threads: dict[str, list[dict]] = defaultdict(list)

    for m in normal_msgs:
        tid = m.get("thread_id")
        if not tid or tid in hp_thread_ids:
            continue
        grouped_threads[tid].append(m)

    actionable_threads: dict[str, list[dict]] = {}

    for tid, msgs in grouped_threads.items():
        t = threads.get(tid)
        if not t:
            actionable_threads[tid] = msgs
            continue

        last_in = t.get("last_inbound_at_utc")
        last_out = t.get("last_outbound_at_utc")

        if last_out and last_in and last_out >= last_in:
            continue  # already replied

        actionable_threads[tid] = msgs

    hp_groups = build_high_priority_groups(hp_msgs, rules)

    # ------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------

    start_local = window_start_utc.astimezone(tz)
    end_local = window_end_utc.astimezone(tz)

    lines: list[str] = []

    lines.append(
        f"# MailTriage — "
        f"{start_local.strftime('%Y-%m-%d %H:%M')} → "
        f"{end_local.strftime('%Y-%m-%d %H:%M')} "
        f"({timezone})"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- High Priority ----

    if hp_groups:
        lines.append("## High Priority")
        lines.append("")

        for block in hp_groups.values():
            sender_label = format_sender(
                block.get("display"),
                block.get("email"),
            )
            lines.append(f"### {sender_label}")
            lines.append("")

            for m in block["messages"]:
                t = _fmt_time(m["date_utc"], tz)
                lines.append(f"**{m.get('subject') or '(no subject)'}**")
                lines.append(f"_{t}_")

                to_addrs = _parse_json_list(m.get("recipients_to"))
                cc_addrs = _parse_json_list(m.get("recipients_cc"))
                if m.get("sender"):
                    lines.append(f"- From: {m['sender']}")
                if to_addrs:
                    lines.append(f"- To: {_fmt_addrs(to_addrs)}")
                if cc_addrs:
                    lines.append(f"- Cc: {_fmt_addrs(cc_addrs)}")

                ex = normalize_excerpt(m["extracted_new_text"])
                if ex:
                    for ln in ex.splitlines():
                        lines.append(f"- {ln}")
                lines.append("")

        lines.append("---")
        lines.append("")

    # ---- Other Messages ----

    if actionable_threads:
        lines.append("## Other Messages")
        lines.append("")

        for msgs in actionable_threads.values():
            subj = msgs[0].get("subject") or "(no subject)"
            lines.append(f"### {subj}")
            lines.append("")

            for m in msgs:
                t = _fmt_time(m["date_utc"], tz)
                lines.append(f"- **{t} — {m['sender']}**")

                to_addrs = _parse_json_list(m.get("recipients_to"))
                cc_addrs = _parse_json_list(m.get("recipients_cc"))
                if to_addrs:
                    lines.append(f"  - To: {_fmt_addrs(to_addrs)}")
                if cc_addrs:
                    lines.append(f"  - Cc: {_fmt_addrs(cc_addrs)}")

                ex = normalize_excerpt(m["extracted_new_text"])
                if ex:
                    for ln in ex.splitlines():
                        lines.append(f"  - {ln}")

            lines.append("")

        lines.append("---")
        lines.append("")

    # ---- Arrival Only ----

    if arrival_msgs:
        lines.append("## Arrivals (No Action Needed)")
        lines.append("")
        for m in arrival_msgs:
            t = _fmt_time(m["date_utc"], tz)
            subj = m.get("subject") or "(no subject)"
            sender = m.get("sender") or ""
            if sender:
                lines.append(f"- {t} — {subj} ({sender})")
            else:
                lines.append(f"- {t} — {subj}")
        lines.append("")

    # ------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------

    outdir = rootdir / start_local.strftime("%Y/%m")
    outdir.mkdir(parents=True, exist_ok=True)

    day = start_local.day
    md_path = outdir / f"{day:02d}.md"
    html_path = outdir / f"{day:02d}.html"

    md_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
    write_report_html(md_path, html_path)
