from __future__ import annotations

import os
from pathlib import Path


def read_dotenv(path: Path) -> dict[str, str]:
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


def load_dotenv_into_environ(path: Path) -> dict[str, str]:
    vals = read_dotenv(path)
    for k, v in vals.items():
        # Keep explicit shell/launchd values authoritative.
        os.environ.setdefault(k, v)
    return vals
