"""My actions on tasks (DESIGN_DETAILS.md "Status, inbox, board", "Split = fork"). Each one is a
load-modify-save under the task-file lock. No processes here: the app starts them."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

from . import paths, projects, taskfile, views
from .taskfile import PRIOS, Task
from .logs import short
from .util import iso, write_atomic

log = logging.getLogger(__name__)


class NotFound(KeyError):
    pass


def locate(task_id: str) -> Path:
    for project in projects.project_paths():
        p = paths.task_path(project, task_id)
        if p.exists():
            return p
    raise NotFound(task_id)


def load(task_id: str) -> Task:
    return taskfile.load(locate(task_id))


def all_tasks() -> list[Task]:
    return [t for p in projects.project_paths() for t in taskfile.all_tasks(p)]


def _update(task_id: str, **values) -> Task:
    with taskfile.LOCK:
        task = load(task_id)
        changes = {k: (getattr(task, k), v) for k, v in values.items() if getattr(task, k) != v}
        for k, v in values.items():
            setattr(task, k, v)
        taskfile.save(task)
    log_changes(task, changes)
    return task


PRIVATE_LISTS = {"browser_tabs"}  # logged as counts: URLs are browsing history


def log_changes(task: Task, changes: dict) -> None:
    """Every change of a task field by an action, old -> new."""
    if changes:
        text = ", ".join(f"{k} {len(old)} -> {len(new)}" if k in PRIVATE_LISTS else f"{k} {_brief(old)} -> {_brief(new)}"
                         for k, (old, new) in changes.items())  # fmt: skip
        log.info("task %s %r: %s", short(task.id), task.display_title[:50], text)


def _brief(value) -> str:
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def mark_read(task_id: str) -> Task:
    return _update(task_id, unread=False)


def mark_opened(task_id: str) -> Task:
    """Selected in Dev mode: read, and the last opened task of its project, where the
    project dropdown lands. `opened` is not logged (the select command is) and does not
    touch `updated`, so the board order stays."""
    with taskfile.LOCK:
        task = load(task_id)
        changes = {"unread": (True, False)} if task.unread else {}
        task.unread, task.opened = False, iso()
        taskfile.save(task)
    log_changes(task, changes)
    return task


def archive(task_id: str) -> Task:
    """Archive, or bring back an archived task. Does not end the process. Archiving unpins:
    the task is done, and an unarchived task comes back unpinned."""
    if load(task_id).status == "done":
        return _update(task_id, status="waiting")
    return _update(task_id, status="done", inbox=False, unread=False, pinned=False)


def pin(task_id: str) -> Task:
    """Pin: the task stays in the inbox until I unpin it, whatever its flags. Or unpin a
    pinned task. An archived task cannot be pinned (it is done; unarchive it first)."""
    task = load(task_id)
    if not task.pinned and task.status == "done":
        raise ValueError("An archived task cannot be pinned: unarchive it first.")
    return _update(task_id, pinned=not task.pinned)


def rename(task_id: str, title: str) -> Task:
    """My name wins on the card too: the short title takes it, whatever its length."""
    return _update(task_id, title=title.strip(), short_title=title.strip())


def set_prio(task_id: str, prio: str) -> Task:
    if prio not in PRIOS:
        raise ValueError(prio)
    return _update(task_id, prio=prio)


def park_others(project: Path | str) -> list[str]:
    """The button of Organize mode: every task of the working column that is not in `project`
    (archived ones too) moves to next, and so leaves the inbox. Returns their ids, for Undo.
    Next, not planning (Renaud, 2026-11-13): they are interrupted work, not plans."""
    keep = str(Path(project))
    moved = []
    for t in all_tasks():
        if t.prio == "working" and str(Path(t.project)) != keep:
            set_prio(t.id, "next")
            moved.append(t.id)
    return moved


def set_open_files(task_id: str, open_files: list[str]) -> Task:
    return _update(task_id, open_files=[str(f) for f in open_files])


def set_terminal_tabs(task_id: str, tabs: list[str]) -> Task:
    return _update(task_id, terminal_tabs=[str(t) for t in tabs])


def set_browser_tabs(task_id: str, urls: list[str]) -> Task:
    return _update(task_id, browser_tabs=[str(u) for u in urls])


def set_finder_tabs(task_id: str, tabs: list[dict]) -> Task:
    return _update(task_id, finder_tabs=_finder_tabs(tabs))


def _finder_tabs(tabs: list[dict]) -> list[dict]:
    """The Finder tabs of column 3: for each, the folder of its first column (`root`) and the
    selected entry (`path`, or None), both absolute."""
    clean = []
    for t in tabs:
        root, path = (t or {}).get("root"), (t or {}).get("path")
        if not isinstance(root, str) or not root.startswith("/"):
            raise ValueError(f"not a Finder tab: {t!r}")
        clean.append({"root": root, "path": path if isinstance(path, str) and path.startswith("/") else None})
    return clean


# ---------------------------------------------------------------- project tabs
# The tabs that belong to a project rather than to one task (DESIGN_DETAILS.md, "Project tabs"):
# they show in every task of the project, on the idle screen and in the project view. Kept in
# <project>/.brain_fry/project.json, with the same lists as a task file.

PROJECT_TAB_LISTS = ("open_files", "terminal_tabs", "browser_tabs", "finder_tabs")
_PROJECT_LOCK = threading.Lock()


def project_tabs(project: str) -> dict:
    """The project's tabs: each list of PROJECT_TAB_LISTS, empty when the file is missing or broken."""
    try:
        data = json.loads(paths.project_tabs_file(project).read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: data[k] if isinstance(data.get(k), list) else [] for k in PROJECT_TAB_LISTS}


