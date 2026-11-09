"""The reader: applies the hook events in the spool to task files. See DESIGN.md, "Hooks".

Order per event file: apply it to the task file, append it to events.jsonl, delete it.
A crash between the first and the last step applies that event twice on the next run.
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

from . import paths, taskfile
from .taskfile import Task, Thread
from .util import iso

BLOCKING = {"permission_prompt", "elicitation_dialog"}
CREATING = {"UserPromptSubmit", "Stop", "Notification"}  # SessionEnd alone creates no task


def pending(project: Path) -> list[tuple[Path, dict]]:
    """Event files in the spool, oldest first. Unreadable files are renamed *.bad."""
    out = []
    for f in paths.spool_dir(project).glob("*.json"):
        try:
            out.append((f, json.loads(f.read_text())))
        except (OSError, json.JSONDecodeError):
            f.rename(f.with_suffix(".bad"))
    out.sort(key=lambda fe: (fe[1].get("bf_ts", 0), fe[0].name))
    return out


def consume(project: Path) -> list[str]:
    """Apply every pending event of the project. Returns the ids of the tasks that changed.

    The lock lets one reader at a time work on a project. Without it, the app's reader and
    `bf list` or `bf consume` run by hand could both read a file before either deletes it,
    and apply it twice. flock is released when its holder exits, even after a crash."""
    if not paths.spool_dir(project).is_dir():  # project folder moved or deleted
        return []
    changed: list[str] = []
    with paths.consume_lock(project).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for f, ev in pending(project):
            task_id = apply(Path(project), ev)
            with paths.event_log(project).open("a") as log:
                log.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.unlink()
            if task_id and task_id not in changed:
                changed.append(task_id)
    return changed


def apply(project: Path, ev: dict) -> str | None:
    with taskfile.LOCK:
        return _apply(project, ev)


def _apply(project: Path, ev: dict) -> str | None:
    sid, name = ev.get("session_id"), ev.get("hook_event_name")
    if not sid or not name:
        return None
    ts = iso(ev.get("bf_ts", time.time()))
    path = paths.task_path(project, sid)
    if path.exists():
        task = taskfile.load(path)
    elif name in CREATING:
        task = Task(session_ids=[sid], project=str(project), cwd=ev.get("cwd") or str(project), created=ts)
    else:
        return None

    task.transcript_path = ev.get("transcript_path") or task.transcript_path
    task.updated = ts

    if name == "UserPromptSubmit":
        if task.threads and task.threads[-1].kind == "working":
            task.threads[-1].kind = "interrupted"
        task.threads.append(Thread(n=len(task.threads) + 1, ts=ts, prompt=ev.get("prompt") or ""))
        _set(task, status="working", live=True, inbox=False, unread=False, notification=None)

    elif name == "Stop":
        # A turn can start without a prompt: a background task that finishes, or hooks
        # installed in the middle of a session. Then the thread has no prompt.
        if not task.threads or task.threads[-1].kind != "working":
            task.threads.append(Thread(n=len(task.threads) + 1, ts=ts))
        thread = task.threads[-1]
        thread.reply = ev.get("last_assistant_message") or ""
        thread.kind = "pending"
        _set(task, status="waiting", live=True, inbox=True, unread=True, notification=None)

    elif name == "Notification":
        if ev.get("notification_type") in BLOCKING:
            _set(task, status="blocked", live=True, inbox=True, unread=True, notification=ev.get("message"))
        else:
            task.live = True

    elif name == "SessionEnd":
        task.live = False
        if task.status in ("working", "blocked"):
            task.status = "waiting"

    taskfile.save(task)
    return sid


def _set(task: Task, **values) -> None:
    for k, v in values.items():
        setattr(task, k, v)
