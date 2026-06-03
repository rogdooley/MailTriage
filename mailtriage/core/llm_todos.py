from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from mailtriage.core.hp_rules import sender_matches_high_priority

_TASK_ID_RE = re.compile(r"<!--\s*mailtriage:id=([a-f0-9]{10,64})\s*-->")
_DONE_RE = re.compile(r"^\s*[-*]\s+(?:\[(x|X)\]\s+|DONE:\s+|done:\s+)")
_DATE_H2_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
_TOP_LEVEL_BULLET_RE = re.compile(r"^[-*]\s+")


@dataclass(frozen=True)
class TodoLlmConfig:
    todo_root: Path
    running_path: Path
    api_base: str
    model: str
    api_key: str | None
    timeout_sec: int
    max_threads: int
    max_tasks_per_thread: int
    max_messages_per_thread: int
    max_chars_per_message: int
    max_output_tokens: int
    retries: int
    retry_backoff_sec: float
    ca_bundle: str | None
    insecure_skip_verify: bool


def run_llm_todo_sync(
    *,
    db,
    window_start_utc: datetime,
    window_end_utc: datetime,
    timezone: str,
    high_priority_senders: list[object],
) -> None:
    cfg = _load_from_env()
    if cfg is None:
        _debug("todo sync disabled: missing MAILTRIAGE_TODO_ROOT/API_BASE/MODEL")
        return

    _debug(
        "todo sync enabled: "
        + f"root={cfg.todo_root} model={cfg.model} base={cfg.api_base} timeout={cfg.timeout_sec}s retries={cfg.retries}"
    )

    running_path = cfg.running_path
    done_root = cfg.todo_root / "done"
    state_path = cfg.todo_root / ".mailtriage_todo_state.json"
    cfg.todo_root.mkdir(parents=True, exist_ok=True)
    running_path.parent.mkdir(parents=True, exist_ok=True)

    done_ids = _load_done_ids(state_path)
    running_lines = _read_lines(running_path)
    today_local = window_end_utc.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d")

    kept_lines, moved_lines, moved_ids = _purge_checked_items(running_lines)
    kept_lines, removed_placeholders = _cleanup_placeholder_entries(kept_lines)
    if removed_placeholders:
        _debug(f"todo sync removed placeholder entries: count={removed_placeholders}")
    if moved_lines:
        _append_done_lines(
            done_root=done_root, date_label=today_local, lines=moved_lines
        )
        done_ids |= moved_ids
        _debug(
            "todo sync archived checked items: "
            + f"count={len(moved_lines)} done_date={today_local}"
        )

    active_ids = _collect_ids(kept_lines)
    extracted = _extract_thread_tasks(
        db=db,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        hp_senders=high_priority_senders,
        cfg=cfg,
    )
    _debug(f"todo sync extracted tasks from llm: count={len(extracted)}")

    new_lines: list[str] = []
    for item in extracted:
        item_id = _item_id(item["thread_id"], item["task"])
        if item_id in done_ids or item_id in active_ids:
            continue
        task = _normalize_task(item["task"])
        if not task:
            continue
        subject = str(item.get("subject") or "").strip()
        new_lines.extend(_format_todo_block(subject=subject, task=task, item_id=item_id))
        active_ids.add(item_id)

    updated_running = _append_to_date_section(kept_lines, today_local, new_lines)
    _write_lines(running_path, updated_running)
    _debug(
        "todo sync wrote running markdown: "
        + f"path={running_path} added={len(new_lines)}"
    )

    if moved_ids:
        _save_done_ids(state_path, done_ids)
        _debug(
            f"todo sync updated done id state: path={state_path} ids={len(done_ids)}"
        )


