from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


_TASK_ID_RE = re.compile(r"<!--\s*mailtriage:id=([a-f0-9]{10,64})\s*-->")
_CHECKED_RE = re.compile(r"^\s*[-*]\s+\[(x|X)\]\s+")
_DATE_H2_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")


@dataclass(frozen=True)
class TodoLlmConfig:
    todo_root: Path
    api_base: str
    model: str
    api_key: str | None
    timeout_sec: int
    max_threads: int
    max_tasks_per_thread: int
    ca_bundle: str | None
    insecure_skip_verify: bool


def run_llm_todo_sync(
    *,
    db,
    window_start_utc: datetime,
    window_end_utc: datetime,
    timezone: str,
    high_priority_senders: list[str],
) -> None:
    cfg = _load_from_env()
    if cfg is None:
        _debug("todo sync disabled: missing MAILTRIAGE_TODO_ROOT/API_BASE/MODEL")
        return

    _debug(
        "todo sync enabled: "
        + f"root={cfg.todo_root} model={cfg.model} base={cfg.api_base}"
    )

    running_path = cfg.todo_root / "running.md"
    done_root = cfg.todo_root / "done"
    state_path = cfg.todo_root / ".mailtriage_todo_state.json"
    cfg.todo_root.mkdir(parents=True, exist_ok=True)

    done_ids = _load_done_ids(state_path)
    running_lines = _read_lines(running_path)
    today_local = window_end_utc.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d")

    kept_lines, moved_lines, moved_ids = _purge_checked_items(running_lines)
    if moved_lines:
        _append_done_lines(done_root=done_root, date_label=today_local, lines=moved_lines)
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
        task = item["task"].strip()
        if not task:
            continue
        new_lines.append(f"- [ ] {task} <!-- mailtriage:id={item_id} -->")
        active_ids.add(item_id)

    updated_running = _append_to_date_section(kept_lines, today_local, new_lines)
    _write_lines(running_path, updated_running)
    _debug(
        "todo sync wrote running markdown: "
        + f"path={running_path} added={len(new_lines)}"
    )

    if moved_ids:
        _save_done_ids(state_path, done_ids)
        _debug(f"todo sync updated done id state: path={state_path} ids={len(done_ids)}")


def _load_from_env() -> TodoLlmConfig | None:
    todo_root = (os.environ.get("MAILTRIAGE_TODO_ROOT") or "").strip()
    api_base = (os.environ.get("MAILTRIAGE_LITELLM_API_BASE") or "").strip()
    model = (os.environ.get("MAILTRIAGE_LITELLM_MODEL") or "").strip()
    if not todo_root or not api_base or not model:
        return None

    return TodoLlmConfig(
        todo_root=Path(todo_root).expanduser(),
        api_base=api_base.rstrip("/"),
        model=model,
        api_key=(os.environ.get("MAILTRIAGE_LITELLM_API_KEY") or "").strip() or None,
        timeout_sec=max(5, int(os.environ.get("MAILTRIAGE_LITELLM_TIMEOUT_SEC", "20") or "20")),
        max_threads=max(
            1,
            int(os.environ.get("MAILTRIAGE_LITELLM_MAX_THREADS", "20") or "20"),
        ),
        max_tasks_per_thread=max(
            1,
            int(
                os.environ.get("MAILTRIAGE_LITELLM_MAX_TASKS_PER_THREAD", "5") or "5"
            ),
        ),
        ca_bundle=(os.environ.get("MAILTRIAGE_LITELLM_CA_BUNDLE") or "").strip() or None,
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
    hp_senders: list[str],
    cfg: TodoLlmConfig,
) -> list[dict[str, str]]:
    hp = {x.strip().lower() for x in hp_senders if x and x.strip()}
    if not hp:
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
        sender = str(row.get("sender") or "").strip().lower()
        if sender not in hp:
            continue
        tid = str(row.get("thread_id") or "")
        if not tid:
            continue
        grouped.setdefault(tid, []).append(row)

    _debug(
        "todo sync high-priority inbound candidates: "
        + f"messages={len(rows)} threads={len(grouped)}"
    )

    out: list[dict[str, str]] = []
    for tid in sorted(grouped.keys())[: cfg.max_threads]:
        msgs = grouped[tid]
        prompt = _build_prompt(msgs=msgs, max_tasks=cfg.max_tasks_per_thread)
        tasks = _call_litellm(prompt=prompt, cfg=cfg)
        _debug(
            "todo sync llm thread result: "
            + f"thread={tid[:10]} msgs={len(msgs)} tasks={len(tasks)}"
        )
        for task in tasks[: cfg.max_tasks_per_thread]:
            out.append({"thread_id": tid, "task": task})
    return out


