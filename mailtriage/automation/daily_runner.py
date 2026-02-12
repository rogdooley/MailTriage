from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import yaml

from mailtriage.cli import main as mailtriage_main
from mailtriage.core.config import load_config


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("workday_start must be HH:MM")
    hh = int(parts[0])
    mm = int(parts[1])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("workday_start out of range")
    return hh, mm


def _window_label_for_now(now_local: datetime, workday_start: str) -> date:
    hh, mm = _parse_hhmm(workday_start)
    today = now_local.date()
    today_start = datetime.combine(today, time(hh, mm), tzinfo=now_local.tzinfo)

    if now_local >= today_start:
        return today - timedelta(days=1)
    return today - timedelta(days=2)


def _read_policy(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _parse_ymd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_compact_date(value: str) -> date | None:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _parse_manual_dates(path: Path) -> set[date]:
    if not path.exists():
        return set()

    out: set[date] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(_parse_ymd(line))
    return out


def _parse_ics_dates(path: Path) -> set[date]:
    if not path.exists():
        return set()

    out: set[date] = set()
    start_val: date | None = None
    end_val: date | None = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if line == "BEGIN:VEVENT":
            start_val = None
            end_val = None
            continue

        if line.startswith("DTSTART"):
            _, _, value = line.partition(":")
            start_val = _parse_compact_date(value)
            continue

        if line.startswith("DTEND"):
            _, _, value = line.partition(":")
            end_val = _parse_compact_date(value)
            continue

        if line == "END:VEVENT" and start_val is not None:
            if end_val is None:
                out.add(start_val)
                continue

            d = start_val
            while d < end_val:
                out.add(d)
                d += timedelta(days=1)

    return out


def _country_holidays(country: str, subdiv: str | None, years: list[int]) -> set[date]:
    try:
        import holidays as holidays_pkg
    except ImportError:
        return set()

    items = holidays_pkg.country_holidays(country, subdiv=subdiv, years=years)
    return set(items.keys())


def _notify(title: str, message: str) -> None:
    script = (
        'display notification "'
        + message.replace('"', "'")
        + '" with title "'
        + title.replace('"', "'")
        + '"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def _open_path(path: Path) -> None:
    subprocess.run(["open", str(path)], check=False)


def _on_vpn(host: str | None) -> bool:
    if not host:
        return True
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def _download_holiday_file(url: str, target: Path) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(url, timeout=25) as resp:
            data = resp.read()
        target.write_bytes(data)
        return True
    except Exception:
        return False


def _resolve_path(p: str | None, root: Path) -> Path | None:
    if not p:
        return None
    path = Path(p)
    if path.is_absolute():
        return path
    return root / path


def _is_non_workday(day: date, holiday_set: set[date]) -> bool:
    if day.weekday() >= 5:
        return True
    return day in holiday_set


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'").strip('"')
    return out


def _resolve_download_url(
    dl_cfg: dict[str, Any],
    env_vars: dict[str, str],
) -> str | None:
    env_name = dl_cfg.get("url_env")
    if env_name:
        env_value = env_vars.get(str(env_name))
        if env_value:
            return str(env_value)
    url = dl_cfg.get("url")
    return str(url) if url else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailtriage-daily")
    parser.add_argument("--config", type=Path, default=Path("config.yml"))
    parser.add_argument("--policy", type=Path, default=Path("daily.policy.yml"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    ns = parser.parse_args(argv)

    cfg = load_config(ns.config)
    repo_root = Path.cwd()
    policy = _read_policy(ns.policy)
    dotenv_file = _resolve_path(policy.get("env_file", ".env"), repo_root)
    dotenv_vars = _read_dotenv(dotenv_file) if dotenv_file else {}
    merged_env = {**dotenv_vars, **os.environ}

    tz = ZoneInfo(cfg.time.timezone)
    now_local = datetime.now(tz)

    country = str(policy.get("country", "US"))
    subdiv = policy.get("subdiv")
    years = [now_local.year - 1, now_local.year, now_local.year + 1]

    holiday_set = _country_holidays(country=country, subdiv=subdiv, years=years)

    manual_holidays_file = _resolve_path(policy.get("manual_holidays_file"), repo_root)
    manual_workdays_file = _resolve_path(policy.get("manual_workdays_file"), repo_root)

    if manual_holidays_file:
        holiday_set |= _parse_manual_dates(manual_holidays_file)
    if manual_workdays_file:
        holiday_set -= _parse_manual_dates(manual_workdays_file)

    ics_files = policy.get("ics_files") or []
    for entry in ics_files:
        ics_path = _resolve_path(str(entry), repo_root)
        if ics_path:
            holiday_set |= _parse_ics_dates(ics_path)

    dl_cfg = policy.get("holiday_download") if isinstance(policy.get("holiday_download"), dict) else {}
    if dl_cfg.get("enabled", False):
        run_month_day = str(dl_cfg.get("run_month_day", "01-01"))
        should_run_download = now_local.strftime("%m-%d") == run_month_day

        output_tpl = dl_cfg.get("output_file_template")
        output_file = None
        if output_tpl:
            output_file = _resolve_path(str(output_tpl).format(year=now_local.year), repo_root)

        if should_run_download and output_file and not output_file.exists():
            vpn_ok = _on_vpn(dl_cfg.get("vpn_check_host"))
            url = _resolve_download_url(dl_cfg, merged_env)
            downloaded = False

            if vpn_ok and url and not ns.dry_run:
                downloaded = _download_holiday_file(url, output_file)

            if output_file.exists():
                holiday_set |= _parse_ics_dates(output_file)

            if (
                not downloaded
                and dl_cfg.get("remind_if_missing", True)
                and not ns.dry_run
            ):
                _notify(
                    "MailTriage",
                    "Holiday file missing. Connect to VPN and refresh holiday calendar.",
                )

    end_day = now_local.date()
    notify_cfg = policy.get("notification") if isinstance(policy.get("notification"), dict) else {}

    suppress_non_workday = bool(notify_cfg.get("suppress_on_non_workday", True))
    is_non_workday = _is_non_workday(end_day, holiday_set)

    label_day = _window_label_for_now(now_local, cfg.time.workday_start)
    report_path = cfg.rootdir / label_day.strftime("%Y/%m/%d.md")
    latest_path = cfg.rootdir / "latest.md"

    if ns.dry_run:
        print(f"now_local={now_local.isoformat()}")
        print(f"label_day={label_day.isoformat()}")
        print(f"report_path={report_path}")
        print(f"non_workday={is_non_workday}")
        return 0

    if ns.force or not report_path.exists():
        rc = mailtriage_main(["run", "--config", str(ns.config), "--days", "1"])
        if rc != 0:
            _notify("MailTriage", "Daily run failed.")
            return rc

    if report_path.exists():
        if latest_path.exists() or latest_path.is_symlink():
            latest_path.unlink()
        latest_path.symlink_to(report_path)

    notify_enabled = bool(notify_cfg.get("enabled", True))
    open_enabled = bool(notify_cfg.get("open_report", False))

    if suppress_non_workday and is_non_workday:
        return 0

    if report_path.exists() and notify_enabled:
        _notify("MailTriage", f"Daily report ready: {report_path.name}")

    if report_path.exists() and open_enabled:
        _open_path(report_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
