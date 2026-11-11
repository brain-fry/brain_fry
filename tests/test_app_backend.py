"""The backend over its real websockets, with a fake `claude`. No window."""

import json
import time

import pytest
from websockets.sync.client import connect

from brain_fry import actions, events
from brain_fry.app.core import Core
from brain_fry.app.server import start_backend
from brain_fry.taskfile import Thread

FAKE_CLAUDE = """#!/bin/bash
echo "ARGS $* HYPERLINK=$FORCE_HYPERLINK"
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
            out = read_pty(pty, b"HYPERLINK=")
            assert f"ARGS --session-id {tid}".encode() in out
            assert b"HYPERLINK=1" in out  # Claude Code writes its links as OSC 8
            pty.send(json.dumps({"t": "in", "d": "hello\r"}))
            assert b"GOT hello" in read_pty(pty, b"GOT")

        ctl.send(json.dumps({"cmd": "archive", "id": tid}))
        st = states(ctl, lambda m: m["tasks"][tid]["status"] == "done")
        assert st["tasks"][tid]["pty"]  # archive keeps the process


def test_new_task_with_the_tabs_of_the_previous_task(backend, project):
    core, port = backend
    prev = actions.new_task(project)
    actions._update(prev.id, open_files=["a.py"], browser_tabs=["https://example.com/"], terminal_tabs=["zsh", "zsh"])
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "new", "project": str(project), "from": prev.id, "shellRows": 10, "shellCols": 60}))
        st = states(ctl, lambda m: m["selected"] not in (None, prev.id) and len(m["tasks"][m["selected"]]["shells"]) == 2)
        task = st["tasks"][st["selected"]]
        assert (task["openFiles"], task["browserTabs"]) == (["a.py"], ["https://example.com/"])
        assert actions.load(task["id"]).terminal_tabs == ["zsh", "zsh"]
        assert st["tasks"][prev.id]["shells"] == []  # the previous task's own tabs are not started


def test_fork_starts_the_terminal_tabs_of_its_parent(backend, project):
    core, port = backend
    parent = actions.new_task(project)
    actions._update(parent.id, terminal_tabs=["zsh"])
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "fork", "id": parent.id, "shellRows": 10, "shellCols": 60}))
        st = states(ctl, lambda m: m["selected"] not in (None, parent.id) and len(m["tasks"][m["selected"]]["shells"]) == 1)
        assert st["tasks"][st["selected"]]["parent"] == parent.id


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
        assert actions.load(t.id).opened  # the project dropdown lands on it
        # the dropdown picked a project without a task to land on: nothing selected
        ctl.send(json.dumps({"cmd": "select", "id": None}))
        states(ctl, lambda m: m["selected"] is None)


def test_idle_starts_the_terminal_tabs_of_the_task_without_its_session(backend, project, tmp_path, monkeypatch):
    # nothing needs me: column 3 shows the tabs of the task that I opened last, 4a a photo
    core, port = backend
    fake_shell = tmp_path / "fake_shell"
    fake_shell.write_text("#!/bin/bash\nsleep 30\n")
    fake_shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_shell))
    t = actions.new_task(project)
    actions.set_live(t.id, False)
    actions._update(t.id, terminal_tabs=["zsh", "zsh"])
    opened = actions.load(t.id).opened
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "idle", "id": t.id, "shellRows": 10, "shellCols": 60}))
        st = states(ctl, lambda m: len(m["tasks"][t.id]["shells"]) == 2)
        assert st["selected"] is None and not st["tasks"][t.id]["pty"]  # no session started
        assert actions.load(t.id).opened == opened
        # once per run: a second idle or the select that follows starts no other shell
        ctl.send(json.dumps({"cmd": "idle", "id": t.id}))
        ctl.send(json.dumps({"cmd": "select", "id": t.id, "rows": 24, "cols": 80}))
        st = states(ctl, lambda m: m["tasks"][t.id]["pty"])
        assert len(st["tasks"][t.id]["shells"]) == 2


def test_plan_puts_a_card_on_the_board_without_a_session(backend, project):
    """Organize mode: a new task is a card with my title, no `claude`; opening it starts one."""
    core, port = backend
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        tid = request(ctl, 1, cmd="plan", project=str(project), title="  Spec review mode ", prio="planning",
                      notes="Like SourceTree.\n- stage hunks\n")["data"]["id"]  # fmt: skip
        st = states(ctl, lambda m: tid in m.get("tasks", {}))
        task = st["tasks"][tid]
        assert (task["title"], task["shortTitle"], task["prio"]) == ("Spec review mode", "Spec review mode", "planning")
        assert task["notes"] == actions.load(tid).notes == "Like SourceTree.\n- stage hunks"
        assert (task["live"], task["pty"], task["inbox"], st["selected"]) == (False, None, False, None)
        assert tid not in core.ptys and not actions.load(tid).opened
        assert not request(ctl, 2, cmd="plan", project=str(project), title="  ", prio="working")["ok"]
        assert not request(ctl, 3, cmd="plan", project=str(project), title="x", prio="soon")["ok"]
        ctl.send(json.dumps({"cmd": "select", "id": tid, "rows": 24, "cols": 80}))
        states(ctl, lambda m: m.get("tasks", {}).get(tid, {}).get("pty"))
        with connect(f"ws://127.0.0.1:{port}/pty/{tid}") as pty:
            assert f"ARGS --session-id {tid}".encode() in read_pty(pty, b"ARGS")


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
        link = request(ctl, 8, cmd="fs.link", id=t.id, target="notes.md#L1")["data"]
        assert link == {"path": "notes.md", "line": 1}
        bad = request(ctl, 9, cmd="fs.link", id=t.id, target="../../etc/passwd")
        assert not bad["ok"] and "PermissionError" in bad["error"]
        request(ctl, 7, cmd="files.set", id=t.id, paths=["notes.md"])
        assert actions.load(t.id).open_files == ["notes.md"]


def test_file_commands_for_a_project_without_a_task(backend, project, tmp_path):
    """A project picked while it has no open task: 2a and the file tabs use `project:<path>`."""
    import urllib.error
    import urllib.parse
    import urllib.request

    core, port = backend
    (project / "notes.md").write_text("hello\n")
    (project / "plot.png").write_bytes(b"\x89PNG fake")
    pid = f"project:{project}"
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert {"name": "notes.md", "path": "notes.md", "dir": False} in request(ctl, 1, cmd="fs.list", id=pid)["data"]
        r = request(ctl, 2, cmd="fs.read", id=pid, path="notes.md")["data"]
        request(ctl, 3, cmd="fs.write", id=pid, path="notes.md", text="bye\n", base=r["version"])
        assert (project / "notes.md").read_text() == "bye\n"
        bad = request(ctl, 4, cmd="fs.read", id=pid, path="../../etc/passwd")
        assert not bad["ok"] and "PermissionError" in bad["error"]
        (tmp_path / "elsewhere").mkdir()
        bad = request(ctl, 5, cmd="fs.list", id=f"project:{tmp_path / 'elsewhere'}")  # not a registered project
        assert not bad["ok"] and "NotFound" in bad["error"]
    base = f"http://127.0.0.1:{port}/file/{urllib.parse.quote(pid, safe='')}"
    assert urllib.request.urlopen(f"{base}/plot.png?v=1").read() == b"\x89PNG fake"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/file/{urllib.parse.quote(f'project:{tmp_path}', safe='')}/a.png")
    assert e.value.code == 404


def test_save_as_asks_the_save_panel_for_a_path_in_the_project(backend, project):
    """An untitled tab (Cmd+N F): fs.saveas shows the save panel, the front end then writes."""
    core, port = backend
    t = actions.new_task(project)
    asked = []

    class Window:  # pywebview's, in the app
        answer = str(project / "docs" / "new.md")

        def create_file_dialog(self, kind, directory, save_filename):
            asked.append((kind, directory, save_filename))
            return self.answer

    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert not request(ctl, 1, cmd="fs.saveas", id=t.id, name="Untitled-1")["ok"]  # no window: --browser
        core.window = Window()
        assert request(ctl, 2, cmd="fs.saveas", id=t.id, name="Untitled-1")["data"] == {"path": "docs/new.md"}
        assert asked == [(30, str(project), "Untitled-1")]
        Window.answer = None  # cancelled
        assert request(ctl, 3, cmd="fs.saveas", id=t.id, name="Untitled-1")["data"] is None
        Window.answer = "/tmp/outside.md"
        bad = request(ctl, 4, cmd="fs.saveas", id=t.id, name="Untitled-1")
        assert not bad["ok"] and "outside the project" in bad["error"]


def test_commands_of_the_explorer_menu(backend, project, monkeypatch):
    """Rename, Trash, Paste (also from a task of another project), Copy path."""
    core, port = backend
    t = actions.new_task(project)
    (project / "notes.md").write_text("hello\n")
    (project / "docs").mkdir()
    from brain_fry import projects

    other = project.parent / "other_project"
    other.mkdir()
    projects.init(other)
    t2 = actions.new_task(other)
    trashed, clip = [], []
    monkeypatch.setattr("brain_fry.files._move_to_trash", trashed.append)
    monkeypatch.setattr("brain_fry.files.copy_to_clipboard", clip.append)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert request(ctl, 1, cmd="fs.rename", id=t.id, path="notes.md", name="todo.md")["data"] == "todo.md"
        taken = request(ctl, 2, cmd="fs.rename", id=t.id, path="todo.md", name="docs")
        assert not taken["ok"] and taken["error"].startswith("FileExistsError")
        assert request(ctl, 3, cmd="fs.paste", id=t.id, **{"from": t.id}, paths=["todo.md"], dir="docs", cut=False)["data"] == [["todo.md", "docs/todo.md"]]
        assert request(ctl, 4, cmd="fs.paste", id=t2.id, **{"from": t.id}, paths=["todo.md"], dir="", cut=True)["data"] == [["todo.md", "todo.md"]]
        assert (other / "todo.md").read_text() == "hello\n" and not (project / "todo.md").exists()
        assert request(ctl, 5, cmd="fs.trash", id=t.id, path="docs/todo.md")["ok"]
        assert request(ctl, 6, cmd="fs.copypath", id=t.id, path="docs", relative=True)["data"] == "docs"
        assert request(ctl, 7, cmd="fs.copypath", id=t.id, path="docs", relative=False)["data"] == str(project / "docs")
    assert trashed == [(project / "docs").resolve() / "todo.md"]
    assert clip == ["docs", str(project / "docs")]


def test_thread_get(backend, project):
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


def test_terminal_here(backend, project, tmp_path, monkeypatch):
    """"Open a terminal here" in 2a: the shell starts in the folder, or in the folder of the file;
    never outside the project."""
    core, port = backend
    fake_shell = tmp_path / "fake_shell"
    fake_shell.write_text("#!/bin/bash\necho \"SHELL in $(pwd -P)\"\nsleep 30\n")
    fake_shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_shell))
    (project / "sub" / "deep").mkdir(parents=True)
    (project / "sub" / "a.py").write_text("x = 1\n")
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        for i, (path, folder) in enumerate([("sub/deep", project / "sub" / "deep"), ("sub/a.py", project / "sub"), ("", project)]):
            key = request(ctl, 10 + i, cmd="shell.new", id=t.id, rows=10, cols=80, path=path)["data"]["key"]
            with connect(f"ws://127.0.0.1:{port}/pty/{key}") as pty:
                assert f"SHELL in {folder.resolve()}\r".encode() in read_pty(pty, b"\r")
        assert actions.load(t.id).terminal_tabs == ["fake_shell"] * 3
        assert not request(ctl, 20, cmd="shell.new", id=t.id, rows=10, cols=80, path="../")["ok"]
        assert not request(ctl, 21, cmd="shell.new", id=t.id, rows=10, cols=80, path="nope")["ok"]
        assert len(actions.load(t.id).terminal_tabs) == 3


def test_run_python_file(backend, project, tmp_path, monkeypatch):
    """The play button of a Python file: a new shell at the project's root, which gets
    `python <file>` typed once its output is quiet (a shell that does not tell its prompts);
    with the project's .venv when it has one. Only a .py file of the project."""
    core, port = backend
    fake_shell = tmp_path / "fake_shell"
    fake_shell.write_text('#!/bin/bash\necho "SHELL in $(pwd -P)"\nread -r line\necho "GOT $line"\nsleep 30\n')
    fake_shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_shell))
    (project / "sub").mkdir()
    for name in ("a.py", "my script.py", "notes.txt"):
        (project / "sub" / name).write_text("x = 1\n")
    t = actions.new_task(project)

    def ran(req, path):
        key = request(ctl, req, cmd="shell.new", id=t.id, rows=10, cols=80, python=path)["data"]["key"]
        with connect(f"ws://127.0.0.1:{port}/pty/{key}") as pty:
            out = read_pty(pty, b"GOT ")
            out += read_pty(pty, b"\r", 2) if b"\r" not in out.split(b"GOT ", 1)[-1] else b""
        assert f"SHELL in {project.resolve()}\r".encode() in out
        return out.split(b"GOT ", 1)[1].split(b"\r", 1)[0].decode()

    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert ran(10, "sub/a.py") == "python sub/a.py"
        assert ran(11, "sub/my script.py") == "python 'sub/my script.py'"
        (project / ".venv" / "bin").mkdir(parents=True)
        (project / ".venv" / "bin" / "python").write_text("")
        assert ran(12, "sub/a.py") == ".venv/bin/python sub/a.py"
        assert actions.load(t.id).terminal_tabs == ["fake_shell"] * 3
        for req, path in [(20, "sub/notes.txt"), (21, "../x.py"), (22, "sub/nope.py"), (23, "sub")]:
            assert not request(ctl, req, cmd="shell.new", id=t.id, rows=10, cols=80, python=path)["ok"]
        assert len(actions.load(t.id).terminal_tabs) == 3


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
    # the photos of the empty 4a, one for each name in app.js
    import re
    from pathlib import Path

    js = urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js").read().decode()
    names = re.findall(r"'([a-z-]+)'", js.split("const CALM_PHOTOS = [", 1)[1].split("];", 1)[0])
    calm = Path(actions.__file__).parent / "web" / "calm"
    assert sorted(names) == sorted(f.stem for f in calm.glob("*.jpg"))  # every photo listed, none missing
    for name in names:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/calm/{name}.jpg")
        assert r.headers["Content-Type"] == "image/jpeg" and r.read(3) == b"\xff\xd8\xff"


