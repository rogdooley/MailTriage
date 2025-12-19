from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mailtriage.core.config import load_config
from mailtriage.core.db import Database
from mailtriage.core.schema import ensure_schema_v1, verify_schema_hash
from mailtriage.core.timewindow import compute_windows
from mailtriage.ingest.ingest import ingest_account
from mailtriage.render.window import render_window


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

        for w in windows:
            start_utc = _parse_utc_z(w.start_utc)
            end_utc = _parse_utc_z(w.end_utc)

            # ingest per account
            for acct in cfg.accounts:
                ingest_account(
                    db=db,
                    account_cfg=acct,
                    window_start_utc=start_utc,
                    window_end_utc=end_utc,
                )

            # render exactly once per window
            render_window(
                db=db,
                window_start_utc=start_utc,
                window_end_utc=end_utc,
                rootdir=rootdir,
                rules=cfg.rules,
                timezone=cfg.time.timezone,
            )

    return 0
