"""The backend over its real websockets, with a fake `claude`. No window."""

import json
import time

import pytest
from websockets.sync.client import connect

from brain_fry import actions
from brain_fry.app.core import Core
from brain_fry.app.server import start_backend

FAKE_CLAUDE = """#!/bin/bash
echo "ARGS $*"
read -r line
echo "GOT $line"
sleep 30
"""


@pytest.fixture
def backend(project, tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    monkeypatch.setenv("BRAIN_FRY_CLAUDE", str(fake))
    core = Core()
    port = start_backend(core)
    yield core, port
    core.shutdown()


def states(ws, until, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        m = json.loads(ws.recv(timeout=end - time.time()))
        if m["type"] == "error":
            raise AssertionError(m["message"])
        if until(m):
            return m
    raise AssertionError("timeout")


def read_pty(ws, needle, timeout=5.0):
    out, end = b"", time.time() + timeout
    while needle not in out and time.time() < end:
        out += ws.recv(timeout=end - time.time())
    return out


def test_new_task_runs_in_a_pty_and_takes_input(backend, project):
    core, port = backend
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        first = json.loads(ctl.recv(timeout=5))
        assert first["type"] == "state" and first["projects"][0]["path"] == str(project)
        ctl.send(json.dumps({"cmd": "new", "project": str(project), "rows": 30, "cols": 100}))
        st = states(ctl, lambda m: m["selected"] and m["tasks"].get(m["selected"], {}).get("pty"))
        tid = st["selected"]
        task = st["tasks"][tid]
        assert (task["status"], task["live"], task["outside"]) == ("waiting", True, False)

        with connect(f"ws://127.0.0.1:{port}/pty/{tid}") as pty:
            assert f"ARGS --session-id {tid}".encode() in read_pty(pty, b"ARGS")
            pty.send(json.dumps({"t": "in", "d": "hello\r"}))
            assert b"GOT hello" in read_pty(pty, b"GOT")

        ctl.send(json.dumps({"cmd": "archive", "id": tid}))
        st = states(ctl, lambda m: tid in m["archive"])
        assert st["tasks"][tid]["status"] == "done" and st["tasks"][tid]["pty"]  # archive keeps the process


def test_select_resumes_a_task_without_live_session(backend, project):
    core, port = backend
    t = actions.new_task(project)
    actions.set_live(t.id, False)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "select", "id": t.id, "rows": 24, "cols": 80}))
        states(ctl, lambda m: m["tasks"][t.id]["pty"])
        with connect(f"ws://127.0.0.1:{port}/pty/{t.id}") as pty:
            # never prompted: nothing to resume, so the same id is started again
            assert f"ARGS --session-id {t.id}".encode() in read_pty(pty, b"ARGS")


def test_session_that_ends_is_marked_not_live(backend, project):
    core, port = backend
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "new", "project": str(project)}))
        st = states(ctl, lambda m: m["selected"] and m["tasks"].get(m["selected"], {}).get("pty"))
        tid = st["selected"]
        core.ptys[tid].hangup()
        st = states(ctl, lambda m: not m["tasks"][tid]["pty"] and not m["tasks"][tid]["live"])
        assert st["tasks"][tid]["outside"] is False


def request(ctl, req, **msg):
    """Send a command with a request number and return its reply, skipping state pushes."""
    ctl.send(json.dumps({"req": req, **msg}))
    end = time.time() + 5
    while time.time() < end:
        m = json.loads(ctl.recv(timeout=end - time.time()))
        if m["type"] == "reply" and m["req"] == req:
            return m
    raise AssertionError("no reply")


def test_file_commands(backend, project):
    core, port = backend
    t = actions.new_task(project)
    (project / "notes.md").write_text("hello\n")
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        listing = request(ctl, 1, cmd="fs.list", id=t.id)["data"]
        assert {"name": "notes.md", "path": "notes.md", "dir": False} in listing
        r = request(ctl, 2, cmd="fs.read", id=t.id, path="notes.md")["data"]
        assert r["text"] == "hello\n"
        w = request(ctl, 3, cmd="fs.write", id=t.id, path="notes.md", text="bye\n", base=r["version"])["data"]
        assert (project / "notes.md").read_text() == "bye\n"
        assert request(ctl, 4, cmd="fs.write", id=t.id, path="notes.md", text="x", base=r["version"])["data"] == {"conflict": True}
        assert request(ctl, 5, cmd="fs.versions", id=t.id, paths=["notes.md"])["data"] == {"notes.md": w["version"]}
        bad = request(ctl, 6, cmd="fs.read", id=t.id, path="../../etc/passwd")
        assert not bad["ok"] and "PermissionError" in bad["error"]
        request(ctl, 7, cmd="files.set", id=t.id, paths=["notes.md"])
        assert actions.load(t.id).open_files == ["notes.md"]


def test_thread_get(backend, project):
    from brain_fry.taskfile import Thread

    core, port = backend
    t = actions.new_task(project)
    actions._update(t.id, threads=[Thread(n=1, ts="x", kind="answer", prompt="do it", reply="Done.\nA or B?", question="A or B?")])
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        th = request(ctl, 1, cmd="thread.get", id=t.id, n=1)["data"]
        assert (th["prompt"], th["reply"], th["question"]) == ("do it", "Done.\nA or B?", "A or B?")
        assert not request(ctl, 2, cmd="thread.get", id=t.id, n=9)["ok"]


def test_terminal_tabs(backend, project, tmp_path, monkeypatch):
    core, port = backend
    fake_shell = tmp_path / "fake_shell"
    fake_shell.write_text("#!/bin/bash\necho \"SHELL in $PWD\"\nsleep 30\n")
    fake_shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_shell))
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        key = request(ctl, 1, cmd="shell.new", id=t.id, rows=10, cols=80)["data"]["key"]
        st = states(ctl, lambda m: m["tasks"][t.id]["shells"])
        assert st["tasks"][t.id]["shells"][0]["key"] == key
        assert actions.load(t.id).terminal_tabs == ["fake_shell"]
        with connect(f"ws://127.0.0.1:{port}/pty/{key}") as pty:
            assert f"SHELL in {project}".encode() in read_pty(pty, b"SHELL in")
        ctl.send(json.dumps({"cmd": "shell.close", "key": key}))
        states(ctl, lambda m: not m["tasks"][t.id]["shells"])
        time.sleep(0.3)
        assert actions.load(t.id).terminal_tabs == []


def test_unknown_command_reports_an_error(backend):
    core, port = backend
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "nope"}))
        m = json.loads(ctl.recv(timeout=5))
        assert m["type"] == "error" and "nope" in m["message"]


def test_static_files_are_served(backend):
    import urllib.request

    core, port = backend
    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
    assert "<title>Brain Fry</title>" in html
    assert urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js").status == 200
