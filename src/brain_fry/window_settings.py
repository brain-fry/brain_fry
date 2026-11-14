"""The window's settings, in ~/.brain_fry/window.json: a plain key --> value map that the front
end reads with `stored` and writes with `store` (app.js). The sizes of the panels, the hidden
columns, the switches, the wrap of each file. Not the grid of a task: it is in the task file.
WebKit's own storage cannot hold them: pywebview wipes it at each start, and the page's origin
changes with the port (DESIGN_DETAILS.md, "App")."""

from __future__ import annotations

import json
import logging
import threading

from . import paths
from .util import write_atomic

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


def load() -> dict:
    """The map, empty when the file is missing or broken."""
    try:
        data = json.loads(paths.window_file().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("window.json is unreadable (%s): the window starts with its defaults", e)
        return {}
    return data if isinstance(data, dict) else {}


def update(changes: dict) -> dict:
    """Sets each key of `changes`; a value None removes its key. Writes only on a change."""
    with _LOCK:
        data = load()
        new = dict(data)
        for k, v in changes.items():
            if v is None:
                new.pop(str(k), None)
            else:
                new[str(k)] = v
        if new != data:
            paths.bf_home().mkdir(parents=True, exist_ok=True)
            write_atomic(paths.window_file(), json.dumps(new, indent=1, sort_keys=True) + "\n")
    return new


def script() -> str:
    """The map as a script that sets `window.__windowSettings`, which index.html loads before
    app.js: `stored` is synchronous and runs while the scripts load. json.dumps escapes every
    character outside ASCII, so the text is valid JavaScript."""
    return f"window.__windowSettings = {json.dumps(load())};\n"