def set_project_tabs(project: str, key: str, values: list) -> dict:
    """One list of the project's tabs (`key`: one of PROJECT_TAB_LISTS)."""
    if key not in PROJECT_TAB_LISTS:
        raise ValueError(f"not a list of project tabs: {key}")
    clean = _finder_tabs(values) if key == "finder_tabs" else [str(v) for v in values]
    with _PROJECT_LOCK:
        data = project_tabs(project)
        old, data[key] = data[key], clean
        if old != clean:
            file = paths.project_tabs_file(project)
            file.parent.mkdir(exist_ok=True)
            write_atomic(file, json.dumps(data, indent=2) + "\n")
    if old != clean:
        text = f"{len(old)} -> {len(clean)}" if key in PRIVATE_LISTS else f"{_brief(old)} -> {_brief(clean)}"
        log.info("project %s: %s %s", Path(project).name, key, text)
    return data


def set_live(task_id: str, live: bool) -> Task:
    with taskfile.LOCK:
        task = load(task_id)
        if live:
            changes = {"live": (task.live, live)} if task.live != live else {}
            task.live = live
        else:
            changes = end_session(task)
        taskfile.save(task)
    log_changes(task, changes)
    return task


ENDED = ("live", "status", "inbox", "unread", "notification")


def end_session(task: Task) -> dict:
    """The task's `claude` is gone: SessionEnd, the app quit or crashed, or the process list
    no longer has it. Nothing runs any more, so working or blocked --> waiting. Its turn was
    cut off (the reply never came, or its background agents died with it), so it goes to the
    inbox, where I resume it ("Continue"). Before 2026-11-10 it only turned green, and my
    prompt had cleared `inbox`: the task left my sight after every restart of the app.
    Changes the task in memory; returns the changes, old -> new."""
    old = {k: getattr(task, k) for k in ENDED}
    task.live = False
    task.held = False  # its background shell died with it: the agent cannot go on by itself
    if task.status in ("working", "blocked"):
        task.status, task.inbox, task.unread, task.notification = "waiting", True, True, None
        if task.threads and task.threads[-1].kind == "working":
            task.threads[-1].kind = "interrupted"
    return {k: (v, getattr(task, k)) for k, v in old.items() if getattr(task, k) != v}


def settle_hold(task: Task, waits: bool) -> None:
    """The verdict on a held Stop (`held`: it left a background shell or monitor running).
    The agent waits for it and will go on by itself (the digest's kind `wait`): the task stays
    working and out of the inbox. Else the reply goes to the inbox, as the Stop would have
    done, unless I archived the task in the meantime. Changes the task in memory."""
    task.held = False
    if not waits and task.status == "working":
        task.status, task.inbox, task.unread = "waiting", True, True


def end_block(task_id: str, process_status: str, since: float) -> Task | None:
    """The dialog of a blocked task closed: I granted or refused the permission, or answered
    the question. No hook fires for it; Claude Code's process file leaves `waiting` (see
    DESIGN_DETAILS.md, "Hooks"). The task leaves the inbox, as after an answer. process_status busy:
    the agent goes on (working); idle: the turn ended (a refusal), waiting.
    None, and no change, when the task is no longer blocked (a Stop came first), or when the
    process status is older than the block (a snapshot of the files from before it)."""
    if process_status not in ("busy", "idle"):
        raise ValueError(process_status)
    with taskfile.LOCK:
        task = load(task_id)
        if task.status != "blocked" or since <= views.epoch(task.updated):  # updated: the time of the Notification
            return None
        new = {"status": "working" if process_status == "busy" else "waiting",
               "inbox": False, "unread": False, "notification": None}  # fmt: skip
        changes = {k: (getattr(task, k), v) for k, v in new.items() if getattr(task, k) != v}
        for k, v in new.items():
            setattr(task, k, v)
        taskfile.save(task)
    log_changes(task, changes)
    return task


