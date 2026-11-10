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


def in_inbox(t: Task) -> bool:
    """The `inbox` flag (a reply or a block waits for me), or pinned. Never an archived task."""
    return (t.inbox or t.pinned) and t.status != "done"


def inbox_order(t: Task):
    """What needs me first (the `inbox` flag): red first, then prio column, then most recent
    first. DESIGN_DETAILS.md "Status, inbox, board". Then the pinned tasks that need nothing now,
    in the same order."""
    return (not t.inbox, t.status != "blocked", PRIO_ORDER.get(t.prio, 99), -epoch(t.updated))


def board_order(t: Task):
    return (PRIO_ORDER.get(t.prio, 99), -epoch(t.updated))


def project_order(projects: list[dict], tasks: list[Task]) -> list[dict]:
    """The projects of the dropdown, Cmd+P and Cmd+N, the most recently used first: by the
    last `opened` of their tasks (archived ones too), then, for projects whose tasks were
    never opened, by their last `updated`; projects without tasks keep the order of
    projects.json. Each project gets `last`: that time, as ISO text, or None."""
    latest: dict[str, tuple[float, float, str]] = {}  # project path -> (opened, updated, ISO text) of its latest task
    for t in tasks:
        k = str(Path(t.project))
        cand = (epoch(t.opened or ""), epoch(t.updated or ""), t.opened or t.updated)
        if k not in latest or cand[:2] > latest[k][:2]:
            latest[k] = cand

    def of(p: dict) -> tuple[float, float, str | None]:
        return latest.get(str(Path(p["path"])), (0.0, 0.0, None))

    out = [{**p, "last": of(p)[2] or None} for p in projects]
    return sorted(out, key=lambda p: (-of(p)[0], -of(p)[1]))  # stable: ties keep projects.json order


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


def task_json(t: Task, pty_gen: int | None, shells: list[dict] | None = None, elsewhere: dict | None = None) -> dict:
    last = t.threads[-1] if t.threads else None
    return {
        "id": t.id,
        "title": t.display_title,
        "shortTitle": t.short_title,  # the heading of the card on the board
        "project": Path(t.project).name,
        "projectPath": t.project,
        "prio": t.prio,
        "status": t.status,
        "live": t.live,
        "inbox": t.inbox,
        "unread": t.unread,
        "pinned": t.pinned,
        "notification": t.notification,
        "updated": t.updated,
        "opened": t.opened,
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
        # the process that runs it outside the app, if Claude Code says so: {pid, where, status}
        "elsewhere": elsewhere,
        "openFiles": t.open_files,
        "browserTabs": t.browser_tabs,
        "shells": shells or [],  # terminal tabs (3b) that run now: {key, gen, title}
    }
