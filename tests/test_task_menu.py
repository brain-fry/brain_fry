"""The Task menu of the menu bar (app/taskmenu.py): what each state of the page shows."""

import asyncio

import pytest

from brain_fry.app import taskmenu
from brain_fry.app.core import Core


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(taskmenu, "STATE", dict(taskmenu.STATE))
    monkeypatch.setattr(taskmenu, "_ready", False)  # no AppKit in the tests


shown = taskmenu.items


def test_an_open_task():
    m = shown({"task": "open", "pinned": False, "autoAdvance": True})
    assert m == {"New Task": ("New Task", True, False), "Archive": ("Archive", True, False), "Fork": ("Fork", True, False),
                 "Pin": ("Pin", True, False), "Auto-advance": ("Auto-advance", True, True)}  # fmt: skip


def test_a_pinned_task_says_unpin():
    assert shown({"task": "open", "pinned": True, "autoAdvance": False})["Pin"] == ("Unpin", True, False)
    assert shown({"task": "open", "pinned": True, "autoAdvance": False})["Auto-advance"] == ("Auto-advance", True, False)


def test_an_archived_task_says_unarchive_and_cannot_be_pinned():
    m = shown({"task": "done", "pinned": False, "autoAdvance": True})
    assert m["Archive"] == ("Unarchive", True, False)
    assert m["Fork"] == ("Fork", True, False)
    assert m["Pin"] == ("Pin", False, False)


def test_no_task_grays_the_task_items():
    m = shown({"task": None, "pinned": False, "autoAdvance": True})
    assert m["New Task"][1] and m["Auto-advance"][1]
    assert not m["Archive"][1] and not m["Fork"][1] and not m["Pin"][1]


def test_every_item_has_a_tag():
    assert set(taskmenu.items(taskmenu.STATE)) == set(taskmenu.TAGS)


def test_the_page_sends_the_state(project, monkeypatch):
    """`menu.task` from the page: kept for the menu, cleaned, and no state push."""
    core = Core()  # not started: commands run, nothing in the background
    pushes = []
    monkeypatch.setattr(core, "request_broadcast", lambda: pushes.append(1))
    asyncio.run(core.command({"cmd": "menu.task", "task": "done", "pinned": 0, "autoAdvance": False}))
    assert taskmenu.STATE == {"task": "done", "pinned": False, "autoAdvance": False}
    asyncio.run(core.command({"cmd": "menu.task", "task": "nonsense", "pinned": True, "autoAdvance": True}))
    assert taskmenu.STATE == {"task": None, "pinned": True, "autoAdvance": True}
    assert pushes == []


def test_each_item_calls_the_page():
    """A click on an item runs `taskMenu(name)` in the page (pywebview runs the function in a thread)."""
    calls = []

    class Window:
        def evaluate_js(self, js):
            calls.append(js)

    (task,) = taskmenu.menu(lambda: Window())
    assert task.title == "Task"
    actions = [it for it in task.items if hasattr(it, "function")]
    assert [it.title for it in actions] == list(taskmenu.TAGS)
    for it in actions:
        it.function()
    assert calls == ['taskMenu("new")', 'taskMenu("archive")', 'taskMenu("fork")', 'taskMenu("pin")', 'taskMenu("auto")']
