"""What the inbox and the app show for a task. Shared by `bf list` and the app."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .taskfile import PRIOS, Task

PRIO_ORDER = {p: i for i, p in enumerate(PRIOS)}


def epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


def inbox_order(t: Task):
    """Red first, then prio column, then most recent first. DESIGN.md "Status, inbox, board"."""
    return (t.status != "blocked", PRIO_ORDER.get(t.prio, 99), -epoch(t.updated))


def board_order(t: Task):
    return (PRIO_ORDER.get(t.prio, 99), -epoch(t.updated))


def inbox_line(t: Task, width: int = 160) -> str:
    """The blocked message, else the last thread's digest, else (placeholder until the
    digest arrives) the start of the reply."""
    if t.status == "blocked" and t.notification:
        return f"blocked: {t.notification}"
    if not t.threads:
        return ""
    last = t.threads[-1]
    line = " ".join((last.digest or last.reply).split())
    return line if len(line) <= width else line[: width - 3] + "..."


def first_line(text: str, width: int = 100) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        return line if len(line) <= width else line[: width - 3] + "..."
    return ""


def task_json(t: Task, pty_gen: int | None, shells: list[dict] | None = None) -> dict:
    last = t.threads[-1] if t.threads else None
    return {
        "id": t.id,
        "title": t.display_title,
        "project": Path(t.project).name,
        "projectPath": t.project,
        "prio": t.prio,
        "status": t.status,
        "live": t.live,
        "inbox": t.inbox,
        "unread": t.unread,
        "notification": t.notification,
        "updated": t.updated,
        "parent": t.parent,
        "digest": t.digest,
        "line": inbox_line(t),
        "kind": last.kind if last else None,
        "question": last.question if last else None,
        "threads": [
            {"n": th.n, "ts": th.ts, "kind": th.kind, "digest": th.digest, "prompt": first_line(th.prompt)}
            for th in t.threads
        ],
        "pty": pty_gen,
        # live, but not a process of this app: a session started in another terminal
        "outside": t.live and pty_gen is None,
        "openFiles": t.open_files,
        "shells": shells or [],  # terminal tabs (3b) that run now: {key, gen, title}
    }
