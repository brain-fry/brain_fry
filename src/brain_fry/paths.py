"""Where Brain Fry keeps its files. BRAIN_FRY_HOME overrides ~/.brain_fry (used by tests)."""

import os
from pathlib import Path


def bf_home() -> Path:
    return Path(os.environ.get("BRAIN_FRY_HOME") or Path.home() / ".brain_fry")


def projects_file() -> Path:
    return bf_home() / "projects.json"


def hook_file() -> Path:
    return bf_home() / "hook.sh"


def inbox_prompt_file() -> Path:
    return bf_home() / "inbox_prompt.md"


def project_dir(project: Path) -> Path:
    return Path(project) / ".brain_fry"


def project_tabs_file(project: Path) -> Path:
    """The tabs that belong to the project, not to one task (actions.project_tabs)."""
    return project_dir(project) / "project.json"


def spool_dir(project: Path) -> Path:
    """One file per hook event, written by hook.sh, deleted by the reader once applied."""
    return project_dir(project) / "events"


def consume_lock(project: Path) -> Path:
    """Held (flock) by the reader while it applies the spool: one reader at a time per project."""
    return project_dir(project) / "consume.lock"


def event_log(project: Path) -> Path:
    """Every applied event, appended by the reader (the only writer)."""
    return project_dir(project) / "events.jsonl"


def task_path(project: Path, task_id: str) -> Path:
    return project_dir(project) / f"{task_id}.md"