def test_images_and_pdfs_are_served_for_the_viewers(backend, project, monkeypatch):
    import urllib.error
    import urllib.request

    core, port = backend
    t = actions.new_task(project)
    (project / "figs").mkdir()
    (project / "figs" / "my plot.png").write_bytes(b"\x89PNG fake")
    (project / "notes.md").write_text("secret\n")
    base = f"http://127.0.0.1:{port}/file/{t.id}"
    r = urllib.request.urlopen(f"{base}/figs/my%20plot.png?v=1")
    assert (r.status, r.headers["Content-Type"], r.read()) == (200, "image/png", b"\x89PNG fake")
    assert r.headers["Content-Security-Policy"] == "sandbox"
    for url, status in [(f"{base}/notes.md", 404), (f"{base}/missing.pdf", 404), (f"{base}/..%2F..%2Fx.png", 403),
                        (f"http://127.0.0.1:{port}/file/no-such-task/a.png", 404), (f"http://127.0.0.1:{port}/file/a.png", 404)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url)
        assert e.value.code == status, url
    opened = []
    monkeypatch.setattr("brain_fry.files.open_external", opened.append)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert request(ctl, 1, cmd="fs.read", id=t.id, path="figs/my plot.png")["data"]["kind"] == "image"
        assert request(ctl, 2, cmd="fs.open", id=t.id, path="notes.md")["ok"]
    assert opened == [(project / "notes.md").resolve()]


