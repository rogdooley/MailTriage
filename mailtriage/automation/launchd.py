from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LaunchdSpec:
    label: str
    repo_root: Path
    config_path: Path
    policy_path: Path
    hour: int
    minute: int
    weekdays_only: bool
    stdout_path: Path
    stderr_path: Path


def _plist(spec: LaunchdSpec) -> str:
    # Use wrapper script so logs live under output.root and are pruned.
    wrapper = (spec.repo_root / "scripts" / "run_daily_mailtriage_launchd.sh").resolve()
    cmd = str(wrapper)

    if spec.weekdays_only:
        # launchd weekday: 1=Sunday ... 7=Saturday
        intervals = "\n".join(
            f"""      <dict>
        <key>Weekday</key><integer>{wd}</integer>
        <key>Hour</key><integer>{spec.hour}</integer>
        <key>Minute</key><integer>{spec.minute}</integer>
      </dict>"""
            for wd in (2, 3, 4, 5, 6)
        )
        start_calendar = f"<array>\n{intervals}\n    </array>"
    else:
        start_calendar = f"""<dict>
      <key>Hour</key><integer>{spec.hour}</integer>
      <key>Minute</key><integer>{spec.minute}</integer>
    </dict>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{spec.label}</string>

    <key>ProgramArguments</key>
    <array>
      <string>{cmd}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>StartCalendarInterval</key>
    {start_calendar}

    <key>EnvironmentVariables</key>
    <dict>
      <key>MAILTRIAGE_REPO</key><string>{spec.repo_root}</string>
      <key>MAILTRIAGE_CONFIG</key><string>{spec.config_path}</string>
      <key>MAILTRIAGE_POLICY</key><string>{spec.policy_path}</string>
    </dict>

    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/dev/null</string>
  </dict>
</plist>
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mailtriage-launchd")
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="Repo root (default: cwd)")
    p.add_argument("--config", type=Path, default=Path("config.yml"))
    p.add_argument("--policy", type=Path, default=Path("daily.policy.yml"))
    p.add_argument("--label", type=str, default="com.mailtriage.daily")
    p.add_argument("--hour", type=int, default=9)
    p.add_argument("--minute", type=int, default=5)
    p.add_argument("--weekdays-only", action="store_true", default=False)
    p.add_argument("--daily", dest="weekdays_only", action="store_false", default=False)
    # stdout/stderr are handled by the wrapper (written under output.root); launchd gets /dev/null.
    p.add_argument("--stdout", type=Path, default=Path("/dev/null"))
    p.add_argument("--stderr", type=Path, default=Path("/dev/null"))
    p.add_argument("--out", type=Path, default=None, help="Write plist to this file (default: stdout)")
    ns = p.parse_args(argv)

    spec = LaunchdSpec(
        label=ns.label,
        repo_root=ns.repo.resolve(),
        config_path=(ns.repo / ns.config).resolve() if not ns.config.is_absolute() else ns.config,
        policy_path=(ns.repo / ns.policy).resolve() if not ns.policy.is_absolute() else ns.policy,
        hour=ns.hour,
        minute=ns.minute,
        weekdays_only=bool(ns.weekdays_only),
        stdout_path=ns.stdout,
        stderr_path=ns.stderr,
    )

    content = _plist(spec)
    if ns.out:
        ns.out.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
