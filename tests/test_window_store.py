"""The layout and the window's settings survive a restart of the app (2026-11-14): the grid of
each task in its task file (`grid`), the grid of a project's tabs in its project.json, and every
other setting of the window in ~/.brain_fry/window.json, served as /window.js."""

import asyncio
import json
import re
import time
import urllib.request
from pathlib import Path

import pytest
from websockets.sync.client import connect

from brain_fry import actions, paths, taskfile, window_settings
from brain_fry.app.core import QUIET, Core
from brain_fry.app.server import start_backend

FAKE_CLAUDE = "#!/bin/bash\nsleep 30\n"
WEB = Path(__file__).resolve().parents[1] / "src" / "brain_fry" / "web"
GRID = {"pane": 2, "act": {"1": "a.py", "2": "shell#0"}, "at": {"shell#0": 2, "web#0": 3}, "cols": True,
        "order": ["shell#0", "a.py", "web#0"]}  # fmt: skip


@pytest.fixture
def backend(project, tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    monkeypatch.setenv("BRAIN_FRY_CLAUDE", str(fake))
    monkeypatch.setenv("SHELL", str(fake))  # the terminal tabs of a new task: no real shell
    core = Core()
    port = start_backend(core)
    yield core, port
    core.shutdown()


def request(ctl, req, **msg):
    """Send a command with a request number and return its reply, skipping state pushes."""
    ctl.send(json.dumps({"req": req, **msg}))
    end = time.time() + 5
    while time.time() < end:
        m = json.loads(ctl.recv(timeout=end - time.time()))
        if m["type"] == "reply" and m["req"] == req:
            return m
    raise AssertionError("no reply")


def state(ctl, until, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        m = json.loads(ctl.recv(timeout=end - time.time()))
        if m["type"] == "state" and until(m):
            return m
    raise AssertionError("timeout")


def window_js(port) -> dict:
    text = urllib.request.urlopen(f"http://127.0.0.1:{port}/window.js").read().decode()
    m = re.fullmatch(r"window\.__windowSettings = (.*);\n", text, re.S)
    assert m, text
    return json.loads(m[1])


# ---------------------------------------------------------------- window.json


def test_window_settings_file(home):
    assert window_settings.load() == {}  # no file yet
    window_settings.update({"layout": {"side": 300}, "autoAdvance": False, "wrap:/p:é.md": True})
    assert paths.window_file() == home / ".brain_fry" / "window.json"
    assert window_settings.load() == {"layout": {"side": 300}, "autoAdvance": False, "wrap:/p:é.md": True}
    window_settings.update({"wrap:/p:é.md": None, "showHidden": True})  # None removes the key
    assert window_settings.load() == {"layout": {"side": 300}, "autoAdvance": False, "showHidden": True}
    script = window_settings.script()
    assert script.isascii() and script.startswith("window.__windowSettings = {")
    paths.window_file().write_text("{not json")  # broken: the defaults, never a crash
    assert window_settings.load() == {} and window_settings.script() == "window.__windowSettings = {};\n"
    paths.window_file().write_text("[1, 2]")
    assert window_settings.load() == {}


def test_window_set_and_window_js(backend):
    core, port = backend
    assert window_js(port) == {}
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert request(ctl, 1, cmd="window.set", key="layout", value={"side": 310, "agent_on": False})["ok"]
        assert request(ctl, 2, cmd="window.set", key="autoAdvance", value=False)["ok"]
        assert request(ctl, 3, cmd="window.set", key="wrap:/p:a.md", value=True)["ok"]
        assert request(ctl, 4, cmd="window.set", key="wrap:/p:a.md", value=None)["ok"]
        assert not request(ctl, 5, cmd="window.set", value=1)["ok"]  # no key
    # a new start of the app reads the file again: what the page gets before app.js runs
    assert window_js(port) == {"layout": {"side": 310, "agent_on": False}, "autoAdvance": False}
    assert {"window.set", "grid.set"} <= QUIET  # logged at DEBUG only


def test_index_loads_the_settings_before_app_js():
    html = (WEB / "index.html").read_text()
    assert 0 < html.index('src="/window.js"') < html.index('src="/app.js"')
    for js in WEB.glob("*.js"):  # every setting goes through store / stored (app.js)
        assert "localStorage." not in js.read_text(), js.name


# ---------------------------------------------------------------- the grid of a task, of a project's tabs


def test_taskfile_keeps_the_grid(project):
    t = actions.new_task(project)
    assert t.grid == {} and "grid: {}" in taskfile.dumps(t)
    actions.set_grid(t.id, GRID)
    again = actions.load(t.id)
    assert again.grid == GRID
    assert taskfile.loads(taskfile.dumps(again)).grid == GRID
    # an older task file has no `grid`; a hand edit may leave it empty
    text = paths.task_path(project, t.id).read_text()
    block = re.compile(r"(?ms)^grid:.*?(?=^---$)")  # the last field of the frontmatter
    assert block.search(text)
    assert taskfile.loads(block.sub("", text)).grid == {}
    assert taskfile.loads(block.sub("grid:\n", text)).grid == {}


def test_set_grid_drops_what_is_not_a_grid(project):
    t = actions.new_task(project)
    junk = {"pane": True, "act": {"1": "a.py", "5": "b.py", "2": 3}, "at": {"a.py": 7, "b.py": 2, "c.py": "2"},
            "cols": "yes", "order": ["a.py", 4, None], "extra": 1}  # fmt: skip
    assert actions.set_grid(t.id, junk).grid == {"pane": 1, "act": {"1": "a.py"}, "at": {"b.py": 2}, "cols": False,
                                                 "order": ["a.py"]}  # fmt: skip
    with pytest.raises(ValueError):
        actions.set_grid(t.id, ["not", "a", "grid"])


def test_grid_set_saves_without_a_state_push(backend, project):
    core, port = backend
    t = actions.new_task(project)
    actions.set_live(t.id, False)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        first = json.loads(ctl.recv(timeout=5))
        assert first["tasks"][t.id]["grid"] == {}
        assert request(ctl, 1, cmd="grid.set", id=t.id, grid=GRID)["ok"]
        assert actions.load(t.id).grid == GRID
        assert not request(ctl, 2, cmd="grid.set", id=t.id, grid="nonsense")["ok"]
        ctl.send(json.dumps({"cmd": "rename", "id": t.id, "title": "x"}))  # any command that pushes the state
        st = state(ctl, lambda m: m["tasks"][t.id]["title"] == "x")
    assert st["tasks"][t.id]["grid"] == GRID  # what the next start restores (loadGrid in grid.js)


def test_grid_set_and_window_set_push_no_state(project, monkeypatch):
    """A click on a tab saves the grid: the state is not pushed for that (the front end has it)."""
    core = Core()  # not started: commands run, nothing in the background
    pushes = []
    monkeypatch.setattr(core, "request_broadcast", lambda: pushes.append(1))
    t = actions.new_task(project)
    asyncio.run(core.command({"cmd": "grid.set", "id": t.id, "grid": GRID}))
    asyncio.run(core.command({"cmd": "grid.set", "id": f"project:{project}", "grid": GRID}))
    asyncio.run(core.command({"cmd": "window.set", "key": "autoAdvance", "value": False}))
    assert pushes == []
    asyncio.run(core.command({"cmd": "rename", "id": t.id, "title": "x"}))  # control: this one pushes
    assert pushes == [1]


def test_grid_of_project_tabs_in_project_json(backend, project):
    core, port = backend
    pid = f"project:{project}"
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert request(ctl, 1, cmd="grid.set", id=pid, grid={**GRID, "act": {"1": "p:a.py"}})["ok"]
        assert request(ctl, 2, cmd="files.set", id=pid, paths=["a.py"])["ok"]  # the lists keep the grid
        ctl.send(json.dumps({"cmd": "idle", "id": pid}))
        st = state(ctl, lambda m: True)
    data = json.loads(paths.project_tabs_file(project).read_text())
    assert data["grid"]["act"] == {"1": "p:a.py"} and data["open_files"] == ["a.py"]
    tabs = next(p for p in st["projects"] if p["path"] == str(project))["tabs"]
    assert tabs["grid"]["act"] == {"1": "p:a.py"} and tabs["openFiles"] == ["a.py"]
    actions.set_project_tabs(str(project), "browser_tabs", ["https://example.com/"])
    assert actions.project_tabs(str(project))["grid"]["order"] == GRID["order"]
    paths.project_tabs_file(project).write_text('{"grid": [1]}')  # not a grid: none
    assert actions.project_tabs(str(project))["grid"] == {}


def test_new_task_and_fork_start_with_the_grid_of_their_source(backend, project):
    core, port = backend
    src = actions.new_task(project)
    actions._update(src.id, open_files=["a.py"], terminal_tabs=["bash"])
    actions.set_grid(src.id, GRID)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "new", "project": str(project), "from": src.id, "shellRows": 10, "shellCols": 60}))
        st = state(ctl, lambda m: m["selected"] not in (None, src.id))
        new = st["selected"]
        assert st["tasks"][new]["grid"] == GRID  # in the state that announces the new task (onState, copyView)
        ctl.send(json.dumps({"cmd": "fork", "id": src.id, "shellRows": 10, "shellCols": 60}))
        st = state(ctl, lambda m: m["selected"] not in (None, src.id, new))
        assert st["tasks"][st["selected"]]["grid"] == GRID
    assert actions.load(new).grid == GRID
    other = actions.new_task(project)  # no source: an empty grid
    assert other.grid == {}