def new_task(project: Path, like: str | None = None) -> Task:
    """A task started from Brain Fry: born at once, before `claude --session-id` runs.
    `like`: a task of the same project whose workspace it starts with, its file, browser and
    terminal tabs (the front end picks it: the previous task, see newTask in app.js). A task
    of another project, or one that does not exist, is ignored.
    `opened` is set: the backend selects the new task (see mark_opened)."""
    project = Path(project)
    now = iso()
    task = Task(session_ids=[str(uuid.uuid4())], project=str(project), cwd=str(project),
                status="waiting", live=True, created=now, updated=now, opened=now)  # fmt: skip
    src = _workspace_source(like, project)
    if src:
        task.open_files, task.browser_tabs = list(src.open_files), list(src.browser_tabs)
        task.terminal_tabs = list(src.terminal_tabs)
        task.finder_tabs = [dict(t) for t in src.finder_tabs]
    with taskfile.LOCK:
        taskfile.save(task)
    copied = (f", with the tabs of {short(src.id)}: {len(task.open_files)} files, {len(task.browser_tabs)} browser,"
              f" {len(task.terminal_tabs)} terminals, {len(task.finder_tabs)} Finder" if src else "")  # fmt: skip
    log.info("task %s created in %s (new session%s)", short(task.id), project.name, copied)
    return task


def plan_task(project: Path, title: str, prio: str, notes: str = "") -> Task:
    """A task created in Organize mode: a card on the board, no session yet. Selecting it
    later starts `claude --session-id <id>` (core._resume_argv: a task without a thread).
    My title is the card's heading, as after a rename: the digest keeps it. My notes are
    the card's text until the first digest, and stay in the file. Not `opened`, not
    selected, no tabs copied (they would come from whatever task was open when I planned it)."""
    title, notes = title.strip(), notes.strip()
    if not title:
        raise ValueError("a planned task needs a title")
    if prio not in PRIOS:
        raise ValueError(prio)
    project = Path(project)
    now = iso()
    task = Task(session_ids=[str(uuid.uuid4())], project=str(project), cwd=str(project), title=title,
                short_title=title, prio=prio, status="waiting", live=False, notes=notes,
                created=now, updated=now)  # fmt: skip
    with taskfile.LOCK:
        taskfile.save(task)
    log.info("task %s planned in %s, %s: %r, notes of %d chars (no session yet)", short(task.id), project.name, prio,
             title[:50], len(notes))  # fmt: skip
    return task


def _workspace_source(like: str | None, project: Path) -> Task | None:
    if not like:
        return None
    try:
        src = load(like)
    except NotFound:
        log.warning("new task: no task %s to copy the tabs of", short(like))
        return None
    if Path(src.project).resolve() != project.resolve():  # its paths belong to another project
        log.warning("new task: %s is in %s, not in %s: tabs not copied", short(like), Path(src.project).name, project.name)
        return None
    return src


def fork_task(parent_id: str) -> Task:
    """The new task for `claude --resume <parent> --fork-session --session-id <new>`. It starts
    with the parent's tabs (file, browser, terminal), as a new task with the previous one's."""
    parent = load(parent_id)
    now = iso()
    task = Task(
        session_ids=[str(uuid.uuid4())], project=parent.project, cwd=parent.cwd,
        prio=parent.prio, status="waiting", live=True, parent=parent.id,
        digest=parent.digest, title="", created=now, updated=now, opened=now,  # opened: as new_task
        open_files=list(parent.open_files), browser_tabs=list(parent.browser_tabs),
        terminal_tabs=list(parent.terminal_tabs), finder_tabs=[dict(t) for t in parent.finder_tabs],
    )  # fmt: skip
    with taskfile.LOCK:
        taskfile.save(task)
    log.info("task %s created in %s: fork of %s %r, with its tabs: %d files, %d browser, %d terminals, %d Finder",
             short(task.id), Path(task.project).name, short(parent.id), parent.display_title[:50], len(task.open_files),
             len(task.browser_tabs), len(task.terminal_tabs), len(task.finder_tabs))  # fmt: skip
    return task
