from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mailtriage.core.notify import notify
from mailtriage.watch.unreplied import UnrepliedThread, find_unreplied_threads


@dataclass(frozen=True)
class UnrepliedRule:
    id: str
    target_addresses: list[str]
    unreplied_after_minutes: int
    lookback_days: int
    notify_cooldown_minutes: int


@dataclass(frozen=True)
class UnrepliedWatchConfig:
    enabled: bool
    rules: list[UnrepliedRule]
    max_items_in_notification: int = 5
    output_root: Path | None = None


def _entity_type(rule_id: str) -> str:
    return f"watch_unreplied:{rule_id}"


def _parse_iso_z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _get_last_notified_at_utc(db, *, rule_id: str, thread_id: str) -> datetime | None:
    row = db.query_one(
        "SELECT updated_at_utc FROM triage_state WHERE entity_type=? AND entity_id=?",
        (_entity_type(rule_id), thread_id),
    )
    if row is None:
        return None
    try:
        return _parse_iso_z(str(row[0]))
    except Exception:
        return None


def _upsert_notified(db, *, rule_id: str, thread: UnrepliedThread) -> None:
    state = json.dumps(
        {
            "status": "open",
            "date_utc": thread.date_utc,
            "sender": thread.sender,
            "subject": thread.subject,
        }
    )
    db.exec(
        """
        INSERT INTO triage_state (entity_id, entity_type, state, updated_at_utc)
        VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        ON CONFLICT(entity_id, entity_type) DO UPDATE SET
          state=excluded.state,
          updated_at_utc=excluded.updated_at_utc
        """,
        (thread.thread_id, _entity_type(rule_id), state),
    )


def _write_watch_html(*, output_root: Path, by_rule: dict[str, list[UnrepliedThread]]) -> Path:
    out_dir = output_root / "watch"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "unreplied.html"

    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    sections: list[str] = []
    for rule_id, items in by_rule.items():
        if not items:
            continue
        rows: list[str] = []
        # Newest first for scanning.
        for t in sorted(items, key=lambda x: x.date_utc, reverse=True):
            subj = esc(t.subject or "(no subject)")
            sender = esc(t.sender or "(unknown sender)")
            dt = esc(t.date_utc)
            tid = esc(t.thread_id)
            rows.append(
                f"<tr><td class='subj'>{subj}</td><td>{sender}</td><td class='dt'>{dt}</td><td class='tid'>{tid}</td></tr>"
            )
        sections.append(
            f"""
            <section class="rule">
              <h2>{esc(rule_id)}</h2>
              <table>
                <thead>
                  <tr><th>Subject</th><th>From</th><th>Date (UTC)</th><th>Thread</th></tr>
                </thead>
                <tbody>
                  {''.join(rows)}
                </tbody>
              </table>
            </section>
            """
        )

    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>MailTriage Watch: Unreplied</title>
    <style>
      :root {{
        --bg: #0b1020;
        --panel: rgba(17, 26, 51, 0.9);
        --text: #e8ecff;
        --muted: rgba(232, 236, 255, 0.7);
        --border: rgba(232, 236, 255, 0.16);
      }}
      html, body {{ height: 100%; }}
      body {{
        margin: 0;
        color: var(--text);
        background: radial-gradient(1100px 500px at 20% 0%, rgba(125, 159, 255, 0.35), transparent 70%),
                    radial-gradient(900px 500px at 95% 15%, rgba(79, 255, 202, 0.18), transparent 60%),
                    var(--bg);
        font: 14px/1.35 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      }}
      header {{
        position: sticky;
        top: 0;
        backdrop-filter: blur(10px);
        background: color-mix(in srgb, var(--bg) 75%, transparent);
        border-bottom: 1px solid var(--border);
        padding: 16px 18px;
      }}
      header h1 {{ margin: 0; font-size: 16px; letter-spacing: 0.2px; }}
      header p {{ margin: 6px 0 0; color: var(--muted); }}
      main {{ max-width: 1100px; margin: 0 auto; padding: 14px 18px 42px; }}
      .rule {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 14px;
        margin: 12px 0;
        box-shadow: 0 20px 70px rgba(0,0,0,0.35);
      }}
      .rule h2 {{ margin: 0 0 10px; font-size: 15px; }}
      table {{ width: 100%; border-collapse: collapse; }}
      thead th {{
        text-align: left;
        font-size: 12px;
        color: var(--muted);
        border-bottom: 1px solid var(--border);
        padding: 8px 8px;
      }}
      tbody td {{
        border-bottom: 1px solid rgba(232,236,255,0.08);
        padding: 10px 8px;
        vertical-align: top;
      }}
      tbody tr:last-child td {{ border-bottom: none; }}
      .subj {{ font-weight: 650; }}
      .dt, .tid {{
        font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        color: var(--muted);
        white-space: nowrap;
      }}
      .tid {{ max-width: 360px; overflow: hidden; text-overflow: ellipsis; }}
      .empty {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 14px;
        margin: 12px 0;
        color: var(--muted);
      }}
    </style>
  </head>
  <body>
    <header>
      <h1>MailTriage Watch: Unreplied</h1>
      <p>This page is updated by the hourly watcher when it finds threads that may need a reply.</p>
    </header>
    <main>
      {''.join(sections) if sections else "<div class='empty'>No unreplied threads found.</div>"}
    </main>
  </body>
</html>
"""

    out_path.write_text(html, encoding="utf-8")
    return out_path


def run_unreplied_watch(*, db, cfg: UnrepliedWatchConfig, now_utc: datetime | None = None) -> int:
    if not cfg.enabled:
        return 0

    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc

    total_notified = 0
    notified_by_rule: dict[str, list[UnrepliedThread]] = {}
    for rule in cfg.rules:
        if not rule.target_addresses:
            continue

        candidates = find_unreplied_threads(
            db=db,
            target_addresses=rule.target_addresses,
            lookback_days=rule.lookback_days,
            unreplied_after_minutes=rule.unreplied_after_minutes,
            now_utc=now_utc,
        )

        # Dedupe by cooldown using triage_state updated_at_utc.
        to_notify: list[UnrepliedThread] = []
        cooldown = timedelta(minutes=max(1, int(rule.notify_cooldown_minutes or 60)))

        for t in candidates:
            last = _get_last_notified_at_utc(db, rule_id=rule.id, thread_id=t.thread_id)
            if last is not None and now_utc - last < cooldown:
                continue
            to_notify.append(t)

        if not to_notify:
            continue

        # Update state for everything we notify.
        for t in to_notify:
            _upsert_notified(db, rule_id=rule.id, thread=t)

        total_notified += len(to_notify)
        notified_by_rule[rule.id] = list(to_notify)

        # One notification per rule per run; include a handful of subjects.
        top = to_notify[-cfg.max_items_in_notification :]
        lines = [
            f"[{rule.id}] {len(to_notify)} thread(s) may need a reply (SLA {rule.unreplied_after_minutes}m)."
        ]
        for t in reversed(top):
            subj = t.subject or "(no subject)"
            lines.append(f"- {subj} ({t.sender})")

        open_url = None
        if cfg.output_root:
            try:
                watch_page = _write_watch_html(output_root=cfg.output_root, by_rule=notified_by_rule)
                open_url = watch_page.as_uri()
            except Exception:
                open_url = None

        notify("MailTriage", "\n".join(lines), open_url=open_url)

    return total_notified
