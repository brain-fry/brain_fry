"""Quitting: the one question at quit, the shutdown that runs once, the files with unsaved edits
that the front end reports (files.dirty), and no state push once the shutdown has begun. The
ways of quitting themselves (Cmd+Q, the close button, signals) need the window: DESIGN_DETAILS.md,
"App", has how they were checked."""

import json
import subprocess
import sys

import pytest
from websockets.sync.client import connect

from brain_fry.app import core as core_mod
from brain_fry.app import main
from brain_fry.app.core import Core
from brain_fry.app.server import start_backend


def test_no_question_when_nothing_is_lost():
    assert main.quit_question(0, []) == ""


def test_question_names_the_working_tasks_and_the_unsaved_files():
    assert main.quit_question(1, []) == "1 task is working. Quitting ends its turn; --resume brings the conversation back."
    assert main.quit_question(2, []).startswith("2 tasks are working. Quitting ends their turn")
    assert main.quit_question(0, ["a.py"]) == "1 file has unsaved edits, which quitting loses: a.py."
    both = main.quit_question(3, ["a.py", "Untitled-1"])
    working, files = both.split("\n\n")
    assert working.startswith("3 tasks are working.")
    assert files == "2 files have unsaved edits, which quitting loses: a.py, Untitled-1."


def test_question_cuts_the_list_of_files():
    names = [f"f{i}.txt" for i in range(8)]
    text = main.quit_question(0, names)
    assert text == "8 files have unsaved edits, which quitting loses: f0.txt, f1.txt, f2.txt, f3.txt, f4.txt and 3 more."


class FakeCore:
    def __init__(self, browser_fails=False):
        self.calls = []
        self.browser = self
        self.browser_fails = browser_fails

    def shutdown(self):  # both core.shutdown and core.browser.shutdown
        self.calls.append("shutdown")
        if self.browser_fails and len(self.calls) == 1:
            raise RuntimeError("no cookies")


def test_the_shutdown_runs_once(caplog):
    caplog.set_level("INFO")
    core = FakeCore()
    quit_app = main._Quit(core)
    assert not quit_app.started
    quit_app("window closed")
    quit_app("Cocoa quits the app")
    assert quit_app.started
    assert core.calls == ["shutdown", "shutdown"]  # the browser's, then the core's: once each
    lines = [r.getMessage() for r in caplog.records if r.name == "brain_fry.app.main"]
    assert lines == ["quitting: window closed", "quit"]


def test_a_failed_cookie_save_does_not_stop_the_shutdown():
    core = FakeCore(browser_fails=True)
    main._Quit(core)("SIGTERM")
    assert core.calls == ["shutdown", "shutdown"]


def test_the_quit_ends_the_claude_version_probe(monkeypatch):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(main, "_probes", [p])
    main._Quit(FakeCore())("SIGHUP")
    assert p.wait(timeout=5) < 0  # killed


class FakeLoop:
    def __init__(self):
        self.calls = 0

    def call_soon_threadsafe(self, *a):
        self.calls += 1


def test_no_state_push_once_the_shutdown_began(home):
    core = Core()
    core.loop = FakeLoop()
    core.request_broadcast()
    core.notify({"type": "x"})
    assert core.loop.calls == 2
    core.shutdown()
    core.request_broadcast()
    core.notify({"type": "x"})
    assert core.loop.calls == 2


@pytest.fixture
def backend(project):
    core = Core()
    port = start_backend(core)
    yield core, port
    core.shutdown()


def test_the_front_end_reports_the_files_with_unsaved_edits(backend, caplog):
    core, port = backend
    caplog.set_level("DEBUG")
    assert "files.dirty" in core_mod.QUIET  # sent at each change: logged at DEBUG only
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        json.loads(ctl.recv(timeout=5))
        for req, files in ((1, ["a.py", "Untitled-1"]), (2, [])):
            ctl.send(json.dumps({"cmd": "files.dirty", "req": req, "files": files}))
            while (m := json.loads(ctl.recv(timeout=5))).get("req") != req:
                pass
            assert m["type"] == "reply" and m.get("data") is None
            assert core.dirty_files == files
    assert not [r for r in caplog.records if r.levelname == "INFO" and "files.dirty" in r.getMessage()]
