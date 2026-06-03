from __future__ import annotations

import re
from typing import Iterable
from email.utils import getaddresses


def normalize_sender_email(sender: str) -> str:
    s = (sender or "").strip()
    m = re.search(r"<([^>]+)>", s)
    if m:
        return m.group(1).strip().lower()
    return s.lower()


def parse_sender_name_email(sender: str) -> tuple[str, str]:
    parsed = getaddresses([sender or ""])
    if parsed and parsed[0][1]:
        name = (parsed[0][0] or "").strip().strip('"')
        email = parsed[0][1].strip().lower()
        return name, email
    s = (sender or "").strip()
    return "", s.lower()


def sender_matches_high_priority(sender: str, hp_rules: Iterable[object]) -> bool:
    name, email = parse_sender_name_email(sender)
    name_l = (name or "").strip()
    email_l = email.strip().lower()

    for r in hp_rules:
        if isinstance(r, str):
            rule_email = r.strip().lower()
            patt = None
        else:
            rule_email = str(getattr(r, "email", "") or "").strip().lower()
            patt = getattr(r, "name_regex", None)
        if not rule_email or rule_email != email_l:
            continue
        if not patt:
            return True
        try:
            if re.search(str(patt), name_l, flags=re.IGNORECASE):
                return True
        except re.error:
            # Invalid regex should not explode classification; ignore it.
            continue
    return False
