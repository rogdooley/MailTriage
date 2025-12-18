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


@dataclass(frozen=True)
class Args:
    config: Path
    days: int | None
    date: str | None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailtriage", add_help=True)
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Generate daily triage reports")
    run.add_argument("--config", type=Path, default=Path("config.yml"))
    g = run.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=None)
    g.add_argument("--date", type=str, default=None)  # YYYY-MM-DD

    return p


def _parse_utc_z(ts: str) -> datetime:
    # Accepts "YYYY-MM-DDTHH:MM:SSZ"
    if not ts.endswith("Z"):
        raise ValueError(f"Expected UTC Z timestamp, got: {ts}")
    base = ts[:-1]
    dt = datetime.fromisoformat(base)
    return dt.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = build_parser()
    ns = p.parse_args(argv)

    if ns.command != "run":
        p.error("Unsupported command")

    args = Args(config=ns.config, days=ns.days, date=ns.date)

    cfg = load_config(args.config)

    db_path = cfg.state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with Database.open(db_path) as db:
        ensure_schema_v1(
            db, timezone=cfg.time.timezone, workday_start=cfg.time.workday_start
        )
        verify_schema_hash(db)

        windows = compute_windows(
            timezone=cfg.time.timezone,
            workday_start=cfg.time.workday_start,
            days=args.days,
            date=args.date,
        )

        # bookkeeping only (optional)
        for w in windows:
            db.record_run_window(w.start_utc, w.end_utc)

        # ingestion
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

    return 0
