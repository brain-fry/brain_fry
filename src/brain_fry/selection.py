"""The selection in 3a, given to Claude Code as context with my next prompt (DESIGN_DETAILS.md,
"Selection as context").

The front end sends the selection of the selected task's active file tab each time it
changes. This module keeps it in ~/.brain_fry/selection.json. At UserPromptSubmit, the hook
(hook.sh) prints it when the prompt's session is the file's task and this app still runs:
Claude Code adds what a UserPromptSubmit hook prints to the model's context.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import files, paths
from .util import write_atomic

MAX_CHARS = 10_000  # longer selections are cut; the hook says so, and Claude can read the file


def selection_file() -> Path:
    return paths.bf_home() / "selection.json"


def write(task, rel: str, first: int, last: int, text: str, total: int, dirty: bool) -> dict:
    """`rel` is relative to the task's project (as in 3a). The hook shows the path relative
    to the session's cwd (where Claude runs), or absolute when the file is outside it."""
    p = files.resolve(task.project, rel)
    cwd = Path(task.cwd or task.project).resolve()
    shown = str(p.relative_to(cwd)) if cwd in p.parents else str(p)
    data = {"session_id": task.id, "pid": os.getpid(), "path": shown, "from": first, "to": last,
            "text": text[:MAX_CHARS], "cut": max(total, len(text)) > MAX_CHARS, "dirty": dirty}  # fmt: skip
    paths.bf_home().mkdir(parents=True, exist_ok=True)
    write_atomic(selection_file(), json.dumps(data))
    return data


def clear() -> None:
    selection_file().unlink(missing_ok=True)
