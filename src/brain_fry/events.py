"""The reader: applies the hook events in the spool to task files. See DESIGN_DETAILS.md, "Hooks".

Order per event file: apply it to the task file, append it to events.jsonl, delete it.
A crash between the first and the last step applies that event twice on the next run.
"""

from __future__ import annotations

import fcntl
import json
import logging
import time
from pathlib import Path

from . import actions, paths, sessions, taskfile
from .taskfile import Task, Thread
from .logs import short
from .util import iso

log = logging.getLogger(__name__)

BLOCKING = {"permission_prompt", "elicitation_dialog"}
CREATING = {"UserPromptSubmit", "Stop", "Notification"}  # SessionEnd alone creates no task


def pending(project: Path) -> list[tuple[Path, dict]]:
    """Event files in the spool, oldest first. Unreadable files are renamed *.bad."""
    out = []
    for f in paths.spool_dir(project).glob("*.json"):
        try:
            out.append((f, json.loads(f.read_text())))
        except (OSError, json.JSONDecodeError):
            log.warning("unreadable event file %s: set aside as .bad", f)
            f.rename(f.with_suffix(".bad"))
    out.sort(key=lambda fe: (fe[1].get("bf_ts", 0), fe[0].name))
    return out


def consume(project: Path, on_event=None) -> list[str]:
    """Apply every pending event of the project. Returns the ids of the tasks that changed.
    on_event(task_id, event) is called after each applied event (the app's auto-advance).

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
            if task_id and on_event:
                on_event(task_id, ev)
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
        before = _tracked(task)
    elif name in CREATING:
        task = Task(session_ids=[sid], project=str(project), cwd=ev.get("cwd") or str(project), created=ts)
        _import_history(task, name, ev)
        before = None
    else:
        log.info("event %s %s in %s: ignored, no task (a session end alone creates none)", name, short(sid), project.name)
        return None

    task.transcript_path = ev.get("transcript_path") or task.transcript_path
    task.updated = ts

    if name == "UserPromptSubmit":
        if task.threads and task.threads[-1].kind == "working":
            task.threads[-1].kind = "interrupted"
        task.threads.append(Thread(n=len(task.threads) + 1, ts=ts, prompt=ev.get("prompt") or ""))
        _set(task, status="working", live=True, inbox=False, unread=False, notification=None, held=False)

    elif name == "Stop":
        # A turn can start without a prompt: a background task that finishes, or hooks
        # installed in the middle of a session. Then the thread has no prompt.
        if not task.threads or task.threads[-1].kind != "working":
            task.threads.append(Thread(n=len(task.threads) + 1, ts=ts))
        thread = task.threads[-1]
        thread.reply = ev.get("last_assistant_message") or ""
        thread.kind = "pending"
        task.held = False
        if closes(thread):  # archived (and unpinned), as by my key; the digest still runs on the thread
            _set(task, status="done", live=True, inbox=False, unread=False, pinned=False, notification=None)
            log.info("task %s: closed by my `close` prompt: archived", short(task.id))
        elif waiting_for := background_work(ev):
            # The turn is over, but the agent is not done: it waits for its background agents,
            # and their results start the next turn. Out of the inbox until a Stop without any.
            _set(task, status="working", live=True, inbox=False, unread=False, notification=None)
            log.info("task %s: not in the inbox: waits for %d background task(s) (%s)", short(task.id),
                     len(waiting_for), ", ".join(sorted({str(b.get("type")) for b in waiting_for})))  # fmt: skip
        elif running := background_shells(ev):
            # A background shell or monitor runs. The agent may wait for it (a loop that waits for
            # another session, then goes on) or not (a dev server). The digest tells (kind `wait`):
            # out of the inbox until then (actions.settle_hold).
            _set(task, status="working", live=True, inbox=False, unread=False, notification=None, held=True)
            log.info("task %s: out of the inbox until the digest: %d background %s run(s)", short(task.id),
                     len(running), "/".join(sorted({str(b.get("type")) for b in running})))  # fmt: skip
        else:
            _set(task, status="waiting", live=True, inbox=True, unread=True, notification=None)

    elif name == "Notification":
        if ev.get("notification_type") in BLOCKING:
            _set(task, status="blocked", live=True, inbox=True, unread=True, notification=ev.get("message"), held=False)
        else:
            task.live = True

    elif name == "SessionEnd":  # a turn cut off goes to the inbox; the same rule as the app's quit
        actions.end_session(task)

    taskfile.save(task)
    _log(name, ev, project, task, before)
    return sid


# Prompts that Claude Code writes itself to start a turn: the result of a background agent,
# or of a background shell. Seen in events.jsonl, 2026-11-09 and 10.
MACHINE_PROMPTS = ("<agent-message", "<task-notification")


def typed_by_me(ev: dict) -> bool:
    """A UserPromptSubmit whose prompt I wrote, not one with which Claude Code starts a turn
    by itself. The payload's `source` tells (`user`; or `sdk`, `system`, `loop_wakeup`, ...:
    the schema in the binary of 2.1.322), but no payload has had it yet: it is rolling out.
    Without it, the start of the prompt tells."""
    source = ev.get("source")
    if source is not None:
        return source == "user"
    return not (ev.get("prompt") or "").lstrip().startswith(MACHINE_PROMPTS)


IN_FLIGHT = {"running", "pending"}


def _in_flight(ev: dict) -> list[dict]:
    return [b for b in ev.get("background_tasks") or [] if isinstance(b, dict) and b.get("status") in IN_FLIGHT]


def _is_agent(b: dict) -> bool:
    kind = str(b.get("type") or "")
    return kind == "workflow" or "agent" in kind


def background_work(ev: dict) -> list[dict]:
    """The background agents and workflows that still run at this Stop: the agent will go on
    when they finish. Stop's `background_tasks` lists all background work in flight. Claude
    Code's own "Waiting for N background agents and M dynamic workflows to finish" counts
    agents and workflows only, and so does this: a background shell or monitor (a dev server,
    a watch) may never end, and would keep its task out of the inbox for ever. Types seen:
    subagent, shell, monitor; the schema also names workflow (Claude Code 2.1.322)."""
    return [b for b in _in_flight(ev) if _is_agent(b)]


def background_shells(ev: dict) -> list[dict]:
    """The rest of the background work in flight at this Stop: shells, monitors, unknown
    types. The agent may wait for one of them, or not: the digest tells (kind `wait`)."""
    return [b for b in _in_flight(ev) if not _is_agent(b)]


CLOSED_MARK = "closed :-)"
CLOSE_PROMPTS = ("close", "x")


def closes(thread: Thread) -> bool:
    """The turn of my `close` prompt (or just `x`), when the agent confirmed with "closed :-)":
    it wrote down what the conversation found (~/.claude/CLAUDE.md, "close") and the task is
    over. Without the mark (the agent asked something back, or was interrupted), the task goes
    to the inbox as usual. The mark may be in the last line or, when the reply ends with a
    trailer (`[needs: fyi] ...`), in the line before it."""
    if thread.prompt.strip().rstrip(".!").strip().lower() not in CLOSE_PROMPTS:
        return False
    lines = [line for line in thread.reply.splitlines() if line.strip()]
    return any(CLOSED_MARK in line.lower() for line in lines[-2:])


def _import_history(task: Task, name: str, ev: dict) -> None:
    """A session that becomes a task at this event may have turns before it: its project was
    registered in the middle of the session, or it started before `bf install` and was resumed
    since. They come from its transcript (sessions.read_turns). The turn of this event is told apart by
    prompt_id (the promptId of its prompt in the transcript), else by the prompt's text."""
    path = Path(ev.get("transcript_path") or "")
    if not path.is_file():
        return
    try:
        title, threads, ids = sessions.read_turns(path)
    except OSError as e:
        log.warning("task %s: could not read the earlier turns in %s: %s", short(task.id), path, e)
        return
    task.title = title or task.title
    if not threads:
        return
    current = ev.get("prompt_id")
    last_is_current = ids[-1] == current if current and ids[-1] else None  # None: cannot tell
    if name == "UserPromptSubmit":
        same_text = " ".join(threads[-1].prompt.split()) == " ".join((ev.get("prompt") or "").split())
        if last_is_current or (last_is_current is None and same_text):
            threads.pop()  # this prompt is in the transcript already; the event adds it
    elif name == "Stop" or ev.get("notification_type") in BLOCKING:
        if last_is_current is not False:  # False: a turn without a prompt (a background task)
            threads[-1].kind = "working"  # the turn of this event: Stop fills in the reply
    else:  # an idle notification: the last turn is over
        threads[-1].kind = "pending" if threads[-1].reply else "interrupted"
    task.threads = threads
    if threads:
        log.info("task %s: %d earlier turn(s) imported from %s", short(task.id), len(threads), path)


TRACKED = ("status", "live", "inbox", "unread")


def _tracked(task: Task) -> dict:
    return {k: getattr(task, k) for k in TRACKED} | {"threads": len(task.threads)}


def _log(name: str, ev: dict, project: Path, task: Task, before: dict | None) -> None:
    """One line per applied event: what it was, and what it changed in the task."""
    detail = ev.get("notification_type") or ev.get("reason") or ""
    if name == "Notification" and ev.get("message"):
        detail += f": {ev['message']}"
    after = _tracked(task)
    if before is None:
        change = "new task, " + ", ".join(f"{k} {after[k]}" for k in TRACKED)
    else:
        change = ", ".join(f"{k} {before[k]} -> {after[k]}" for k in TRACKED if before[k] != after[k]) or "no change"
    if task.threads and (before is None or after["threads"] != before["threads"] or name == "Stop"):
        th = task.threads[-1]
        change += f"; thread {th.n} {th.kind}"
    log.info("event %s%s %s %r in %s: %s", name, f" ({detail[:120]})" if detail else "", short(task.id),
             task.display_title[:50], project.name, change)  # fmt: skip


def _set(task: Task, **values) -> None:
    for k, v in values.items():
        setattr(task, k, v)