def _load_from_env() -> TodoLlmConfig | None:
    todo_root = (os.environ.get("MAILTRIAGE_TODO_ROOT") or "").strip()
    api_base = (os.environ.get("MAILTRIAGE_LITELLM_API_BASE") or "").strip()
    model = (os.environ.get("MAILTRIAGE_LITELLM_MODEL") or "").strip()
    if not todo_root or not api_base or not model:
        return None

    todo_root_path = Path(todo_root).expanduser()
    running_path_raw = (os.environ.get("MAILTRIAGE_RUNNING_PATH") or "").strip()
    if running_path_raw:
        running_path = Path(running_path_raw).expanduser()
        if not running_path.is_absolute():
            running_path = (todo_root_path / running_path).resolve()
    else:
        running_path = todo_root_path / "RunningToDos.md"

    return TodoLlmConfig(
        todo_root=todo_root_path,
        running_path=running_path,
        api_base=api_base.rstrip("/"),
        model=model,
        api_key=(os.environ.get("MAILTRIAGE_LITELLM_API_KEY") or "").strip() or None,
        timeout_sec=max(5, _env_int("MAILTRIAGE_LITELLM_TIMEOUT_SEC", 20)),
        max_threads=max(
            1,
            _env_int("MAILTRIAGE_LITELLM_MAX_THREADS", 20),
        ),
        max_tasks_per_thread=max(
            1,
            _env_int("MAILTRIAGE_LITELLM_MAX_TASKS_PER_THREAD", 5),
        ),
        max_messages_per_thread=max(
            1,
            _env_int("MAILTRIAGE_LITELLM_MAX_MESSAGES_PER_THREAD", 3),
        ),
        max_chars_per_message=max(
            120,
            _env_int("MAILTRIAGE_LITELLM_MAX_CHARS_PER_MESSAGE", 450),
        ),
        max_output_tokens=max(
            64, _env_int("MAILTRIAGE_LITELLM_MAX_OUTPUT_TOKENS", 280)
        ),
        retries=max(0, _env_int("MAILTRIAGE_LITELLM_RETRIES", 1)),
        retry_backoff_sec=max(
            0.1, _env_float("MAILTRIAGE_LITELLM_RETRY_BACKOFF_SEC", 1.2)
        ),
        ca_bundle=(os.environ.get("MAILTRIAGE_LITELLM_CA_BUNDLE") or "").strip()
        or None,
        insecure_skip_verify=_truthy(
            os.environ.get("MAILTRIAGE_LITELLM_INSECURE_SKIP_VERIFY")
        ),
    )


