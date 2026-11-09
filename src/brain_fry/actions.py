"""My actions on tasks (DESIGN.md "Status, inbox, board", "Split = fork"). Each one is a
load-modify-save under the task-file lock. No processes here: the app starts them."""

from __future__ import annotations

import uuid
from pathlib import Path

from . import paths, projects, taskfile
from .taskfile import PRIOS, Task
from .util import iso


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
        for k, v in values.items():
            setattr(task, k, v)
        taskfile.save(task)
        return task


def mark_read(task_id: str) -> Task:
    return _update(task_id, unread=False)


def dismiss(task_id: str) -> Task:
    """Out of the inbox, nothing to do (an FYI)."""
    return _update(task_id, inbox=False, unread=False)


def archive(task_id: str) -> Task:
    """Archive, or bring back an archived task. Does not end the process."""
    if load(task_id).status == "done":
        return _update(task_id, status="waiting")
    return _update(task_id, status="done", inbox=False, unread=False)


def rename(task_id: str, title: str) -> Task:
    return _update(task_id, title=title.strip())


def set_prio(task_id: str, prio: str) -> Task:
    if prio not in PRIOS:
        raise ValueError(prio)
    return _update(task_id, prio=prio)


def set_open_files(task_id: str, open_files: list[str]) -> Task:
    return _update(task_id, open_files=[str(f) for f in open_files])


def set_terminal_tabs(task_id: str, tabs: list[str]) -> Task:
    return _update(task_id, terminal_tabs=[str(t) for t in tabs])


def set_live(task_id: str, live: bool) -> Task:
    with taskfile.LOCK:
        task = load(task_id)
        task.live = live
        if not live and task.status in ("working", "blocked"):
            task.status = "waiting"
        taskfile.save(task)
        return task


def new_task(project: Path) -> Task:
    """A task started from Brain Fry: born at once, before `claude --session-id` runs."""
    project = Path(project)
    now = iso()
    task = Task(session_ids=[str(uuid.uuid4())], project=str(project), cwd=str(project),
                status="waiting", live=True, created=now, updated=now)  # fmt: skip
    with taskfile.LOCK:
        taskfile.save(task)
    return task


def fork_task(parent_id: str) -> Task:
    """The new task for `claude --resume <parent> --fork-session --session-id <new>`."""
    parent = load(parent_id)
    now = iso()
    task = Task(
        session_ids=[str(uuid.uuid4())], project=parent.project, cwd=parent.cwd,
        prio=parent.prio, status="waiting", live=True, parent=parent.id,
        digest=parent.digest, title="", created=now, updated=now,
    )  # fmt: skip
    with taskfile.LOCK:
        taskfile.save(task)
    return task
