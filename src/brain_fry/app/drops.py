"""The paths of files and folders dropped on the window from Finder or another app.
See DESIGN_DETAILS.md, "Drop from Finder".

A web page gets the names of dropped files, never their paths. pywebview's view of the page
(`WebKitHost.performDragOperation_`, platforms/cocoa.py) reads the paths from the pasteboard
of a drag that comes from another app, and keeps (name, path) pairs in `webview.dom._dnd_state`
for its own DOM events, while one of them listens to `drop`. Brain Fry does not use those
events (they would send the whole page to Python at each drop): `enable` counts as the
listener, and the page asks for the paths of a drop by the names that it got (`take`, the
command `drop.take`). Internal to pywebview 6.2: if it changes, `take` finds nothing and the
page says so. `tests/window/drop/drop_check.py` checks it in a real window.
"""

from __future__ import annotations

import logging
import os
import unicodedata

log = logging.getLogger(__name__)


def _state() -> dict | None:
    """pywebview's record of dropped paths, or None without it (tests, --browser, another version)."""
    try:
        from webview.dom import _dnd_state  # noqa: PLC0415 (pywebview only in the window)
    except Exception:
        return None
    return _dnd_state if isinstance(_dnd_state.get("paths"), list) else None


def enable() -> bool:
    """From now on pywebview records the paths of every drop that comes from another app."""
    state = _state()
    if state is None:
        log.warning("drops: pywebview does not keep the paths of dropped files, so drops from Finder do nothing")
        return False
    state["num_listeners"] = max(1, int(state.get("num_listeners") or 0))
    return True


def match(names: list[str], held: list[str]) -> list[dict]:
    """The entries of one drop: for each name that the page got, the path that pywebview recorded
    last under that name, as {path, dir}. A folder's path comes with a `/` at its end: removed.
    Names are compared in NFC (a file name on disk may be decomposed, the page's may not)."""
    nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
    left = [p.rstrip("/") or "/" for p in held]
    found = []
    for name in names:
        for i in range(len(left) - 1, -1, -1):
            if nfc(os.path.basename(left[i])) == nfc(name):
                path = left.pop(i)
                found.append({"path": path, "dir": os.path.isdir(path)})
                break
    return found


def take(names: list[str]) -> list[dict]:
    """The paths of the drop that the page just got, then nothing is kept: the paths of a drop
    that the page never asked for (it had failed) must not be given to a later one."""
    state = _state()
    if state is None:
        return []
    held = [str(item[1]) for item in list(state["paths"]) if isinstance(item, (tuple, list)) and len(item) > 1]
    del state["paths"][:]
    return match(names, held)