def test_web_pages_of_other_sites_are_refused(backend):
    """A page in my browser may open a websocket to 127.0.0.1 (Origin), or reach the server under
    its own name through DNS rebinding (Host). Brain Fry's own page and clients without Origin pass."""
    import urllib.error
    import urllib.request

    from websockets.exceptions import InvalidStatus

    core, port = backend
    for origin in ("https://evil.example", "http://localhost:8888", "null"):
        with pytest.raises(InvalidStatus) as e, connect(f"ws://127.0.0.1:{port}/ctl", origin=origin):
            pass
        assert e.value.response.status_code == 403
    with connect(f"ws://127.0.0.1:{port}/ctl", origin=f"http://127.0.0.1:{port}") as ctl:
        assert json.loads(ctl.recv(timeout=5))["type"] == "state"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Host": f"evil.example:{port}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert urllib.request.urlopen(f"http://localhost:{port}/").status == 200


def test_outside_tasks_follow_claude_codes_process_files(backend, project, home):
    import os

    core, port = backend
    t = actions.new_task(project)  # live, but no process of the app: it looks like it runs elsewhere
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True)
    (d / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "another", "entrypoint": "cli"}))
    core._refresh_outside()
    assert actions.load(t.id).live is False  # Claude Code runs no process for it
    (d / "2.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": t.id, "entrypoint": "claude-vscode", "status": "idle"}))
    core._refresh_outside()
    assert actions.load(t.id).live is True
    task = core.snapshot()["tasks"][t.id]
    assert task["outside"] and task["elsewhere"]["where"] == "VS Code"



