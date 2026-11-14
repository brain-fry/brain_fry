"""A planned card starts with its notes: its session puts my title and notes in Claude Code's
input box (`--prefill`, not sent). DESIGN_DETAILS.md, "Modes"."""

import json
import logging
import sys
import time

import pytest
from websockets.sync.client import connect

from brain_fry import actions
from brain_fry.app.core import Core
from brain_fry.app.ptys import loggable
from brain_fry.app.server import start_backend
from brain_fry.taskfile import Task, Thread

NOTES = 'First line, about the café.\n/review this first\nask @alice about core.py\nit\'s "quoted"\n-v starts with a dash'

# prints its arguments as one JSON line (non-ASCII escaped), then waits like a session.
# A claude that does not know --prefill: the error of Commander, exit 1.
FAKE = f"""#!{sys.executable}
import json, sys, time
args = sys.argv[1:]
if "--old" in open(__file__ + ".mode").read() and any(a.startswith("--prefill=") for a in args):
    print("error: unknown option '" + next(a for a in args if a.startswith("--prefill=")) + "'", flush=True)
    sys.exit(1)
print("ARGS " + json.dumps(args), flush=True)
time.sleep(30)
"""


@pytest.fixture
def backend(project, tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    (tmp_path / "fake_claude.mode").write_text("")
    monkeypatch.setenv("BRAIN_FRY_CLAUDE", str(fake))
    core = Core()
    port = start_backend(core)
    yield core, port, tmp_path / "fake_claude.mode"
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


def args_of(port, tid, timeout=5.0) -> list[str]:
    """The arguments that the fake claude of the task's PTY got."""
    out, end = b"", time.time() + timeout
    with connect(f"ws://127.0.0.1:{port}/pty/{tid}") as pty:
        while b"\n" not in out.partition(b"ARGS ")[2] and time.time() < end:
            out += pty.recv(timeout=end - time.time())
    return json.loads(out.partition(b"ARGS ")[2].split(b"\r\n")[0])


def task(**kw) -> Task:
    return Task(session_ids=["s1"], project="/p", cwd="/p", **kw)


def test_which_task_gets_text_and_which_text():
    planned = task(title="Plan the meeting", short_title="Plan the meeting", notes=NOTES)
    assert Core._prefill(planned) == "Plan the meeting\n\n" + NOTES
    assert Core._prefill(task(title=" Title only ")) == "Title only"
    assert Core._prefill(task(notes="notes only")) == "notes only"  # a hand edit: no title
    assert Core._prefill(task()) == ""  # born in Dev mode: no title, no notes before its first turn
    assert Core._prefill(task(title="x", notes="y", threads=[Thread(1, "t", prompt="go")])) == ""  # prompted
    assert Core._prefill(task(title="x", parent="p0")) == ""  # a fork: its parent's conversation
    assert Core._prefill(task(title="a\x00b")) == "ab"  # argv cannot hold a NUL


def test_argv_of_a_planned_card(monkeypatch):
    monkeypatch.setenv("BRAIN_FRY_CLAUDE", "claude")
    assert Core._resume_argv(task(title="T", notes="-n")) == ["claude", "--session-id", "s1", "--prefill=T\n\n-n"]
    assert Core._resume_argv(task()) == ["claude", "--session-id", "s1"]
    assert Core._resume_argv(task(title="T", threads=[Thread(1, "t")])) == ["claude", "--resume", "s1"]
    assert Core._resume_argv(task(title="T", parent="p0")) == ["claude", "--resume", "p0", "--fork-session",
                                                               "--session-id", "s1"]  # fmt: skip


def test_the_log_gets_the_length_only():
    line = loggable(["claude", "--session-id", "s1", "--prefill=" + "Plan\n\n" + NOTES])
    assert line == f"claude --session-id s1 '--prefill=<{len(NOTES) + 6} chars>'"
    assert loggable(["claude", "--resume", "s1"]) == "claude --resume s1"


def test_each_start_of_a_planned_card_fills_the_box_until_its_first_turn(backend, project, caplog):
    core, port, _ = backend
    tid = actions.plan_task(project, "Planifier la réunion", "working", NOTES).id
    caplog.set_level(logging.INFO)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "select", "id": tid, "rows": 24, "cols": 80}))
        states(ctl, lambda m: m.get("tasks", {}).get(tid, {}).get("pty"))
        assert args_of(port, tid) == ["--session-id", tid, "--prefill=Planifier la réunion\n\n" + NOTES]
        assert "café" not in caplog.text  # free text: never logged
        started = [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"pty {tid}: pid")]
        assert len(started) == 1 and "réunion" not in started[0]  # the title is logged elsewhere, not here
        assert f"<{len('Planifier la réunion') + 2 + len(NOTES)} chars>" in started[0]

        # stateless: the session ends without a prompt, the next start fills the box again
        core.ptys[tid].hangup()
        states(ctl, lambda m: not m["tasks"][tid]["pty"] and not m["tasks"][tid]["live"])
        ctl.send(json.dumps({"cmd": "resume", "id": tid, "rows": 24, "cols": 80}))
        states(ctl, lambda m: m["tasks"][tid]["pty"])
        assert args_of(port, tid)[-1].startswith("--prefill=Planifier")

        # once prompted, it resumes its conversation: no text
        core.ptys[tid].hangup()
        states(ctl, lambda m: not m["tasks"][tid]["pty"])
        actions._update(tid, threads=[Thread(1, "2026-11-14T10:00:00", prompt="go")])
        ctl.send(json.dumps({"cmd": "select", "id": tid, "rows": 24, "cols": 80}))
        states(ctl, lambda m: m["tasks"][tid]["pty"])
        assert args_of(port, tid) == ["--resume", tid]


def test_a_task_of_dev_mode_starts_with_an_empty_box(backend, project):
    core, port, _ = backend
    t = actions.new_task(project)
    actions.set_live(t.id, False)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "select", "id": t.id, "rows": 24, "cols": 80}))
        states(ctl, lambda m: m["tasks"][t.id]["pty"])
        assert args_of(port, t.id) == ["--session-id", t.id]


def test_a_claude_without_prefill_starts_again_without_it(backend, project):
    core, port, mode = backend
    mode.write_text("--old")
    tid = actions.plan_task(project, "Title", "working", "notes").id
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"cmd": "select", "id": tid, "rows": 24, "cols": 80}))
        st = states(ctl, lambda m: (m.get("tasks", {}).get(tid, {}).get("pty") or 0) >= 2)
        assert args_of(port, tid) == ["--session-id", tid]
        assert core.ptys[tid].alive and st["tasks"][tid]["pty"] == 2
        assert core.ptys[tid].argv[-1] == tid  # and no third start
