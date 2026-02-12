from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os

from mailtriage.core.config import load_config
from mailtriage.core.db import Database
from mailtriage.core.notify import notify
from mailtriage.core.schema import ensure_schema_v1, verify_schema_hash
from mailtriage.core.timewindow import compute_windows
from mailtriage.ingest.ingest import SecretProviderError
from mailtriage.ingest.ingest import ingest_account
from mailtriage.render.window import render_window
from mailtriage.render.site import render_index


@dataclass(frozen=True)
class Args:
    config: Path
    days: int | None
    date: str | None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailtriage", add_help=True)
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Generate triage reports")
    run.add_argument(
        "--config",
        type=Path,
        default=Path("config.yml"),
        help="Path to config file (default: ./config.yml)",
    )

    g = run.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=None)
    g.add_argument("--date", type=str, default=None)  # YYYY-MM-DD

    return p


def _parse_utc_z(ts: str) -> datetime:
    if not ts.endswith("Z"):
        raise ValueError(f"Expected UTC Z timestamp, got: {ts}")
    return datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)


def _maybe_load_bw_session(*, output_root: Path) -> None:
    """
    Best-effort convenience: if Bitwarden CLI is used and a session token was
    previously saved to disk, load it so `bw get item` can run non-interactively.
    """
    if os.environ.get("BW_SESSION"):
        if os.environ.get("MAILTRIAGE_DEBUG"):
            sys.stderr.write("[mailtriage] BW_SESSION already set in environment\n")
        return
    override = os.environ.get("MAILTRIAGE_BW_SESSION_FILE")
    session_path = Path(override) if override else (output_root / ".mailtriage" / "bw_session")
    try:
        token = session_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        if os.environ.get("MAILTRIAGE_DEBUG"):
            sys.stderr.write(f"[mailtriage] BW session file not found: {session_path}\n")
        return
    if token:
        if os.environ.get("MAILTRIAGE_DEBUG"):
            sys.stderr.write(f"[mailtriage] Loaded BW_SESSION from: {session_path}\n")
        os.environ["BW_SESSION"] = token


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    ns = parser.parse_args(argv)

    if ns.command != "run":
        parser.error("Unsupported command")

    args = Args(
        config=ns.config,
        days=ns.days,
        date=ns.date,
    )

    cfg = load_config(args.config)

    # authoritative output root — config only
    rootdir = cfg.rootdir
    _maybe_load_bw_session(output_root=rootdir)

    # state DB location (persistent)
    db_path = cfg.state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with Database.open(db_path) as db:
        ensure_schema_v1(
            db,
            timezone=cfg.time.timezone,
            workday_start=cfg.time.workday_start,
        )
        verify_schema_hash(db)

        windows = compute_windows(
            timezone=cfg.time.timezone,
            workday_start=cfg.time.workday_start,
            days=args.days,
            date=args.date,
        )

        # optional bookkeeping
        for w in windows:
            db.record_run_window(w.start_utc, w.end_utc)

        try:
            for w in windows:
                start_dt = _parse_utc_z(w.start_utc)
                end_dt = _parse_utc_z(w.end_utc)

                for acct in cfg.accounts:
                    ingest_account(
                        db=db,
                        account_cfg=acct,
                        window_start_utc=start_dt,
                        window_end_utc=end_dt,
                    )

                render_window(
                    db=db,
                    window_start_utc=start_dt,
                    window_end_utc=end_dt,
                    rootdir=rootdir,
                    rules=cfg.rules,
                    timezone=cfg.time.timezone,
                )
        except SecretProviderError as e:
            # Best-effort: show a desktop notification in addition to the error.
            notify("MailTriage", f"Cannot fetch secrets: {e}")
            raise

        # Refresh static viewer page after rendering.
        render_index(rootdir)

    return 0
