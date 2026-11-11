"""Brain Fry's own logs, in ~/.brain_fry/logs/. See DESIGN_DETAILS.md, "Logs".

- bf.log: everything that Brain Fry does (commands, applied events, task changes, digests,
  processes, errors), written by the app and by the `bf` subcommands.
- hook.log: one line per event that the hook wrote, and its errors (written by hook.sh).

Tests do not call setup(), so they write no log file.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import paths

MAX_BYTES, BACKUPS = 5 * 2**20, 5
HOOK_LOG_MAX = 2 * 2**20


def log_dir() -> Path:
    return paths.bf_home() / "logs"


def setup(role: str, debug: bool = False) -> Path:
    """role: "app", or "cli:<command>" for a `bf` subcommand. Only the app rotates the
    file: two processes that rotate the same file could lose lines."""
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / "bf.log"
    if role == "app":
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            f, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")  # fmt: skip
        _rotate_hook_log(d / "hook.log")
    else:
        handler = logging.FileHandler(f, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s.%(msecs)03d %(levelname)-7s {role}[{os.getpid()}] %(threadName)s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    ))  # fmt: skip
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)
    for noisy in ("websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return f


def _rotate_hook_log(f: Path) -> None:
    """hook.sh only appends. The app keeps one older file."""
    try:
        if f.stat().st_size > HOOK_LOG_MAX:
            f.replace(f.with_name("hook.log.1"))
    except OSError:
        pass


def short(task_id: str | None) -> str:
    return (task_id or "-")[:8]
