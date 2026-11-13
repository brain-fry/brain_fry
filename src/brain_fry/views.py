"""What the inbox and the app show for a task. Shared by `bf list` and the app."""

from __future__ import annotations

import re
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
    """The `inbox` flag (a reply or a block waits for me), or pinned. Only a task of the working
    column (Renaud, 2026-11-11: planning and the hidden columns wait; next too, 2026-11-13),
    never an archived one."""
    return (t.inbox or t.pinned) and t.status != "done" and t.prio == "working"


def inbox_order(t: Task):
    """What needs me first (the `inbox` flag): red first, then most recent first.
    DESIGN_DETAILS.md "Status, inbox, board". Then the pinned tasks that need nothing now,
    in the same order."""
    return (not t.inbox, t.status != "blocked", -epoch(t.updated))


def board_order(t: Task):
    return (PRIO_ORDER.get(t.prio, 99), -epoch(t.updated))


def project_order(projects: list[dict], tasks: list[Task]) -> list[dict]:
    """The projects of the dropdown, Cmd+P and Cmd+T, the most recently used first: by the
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
    digest arrives) the start of the reply. A turn cut off before its reply (the session
    ended, see actions.end_session) shows its prompt."""
    if t.status == "blocked" and t.notification:
        return f"blocked: {t.notification}"
    if not t.threads:
        return ""
    last = t.threads[-1]
    if last.kind == "interrupted" and not (last.digest or last.reply):
        line = "interrupted: " + " ".join(last.prompt.split())
    else:
        line = " ".join((last.digest or last.reply).split())
    return line if len(line) <= width else line[: width - 3] + "..."


def goal_line(digest: str) -> str:
    """What the task is about, for the card of 4a at a switch: the "Goal:" line of the task's
    digest (its prompt asks for the goal first), without the label; else the digest's first line."""
    lines = [line.strip() for line in digest.splitlines() if line.strip()]
    for line in lines:
        if line.lower().startswith("goal:"):
            return line[5:].strip()
    return lines[0] if lines else ""


PASTE_TAG = re.compile(r"</?pasted_content\b[^>]*>")  # a pasted text arrives inside it (seen in the task files)
IMAGE_REF = re.compile(r"\[Image #\d+\]")


def first_prompt(t: Task, width: int = 300) -> str:
    """The start of the first prompt that I typed, on one line, for the card of 4a at a switch (the
    card shows two lines of it). A planned task that never started: its notes. Empty until the
    first digest, since the title shown then is already that prompt's first line (display_title)."""
    if not t.title:
        return ""
    text = next((th.prompt for th in t.threads if th.prompt.strip()), "") or t.notes
    text = " ".join(IMAGE_REF.sub(" ", PASTE_TAG.sub(" ", text)).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def first_line(text: str, width: int = 100) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        return line if len(line) <= width else line[: width - 3] + "..."
    return ""


def project_tabs_json(tabs: dict, shells: list[dict] | None = None) -> dict:
    """The tabs of a project (actions.project_tabs), with the names of a task's (task_json)."""
    return {
        "openFiles": tabs["open_files"],
        "browserTabs": tabs["browser_tabs"],
        "finderTabs": tabs["finder_tabs"],
        "shells": shells or [],
        "terminalTabs": len(tabs["terminal_tabs"]),
    }


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
        "updated": t.updated,
        "opened": t.opened,
        "parent": t.parent,
        "digest": t.digest,
        "notes": t.notes,  # a planned task: the card's text until the first digest
        "goal": goal_line(t.digest),  # the card of 4a at a switch
        "firstPrompt": first_prompt(t),  # ditto
        "line": inbox_line(t),
        "kind": last.kind if last else None,
        "question": last.question if last else None,
        "threads": [
            {"n": th.n, "kind": th.kind, "digest": th.digest, "prompt": first_line(th.prompt)}
            for th in t.threads
        ],
        "pty": pty_gen,
        # live, but not a process of this app: a session started in another terminal
        "outside": t.live and pty_gen is None,
        # the process that runs it outside the app, if Claude Code says so: {pid, where, status}
        "elsewhere": elsewhere,
        "openFiles": t.open_files,
        "browserTabs": t.browser_tabs,
        "finderTabs": t.finder_tabs,
        "shells": shells or [],  # terminal tabs (3b) that run now: {key, gen, title}
        "terminalTabs": len(t.terminal_tabs),  # the shells that the select starts: veil.js waits for them
    }
