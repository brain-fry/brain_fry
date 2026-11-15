"""The Task menu of the menu bar (asked by Renaud on 2026-11-15, it replaced the buttons of the
header): New Task, Archive, Fork, Pin, Auto-advance. Each item calls `taskMenu(name)` in the page.

pywebview's menus have fixed titles, no check mark and no disabled item. So the page sends what
the menu must show (`menu.task`, at each change), `update` keeps it, and `apply` sets the titles
(Unarchive, Unpin), the grayed items and the check mark of Auto-advance on the menu items
(AppKit, main thread). `prepare` runs inside pywebview's builder of the menu bar (main.py), so a
rebuilt bar gets them too. Internal to pywebview 6.2: if it changes, the items stay plain (always
enabled, no check mark), and the page ignores an item that has no task to act on."""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

TITLE = "Task"
# the items' first titles --> their tags, by which `apply` finds them again after a new title
TAGS = {"New Task": 1, "Archive": 2, "Fork": 3, "Pin": 4, "Auto-advance": 5}
STATE = {"task": None, "pinned": False, "autoAdvance": True}  # task: None, "open" or "done"
_ready = False  # the bar has a prepared Task menu: an update may go to AppKit


def items(state: dict) -> dict[str, tuple[str, bool, bool]]:
    """The first title of each item --> (title, enabled, checked) for this state."""
    task = state.get("task")
    done = task == "done"
    return {
        "New Task": ("New Task", True, False),
        "Archive": ("Unarchive" if done else "Archive", bool(task), False),
        "Fork": ("Fork", bool(task), False),
        "Pin": ("Unpin" if state.get("pinned") else "Pin", bool(task) and not done, False),  # an archived task cannot be pinned
        "Auto-advance": ("Auto-advance", True, bool(state.get("autoAdvance"))),
    }


def update(msg: dict) -> None:
    """The command `menu.task` (the page, at each change of the selected task or of the switch)."""
    task = msg.get("task")
    STATE.update(task=task if task in ("open", "done") else None, pinned=bool(msg.get("pinned")),
                 autoAdvance=bool(msg.get("autoAdvance")))  # fmt: skip
    if _ready:
        try:
            from PyObjCTools import AppHelper  # noqa: PLC0415

            AppHelper.callAfter(apply)
        except Exception:
            log.debug("Task menu not updated", exc_info=True)


def menu(get_window) -> list:
    """The menu for pywebview's `create_window(menu=...)`. No key equivalents, as in the File menu:
    a key of the menu bar would never reach the page."""
    from webview.menu import Menu, MenuAction, MenuSeparator  # noqa: PLC0415

    def item(title: str, name: str) -> MenuAction:
        def run():  # pywebview runs it in a thread of its own
            log.info("menu: Task > %s", items(STATE)[title][0])
            try:
                get_window().evaluate_js(f"taskMenu({json.dumps(name)})")
            except Exception:
                log.warning("menu action failed", exc_info=True)

        return MenuAction(title, run)

    return [Menu(TITLE, [item("New Task", "new"), MenuSeparator(), item("Archive", "archive"), item("Fork", "fork"),
                         item("Pin", "pin"), MenuSeparator(), item("Auto-advance", "auto")])]  # fmt: skip


def _submenu(bar):
    i = bar.indexOfItemWithTitle_(TITLE)
    return bar.itemAtIndex_(i).submenu() if i >= 0 else None


def prepare(bar) -> None:
    """In pywebview's builder (main thread), on the new bar: tags, no automatic enabling, the state."""
    global _ready  # noqa: PLW0603
    sub = _submenu(bar)
    if sub is None:
        return
    sub.setAutoenablesItems_(False)  # else AppKit enables every item whose target answers its action
    for it in sub.itemArray():
        if it.title() in TAGS:
            it.setTag_(TAGS[it.title()])
    apply(bar)
    _ready = True


def apply(bar=None) -> None:
    """Sets the titles, the grayed items and the check mark from STATE (main thread)."""
    try:
        if bar is None:
            from AppKit import NSApp  # noqa: PLC0415

            bar = NSApp().mainMenu()
        sub = bar and _submenu(bar)
        if sub is None:
            return
        for first, (title, enabled, checked) in items(STATE).items():
            it = sub.itemWithTag_(TAGS[first])
            if it is not None:
                it.setTitle_(title)
                it.setEnabled_(enabled)
                it.setState_(1 if checked else 0)  # NSControlStateValueOn / Off
    except Exception:
        log.debug("Task menu not applied", exc_info=True)