def test_a_blocked_task_whose_dialog_closed_leaves_the_inbox(backend, project, home):
    import os

    core, port = backend
    t = actions.new_task(project)
    now = time.time()
    events.apply(project, {"session_id": t.id, "hook_event_name": "Notification", "bf_ts": now - 10,
                           "notification_type": "permission_prompt", "message": "Claude needs your permission"})
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True)

    def process(status, at):
        (d / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": t.id, "entrypoint": "cli",
                                              "status": status, "statusUpdatedAt": int(at * 1000)}))

    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        process("waiting", now - 16)  # the dialog opened 6 s before the Notification
        core._refresh_outside()
        assert actions.load(t.id).status == "blocked"
        process("busy", now - 20)  # a status from before this block
        core._refresh_outside()
        assert actions.load(t.id).inbox
        process("busy", now)  # I granted the permission
        core._refresh_outside()
        m = states(ctl, lambda m: m["type"] == "prompted")
        assert m == {"type": "prompted", "id": t.id, "slash": False, "why": "dialog closed"}
        got = actions.load(t.id)
        assert (got.status, got.inbox, got.unread) == ("working", False, False)

def test_front_end_errors_go_to_the_log(backend, caplog):
    import logging

    core, port = backend
    with caplog.at_level(logging.INFO), connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        request(ctl, 1, cmd="log", level="error", msg="TypeError: x is undefined @app.js:12")
    assert any(r.name == "brain_fry.ui" and "x is undefined" in r.getMessage() for r in caplog.records)