def _query_all(db, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = db.conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _extract_thread_tasks(
    *,
    db,
    window_start_utc: datetime,
    window_end_utc: datetime,
    hp_senders: list[object],
    cfg: TodoLlmConfig,
) -> list[dict[str, str]]:
    if not hp_senders:
        return []

    rows = _query_all(
        db,
        """
        SELECT thread_id, sender, subject, date_utc, extracted_new_text
        FROM messages
        WHERE date_utc >= ?
          AND date_utc < ?
          AND inbound = 1
        ORDER BY date_utc ASC
        """,
        (
            window_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sender = str(row.get("sender") or "")
        if not sender_matches_high_priority(sender, hp_senders):
            continue
        tid = str(row.get("thread_id") or "")
        if not tid:
            continue
        grouped.setdefault(tid, []).append(row)

    _debug(
        "todo sync high-priority inbound candidates: "
        + f"messages={len(rows)} threads={len(grouped)}"
    )

    # Prioritize most recent active threads first instead of lexical thread-id order.
    all_thread_ids = [
        tid
        for tid, _ in sorted(
            grouped.items(),
            key=lambda item: (
                max(str(m.get("date_utc") or "") for m in item[1]),
                item[0],
            ),
            reverse=True,
        )
    ]
    selected_thread_ids = all_thread_ids[: cfg.max_threads]
    if len(all_thread_ids) > len(selected_thread_ids):
        _debug(
            "todo sync thread cap applied: "
            + f"selected={len(selected_thread_ids)} skipped={len(all_thread_ids) - len(selected_thread_ids)} max_threads={cfg.max_threads}"
        )

    out: list[dict[str, str]] = []
    for tid in selected_thread_ids:
        raw_msgs = grouped[tid]
        msgs = _dedupe_thread_messages(raw_msgs)
        latest_subject = _latest_subject(msgs)
        _debug(
            "todo sync selected thread: "
            + f"thread={tid[:10]} subject={latest_subject!r}"
        )
        prompt = _build_prompt(
            msgs=msgs,
            max_tasks=cfg.max_tasks_per_thread,
            max_messages=cfg.max_messages_per_thread,
            max_chars_per_message=cfg.max_chars_per_message,
        )
        tasks = _call_litellm(prompt=prompt, cfg=cfg)
        if not tasks:
            fallback = _fallback_entry_from_subject(latest_subject)
            if fallback:
                tasks = [fallback]
                _debug(
                    "todo sync using subject fallback entry: "
                    + f"thread={tid[:10]} subject={latest_subject!r}"
                )
        _debug(
            "todo sync llm thread result: "
            + f"thread={tid[:10]} raw_msgs={len(raw_msgs)} unique_msgs={len(msgs)} tasks={len(tasks)}"
        )
        for task in tasks[: cfg.max_tasks_per_thread]:
            out.append({"thread_id": tid, "task": task, "subject": latest_subject})
    return out


def _latest_subject(msgs: list[dict[str, Any]]) -> str:
    if not msgs:
        return ""
    subj = str(msgs[-1].get("subject") or "").strip()
    return re.sub(r"\s+", " ", subj)


def _dedupe_thread_messages(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated copies of the same thread update before LLM calls."""
    if not msgs:
        return msgs

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in msgs:
        key = (
            re.sub(r"\s+", " ", str(m.get("subject") or "").strip().lower()),
            str(m.get("date_utc") or ""),
        )
        grouped.setdefault(key, []).append(m)

    out: list[dict[str, Any]] = []
    for key in sorted(grouped.keys(), key=lambda k: k[1]):
        candidates = grouped[key]
        best = max(candidates, key=_message_quality)
        out.append(best)
    return out


def _message_signature(m: dict[str, Any]) -> str:
    subject = re.sub(r"\s+", " ", str(m.get("subject") or "").strip().lower())
    body = re.sub(r"\s+", " ", str(m.get("extracted_new_text") or "").strip().lower())
    return hashlib.sha256((subject + "\n" + body).encode("utf-8")).hexdigest()


def _message_quality(m: dict[str, Any]) -> tuple[int, int]:
    subject = re.sub(r"\s+", " ", str(m.get("subject") or "").strip().lower())
    body = re.sub(r"\s+", " ", str(m.get("extracted_new_text") or "").strip().lower())
    score = 0
    if body:
        score += min(len(body), 600)
    if body == "this transaction appears to have no content":
        score -= 100
    if subject and body and subject in body:
        score += 80
    return score, len(body)


def _fallback_entry_from_subject(subject: str) -> str:
    subj = re.sub(r"\s+", " ", (subject or "").strip())
    if not subj:
        return ""

    low = subj.lower()
    incident_terms = (
        "fail",
        "failed",
        "failure",
        "error",
        "outage",
        "down",
        "incident",
        "instability",
        "unstable",
        "problem",
        "issue",
        "degraded",
        "urgent",
        "alert",
    )
    if any(term in low for term in incident_terms):
        return "Incident reported. Action: Investigate and resolve the issue"

    return "Subject-only email. Action: Review and decide if follow-up is needed"


def _build_prompt(
    *,
    msgs: list[dict[str, Any]],
    max_tasks: int,
    max_messages: int,
    max_chars_per_message: int,
) -> str:
    snippets: list[str] = []
    for m in msgs[-max_messages:]:
        when = str(m.get("date_utc") or "")
        subject = str(m.get("subject") or "").strip()
        body_text = str(m.get("extracted_new_text") or "").strip()
        body_text = re.sub(r"\s+", " ", body_text)

        combined = ""
        if subject and body_text:
            combined = f"Subject: {subject}. Body: {body_text}"
        elif body_text:
            combined = body_text
        elif subject:
            combined = f"(no body excerpt; use subject) {subject}"
        combined = combined[:max_chars_per_message]

        snippets.append(
            "\n".join(
                [
                    f"Timestamp: {when}",
                    f"Subject: {subject}",
                    f"Message: {combined}",
                ]
            )
        )

    joined = "\n\n---\n\n".join(snippets)
    return "\n".join(
        [
            "Summarize each email thread and create todo entries.",
            'Return strict JSON object: {"todos": ["entry", ...]}',
            "Return JSON only. No prose. No markdown. No analysis.",
            "Each entry must use this format: '<summary>. Action: <todo or No action required>'.",
            "Do not output literal placeholders like '<summary>' or '<todo ...>'; use concrete text.",
            "If no action is required, still return an entry with 'Action: No action required'.",
            "Never use placeholder text like '...' or 'TBD'.",
            f"Limit to {max_tasks} tasks.",
            "Always return at least one entry when emails are provided.",
            "",
            joined,
        ]
    )


def _call_litellm(*, prompt: str, cfg: TodoLlmConfig) -> list[str]:
    url = cfg.api_base + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You summarize work email and generate concise todo entries. "
                    'Output valid JSON only using schema {"todos": ["..."]}. '
                    "Do not include reasoning or markdown. "
                    "Every entry must include an Action clause."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": cfg.max_output_tokens,
        "response_format": {"type": "json_object"},
    }

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    req = Request(url=url, data=body, headers=headers, method="POST")
    ssl_context = _build_ssl_context(cfg)
    attempts = cfg.retries + 1
    raw = ""
    for attempt in range(1, attempts + 1):
        try:
            _debug(
                f"todo sync calling litellm: url={url} model={cfg.model} attempt={attempt}/{attempts}"
            )
            with urlopen(req, timeout=cfg.timeout_sec, context=ssl_context) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except HTTPError as e:
            _debug(
                f"todo sync litellm http error: status={e.code} attempt={attempt}/{attempts}"
            )
            if attempt < attempts and e.code in {408, 429, 500, 502, 503, 504}:
                time.sleep(cfg.retry_backoff_sec * attempt)
                continue
            return []
        except URLError as e:
            _debug(
                f"todo sync litellm url error: reason={e.reason} attempt={attempt}/{attempts}"
            )
            if attempt < attempts:
                time.sleep(cfg.retry_backoff_sec * attempt)
                continue
            return []
        except TimeoutError:
            _debug(f"todo sync litellm timeout: attempt={attempt}/{attempts}")
            if attempt < attempts:
                time.sleep(cfg.retry_backoff_sec * attempt)
                continue
            return []

    try:
        doc = json.loads(raw)
        msg = doc["choices"][0]["message"]
        content = _coerce_content_to_text(msg.get("content"))
    except Exception:
        _debug(
            "todo sync litellm response parse error: missing choices[0].message.content"
        )
        return []

    parsed = _extract_json(content)
    if not parsed:
        reasoning = _extract_reasoning_text(doc)
        if reasoning:
            parsed = _extract_json(reasoning)
    if not parsed:
        if content:
            _debug(f"todo sync content sample: {content[:220]!r}")
        reasoning = _extract_reasoning_text(doc)
        if reasoning:
            _debug(f"todo sync reasoning sample: {reasoning[:220]!r}")
        _debug("todo sync litellm content parse produced no todos")
        return []
    out: list[str] = []
    for item in parsed:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _extract_json(content: str) -> list[str]:
    content = (content or "").strip()
    for candidate in _json_candidates(content):
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            items = _extract_items_from_dict(data)
            if items:
                return items
        if isinstance(data, list):
            items = _extract_items_from_list(data)
            if items:
                return items
    return []


def _json_candidates(content: str) -> list[str]:
    out = [content]
    m = re.search(r"```json\s*(.*?)\s*```", content, flags=re.S | re.I)
    if m:
        out.append(m.group(1).strip())
    for m2 in re.finditer(r"\{[\s\S]*?\}", content):
        cand = m2.group(0)
        if '"todos"' in cand:
            out.append(cand)
    return out


def _extract_reasoning_text(doc: dict[str, Any]) -> str:
    try:
        msg = doc["choices"][0]["message"]
    except Exception:
        return ""
    if isinstance(msg.get("reasoning_content"), str):
        return msg["reasoning_content"]
    ps = msg.get("provider_specific_fields")
    if isinstance(ps, dict):
        for k in ("reasoning_content", "reasoning"):
            if isinstance(ps.get(k), str):
                return ps[k]
    return ""


def _coerce_content_to_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    if isinstance(content, dict):
        txt = content.get("text") if isinstance(content, dict) else None
        if isinstance(txt, str):
            return txt
    return str(content)


def _extract_items_from_dict(data: dict[str, Any]) -> list[str]:
    for key in ("todos", "tasks", "items", "entries"):
        val = data.get(key)
        if isinstance(val, list):
            items = _extract_items_from_list(val)
            if items:
                return items
    return []


def _extract_items_from_list(items: list[Any]) -> list[str]:
    out: list[str] = []
    for x in items:
        if isinstance(x, str):
            s = _normalize_task(x)
            if s:
                out.append(s)
            continue
        if isinstance(x, dict):
            text = None
            for key in ("task", "todo", "text", "entry"):
                if isinstance(x.get(key), str):
                    text = x[key]
                    break
            if text is None and isinstance(x.get("summary"), str):
                summary = x.get("summary", "").strip()
                action = x.get("action") if isinstance(x.get("action"), str) else ""
                text = f"{summary}. Action: {action}" if action else summary
            s = _normalize_task(text or "")
            if s:
                out.append(s)
    return out


def _normalize_task(value: str) -> str:
    s = re.sub(r"\s+", " ", (value or "").strip())
    s = s.strip("-*")
    s = s.strip()
    if not s:
        return ""
    low = s.lower().strip(" .")
    if low in {"...", "…", "tbd", "todo", "task", "n/a", "na"}:
        return ""
    if "<summary>" in low or "<todo" in low:
        return ""
    if set(s) <= {".", " ", "-"}:
        return ""
    if len(low) < 4:
        return ""
    return s


def _split_summary_action(task: str) -> tuple[str, str]:
    m = re.search(r"\bAction\s*:\s*", task, flags=re.IGNORECASE)
    if not m:
        return task.strip(), "Review and follow up as needed"
    summary = task[: m.start()].strip().rstrip("-:")
    action = task[m.end() :].strip()
    if not summary:
        summary = "Email update"
    if not action:
        action = "Review and follow up as needed"
    return summary, action


def _format_todo_block(*, subject: str, task: str, item_id: str) -> list[str]:
    summary, action = _split_summary_action(task)
    label = f"({subject}) " if subject else ""
    top = f"- [ ] {label}{summary}".rstrip()
    action_line = f"  - Action: {action} <!-- mailtriage:id={item_id} -->"
    return [top, action_line]


def _cleanup_placeholder_entries(lines: list[str]) -> tuple[list[str], int]:
    kept: list[str] = []
    removed = 0
    for ln in lines:
        m = _TASK_ID_RE.search(ln)
        if not m:
            kept.append(ln)
            continue
        task_part = _TASK_ID_RE.sub("", ln)
        task_part = re.sub(r"^\s*[-*]\s+", "", task_part).strip()
        if not _normalize_task(task_part):
            removed += 1
            continue
        kept.append(ln)
    return kept, removed


def _item_id(thread_id: str, task: str) -> str:
    seed = thread_id + "\n" + re.sub(r"\s+", " ", task.strip().lower())
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return ["# Running Todos", ""]
    return path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _collect_ids(lines: list[str]) -> set[str]:
    ids: set[str] = set()
    for ln in lines:
        m = _TASK_ID_RE.search(ln)
        if m:
            ids.add(m.group(1))
    return ids


def _purge_checked_items(lines: list[str]) -> tuple[list[str], list[str], set[str]]:
    keep: list[str] = []
    moved: list[str] = []
    moved_ids: set[str] = set()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if _DONE_RE.match(ln):
            moved.append(ln)
            m = _TASK_ID_RE.search(ln)
            if m:
                moved_ids.add(m.group(1))
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if _DONE_RE.match(nxt):
                    break
                if _TOP_LEVEL_BULLET_RE.match(nxt) or _DATE_H2_RE.match(nxt):
                    break
                if nxt.startswith(" ") or nxt.startswith("\t"):
                    moved.append(nxt)
                    m = _TASK_ID_RE.search(nxt)
                    if m:
                        moved_ids.add(m.group(1))
                    i += 1
                    continue
                break
            continue
        keep.append(ln)
        i += 1
    return keep, moved, moved_ids


def _append_to_date_section(
    lines: list[str], date_label: str, items: list[str]
) -> list[str]:
    if not items:
        return lines
    out = list(lines)

    header = f"## {date_label}"
    if header not in out:
        if out and out[-1].strip():
            out.append("")
        out.append(header)
        out.append("")
        out.extend(items)
        return out

    idx = out.index(header)
    insert_at = len(out)
    for i in range(idx + 1, len(out)):
        if _DATE_H2_RE.match(out[i]):
            insert_at = i
            break

    block = [ln for ln in items]
    if insert_at > 0 and out[insert_at - 1].strip():
        block = [""] + block
    out[insert_at:insert_at] = block
    return out


def _append_done_lines(*, done_root: Path, date_label: str, lines: list[str]) -> None:
    y, m, d = date_label.split("-")
    path = done_root / y / m / f"{d}.md"
    if path.exists():
        out = path.read_text(encoding="utf-8").splitlines()
    else:
        out = [f"# Done {date_label}", ""]

    if out and out[-1].strip():
        out.append("")
    out.extend(lines)
    _write_lines(path, out)


def _load_done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(data, dict) or not isinstance(data.get("done_ids"), list):
        return set()
    return {str(x) for x in data["done_ids"] if x}


def _save_done_ids(path: Path, done_ids: set[str]) -> None:
    path.write_text(
        json.dumps({"done_ids": sorted(done_ids)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _debug(msg: str) -> None:
    if os.environ.get("MAILTRIAGE_DEBUG"):
        sys.stderr.write(f"[mailtriage][todo] {msg}\n")


def _truthy(value: str | None) -> bool:
    v = (value or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _build_ssl_context(cfg: TodoLlmConfig) -> ssl.SSLContext | None:
    if cfg.insecure_skip_verify:
        _debug(
            "todo sync ssl verify disabled via MAILTRIAGE_LITELLM_INSECURE_SKIP_VERIFY"
        )
        return ssl._create_unverified_context()
    if cfg.ca_bundle:
        try:
            ctx = ssl.create_default_context(cafile=cfg.ca_bundle)
            _debug(f"todo sync using custom CA bundle: {cfg.ca_bundle}")
            return ctx
        except Exception as e:
            _debug(f"todo sync invalid CA bundle '{cfg.ca_bundle}': {e}")
    return None


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _debug(f"todo sync invalid int for {name}: {raw!r}; using default {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _debug(f"todo sync invalid float for {name}: {raw!r}; using default {default}")
        return default