def _build_prompt(*, msgs: list[dict[str, Any]], max_tasks: int) -> str:
    snippets: list[str] = []
    for m in msgs[-6:]:
        when = str(m.get("date_utc") or "")
        subject = str(m.get("subject") or "").strip()
        text = str(m.get("extracted_new_text") or "").strip()
        text = re.sub(r"\s+", " ", text)
        text = text[:900]
        snippets.append(
            "\n".join(
                [
                    f"Timestamp: {when}",
                    f"Subject: {subject}",
                    f"Message: {text}",
                ]
            )
        )

    joined = "\n\n---\n\n".join(snippets)
    return "\n".join(
        [
            "Extract actionable todo items from these emails.",
            "Only include tasks requiring work by the recipient.",
            "Return strict JSON object: {\"todos\": [\"task\", ...]}",
            "Do not include markdown.",
            f"Limit to {max_tasks} tasks.",
            "If there are no actionable items, return {\"todos\": []}.",
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
                "content": "You extract concise, actionable todo items from work email.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    req = Request(url=url, data=body, headers=headers, method="POST")
    ssl_context = _build_ssl_context(cfg)
    try:
        _debug(f"todo sync calling litellm: url={url} model={cfg.model}")
        with urlopen(req, timeout=cfg.timeout_sec, context=ssl_context) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        _debug(f"todo sync litellm http error: status={e.code}")
        return []
    except URLError as e:
        _debug(f"todo sync litellm url error: reason={e.reason}")
        return []
    except TimeoutError:
        _debug("todo sync litellm timeout")
        return []

    try:
        doc = json.loads(raw)
        content = str(doc["choices"][0]["message"]["content"])
    except Exception:
        _debug("todo sync litellm response parse error: missing choices[0].message.content")
        return []

    parsed = _extract_json(content)
    if not parsed:
        _debug("todo sync litellm content parse produced no todos")
        return []
    out: list[str] = []
    for item in parsed:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _extract_json(content: str) -> list[str]:
    content = content.strip()
    for candidate in _json_candidates(content):
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("todos"), list):
            return [str(x) for x in data["todos"] if x]
        if isinstance(data, list):
            return [str(x) for x in data if x]
    return []


def _json_candidates(content: str) -> list[str]:
    out = [content]
    m = re.search(r"```json\s*(.*?)\s*```", content, flags=re.S | re.I)
    if m:
        out.append(m.group(1).strip())
    return out


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
        if _CHECKED_RE.match(ln):
            moved.append(ln)
            m = _TASK_ID_RE.search(ln)
            if m:
                moved_ids.add(m.group(1))
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if _CHECKED_RE.match(nxt):
                    break
                if nxt.lstrip().startswith("- [") or _DATE_H2_RE.match(nxt):
                    break
                if nxt.startswith(" ") or nxt.startswith("\t"):
                    moved.append(nxt)
                    i += 1
                    continue
                break
            continue
        keep.append(ln)
        i += 1
    return keep, moved, moved_ids


def _append_to_date_section(lines: list[str], date_label: str, items: list[str]) -> list[str]:
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
        _debug("todo sync ssl verify disabled via MAILTRIAGE_LITELLM_INSECURE_SKIP_VERIFY")
        return ssl._create_unverified_context()
    if cfg.ca_bundle:
        try:
            ctx = ssl.create_default_context(cafile=cfg.ca_bundle)
            _debug(f"todo sync using custom CA bundle: {cfg.ca_bundle}")
            return ctx
        except Exception as e:
            _debug(f"todo sync invalid CA bundle '{cfg.ca_bundle}': {e}")
    return None