def test_the_picker_gets_a_fresh_project_list(backend, project, home):
    from brain_fry import projects

    core, port = backend
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        projects.init(home / "second")  # bf init in a terminal, while the app runs
        names = [p["name"] for p in request(ctl, 1, cmd="projects")["data"]]
        assert names == ["proj", "second"]
        states(ctl, lambda m: len(m["projects"]) == 2, timeout=8)  # and the state follows


def test_a_project_removed_from_the_lists_comes_back_with_bf_init(backend, project, home):
    from brain_fry import projects

    core, port = backend
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        request(ctl, 1, cmd="project.deactivate", path=str(project))
        st = states(ctl, lambda m: m["projects"][0].get("active") is False, timeout=5)
        assert st["projects"][0]["path"] == str(project)  # still sent: the dropdown shows it while it is current
        assert projects.project_paths() == [project]  # still registered: its events are applied
        assert not request(ctl, 2, cmd="project.deactivate", path=str(home / "nope"))["ok"]
        assert projects.init(project) is True  # bf init: back in the lists
        assert "active" not in projects.load()["projects"][0]
        assert projects.init(project) is False


def test_outdated_hook_is_detected(home):
    from brain_fry import install, paths

    install.install()
    assert Core._hook_state() == (True, False)
    paths.hook_file().write_text("#!/bin/bash\n# an older version\n")
    assert Core._hook_state() == (True, True)


def test_a_prompt_event_tells_the_front_end(backend, project):
    """Auto-advance: the front end learns that a task was answered, before the state push."""
    from brain_fry import paths

    core, port = backend
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        for i, prompt in enumerate(["go on", "/model sonnet"]):
            ev = {"session_id": t.id, "hook_event_name": "UserPromptSubmit", "cwd": str(project), "prompt": prompt, "bf_ts": 1_900_000_000 + i}
            (paths.spool_dir(project) / f"{i}.json").write_text(json.dumps(ev))
            end = time.time() + 5
            while True:
                m = json.loads(ctl.recv(timeout=end - time.time()))
                if m["type"] == "prompted":
                    break
            assert m == {"type": "prompted", "id": t.id, "slash": prompt.startswith("/")}


def test_a_background_result_is_not_my_answer(backend, project):
    """The result of a background agent starts a turn with a UserPromptSubmit too. It must not
    advance: I may be reading that task. Then my own prompt still tells the front end."""
    from brain_fry import paths

    core, port = backend
    t = actions.new_task(project)
    prompts = [{"prompt": '<agent-message from="a1">\n[Subagent hand-back] ...'},
               {"prompt": "<task-notification>\n<task-id>b1</task-id>"},
               {"prompt": "looks like mine", "source": "system"},
               {"prompt": "go on"}]  # fmt: skip
    seen = []
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        for i, fields in enumerate(prompts):
            ev = {"session_id": t.id, "hook_event_name": "UserPromptSubmit", "cwd": str(project), "bf_ts": 1_900_000_000 + i, **fields}
            (paths.spool_dir(project) / f"{i}.json").write_text(json.dumps(ev))
        end = time.time() + 5
        while time.time() < end and len(actions.load(t.id).threads) < len(prompts):  # all four applied
            try:
                m = json.loads(ctl.recv(timeout=0.2))
            except TimeoutError:
                continue
            seen += [m] if m["type"] == "prompted" else []
        drain = time.time() + 1  # the message of the last pass goes out after its file is saved
        while time.time() < drain:
            try:
                m = json.loads(ctl.recv(timeout=0.2))
            except TimeoutError:
                continue
            seen += [m] if m["type"] == "prompted" else []
    assert [th.prompt for th in actions.load(t.id).threads][-1] == "go on"
    assert seen == [{"type": "prompted", "id": t.id, "slash": False}]  # one: my prompt, not the three others


def test_selection_for_the_hook(backend, project, home):
    from brain_fry import selection

    core, port = backend
    (project / "src").mkdir()
    (project / "src" / "x.py").write_text("a = 1\nb = 2\n")
    task = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        json.loads(ctl.recv(timeout=5))
        ctl.send(json.dumps({"cmd": "selection.set", "req": 1, "id": task.id, "path": "src/x.py", "from": 1, "to": 2,
                             "text": "a = 1\nb = 2", "total": 11, "dirty": False}))  # fmt: skip
        states(ctl, lambda m: m.get("req") == 1)
        sel = json.loads(selection.selection_file().read_text())
        assert (sel["session_id"], sel["path"], sel["from"], sel["to"], sel["cut"]) == (task.id, "src/x.py", 1, 2, False)
        ctl.send(json.dumps({"cmd": "selection.set", "req": 2, "id": None}))
        states(ctl, lambda m: m.get("req") == 2)
        assert not selection.selection_file().exists()
        ctl.send(json.dumps({"cmd": "selection.set", "req": 3, "id": task.id, "path": "../out.py", "from": 1, "to": 1,
                             "text": "x", "total": 1}))  # fmt: skip
        assert not states(ctl, lambda m: m.get("req") == 3)["ok"]  # outside the project: refused


def test_selection_path_is_relative_to_the_sessions_cwd(project, home):
    from brain_fry import selection

    (project / "sub").mkdir()
    (project / "top.py").write_text("x\n")
    task = actions.new_task(project)
    task.cwd = str(project / "sub")  # a session that started in a subfolder
    sel = selection.write(task, "top.py", 1, 1, "x" * 12_000, 12_000, True)
    assert sel["path"] == str((project / "top.py").resolve())  # outside its cwd: absolute
    assert len(sel["text"]) == selection.MAX_CHARS and sel["cut"] and sel["dirty"]
    task.cwd = str(project)
    assert selection.write(task, "sub/../top.py", 1, 1, "x", 1, False)["path"] == "top.py"


def test_selection_is_cleared_when_the_front_end_leaves(backend, project, home):
    from brain_fry import selection

    core, port = backend
    (project / "x.py").write_text("x\n")
    task = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        json.loads(ctl.recv(timeout=5))
        ctl.send(json.dumps({"cmd": "selection.set", "req": 1, "id": task.id, "path": "x.py", "from": 1, "to": 1, "text": "x", "total": 1}))
        states(ctl, lambda m: m.get("req") == 1)
        assert selection.selection_file().exists()
    end = time.time() + 5
    while selection.selection_file().exists() and time.time() < end:
        time.sleep(0.05)
    assert not selection.selection_file().exists()


def test_pin_command_keeps_the_task_in_the_inbox(backend, project):
    core, port = backend
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "pin", "id": t.id}))
        st = states(ctl, lambda m: t.id in m["inbox"])
        assert st["tasks"][t.id]["pinned"] and not st["tasks"][t.id]["inbox"]
        ctl.send(json.dumps({"cmd": "pin", "id": t.id}))  # unpin
        st = states(ctl, lambda m: t.id not in m["inbox"])
        assert st["tasks"][t.id]["pinned"] is False


def test_digest_queue_skips_a_task_already_in_it(project, monkeypatch):
    from brain_fry import digest

    a, b = actions.new_task(project), actions.new_task(project)
    for t in (a, b):
        actions._update(t.id, threads=[Thread(n=1, ts="x", kind="pending", prompt="p", reply="r")])
    done = []
    monkeypatch.setattr(digest, "digest_task", lambda task: done.append(task.id))
    core = Core()
    core._enqueue_digest([a.id, b.id])
    core._enqueue_digest([a.id])  # already queued: no second entry
    core.digest_q.put((9, 0, None))  # stops the worker after the rest
    core._digest_worker()
    assert done == [a.id, b.id] and core.queued == set()


def test_commands_of_review_mode(backend, project, monkeypatch):
    """git.state, git.diff, staging a hunk and a commit, for the repository of a task's folder."""
    core, port = backend
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(project.parent / "gitconfig"))
    for k in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{k}_NAME", "T")
        monkeypatch.setenv(f"{k}_EMAIL", "t@t")
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert request(ctl, 1, cmd="git.state", id=t.id)["data"] == {"repo": False}
        import subprocess

        subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
        (project / ".gitignore").write_text(".brain_fry/\n")  # as ~/.gitignore_global does
        (project / "a.txt").write_text("one\n")
        st = request(ctl, 2, cmd="git.state", id=t.id)["data"]
        assert st["repo"] and [f["path"] for f in st["unstaged"]] == [".gitignore", "a.txt"]
        assert [x["id"] for x in st["trees"][0]["tasks"]] == [t.id]
        assert request(ctl, 3, cmd="git.stage", id=t.id, paths=[".gitignore", "a.txt"])["ok"]
        d = request(ctl, 4, cmd="git.diff", id=t.id, side="staged", path="a.txt")["data"]
        assert d["hunks"][0]["lines"] == [["+", "", 1, "one"]] and not d["hunkable"]  # a new file: whole file only
        assert request(ctl, 5, cmd="git.commit", id=t.id, message="first")["ok"]
        rows = request(ctl, 6, cmd="git.log", id=t.id)["data"]["rows"]
        assert [r["s"] for r in rows] == ["Uncommitted changes", "first"]
        bad = request(ctl, 7, cmd="git.diff", id=t.id, side="unstaged", path="../x")
        assert not bad["ok"] and "ValueError" in bad["error"]
