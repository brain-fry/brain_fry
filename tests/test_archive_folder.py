"""Archived tasks live in <project>/.brain_fry/archive/, out of the periodic reads of the app; the state
that the backend pushes holds the open tasks, the archived ones that this run touched, and all of them
while the front end asks (the board with H, or a search). DESIGN_DETAILS.md, "Task file" and "Status,
inbox, board"."""

import json
import os
import time

import pytest
from websockets.sync.client import connect

from brain_fry import actions, events, paths, projects, taskfile, views
from brain_fry.app.core import IDLE_CLOSE, Core
from brain_fry.app.server import start_backend
from brain_fry.taskfile import Task, Thread
from brain_fry.util import iso

_ts = iter(range(1_800_000_000, 1_900_000_000))


def put(project, name, sid="s1", **fields):
    """Drop an event file into the spool, as hook.sh would (as in test_events.py)."""
    ev = {"session_id": sid, "hook_event_name": name, "cwd": str(project), "transcript_path": "", "bf_ts": next(_ts), **fields}
    (paths.spool_dir(project) / f"{ev['bf_ts']}-{sid}.json").write_text(json.dumps(ev))


def where(project, tid) -> str:
    """'top', 'archive', 'both' or 'none': where the task's file is."""
    top, arch = paths.task_path(project, tid).exists(), paths.archive_path(project, tid).exists()
    return {(True, False): "top", (False, True): "archive", (True, True): "both", (False, False): "none"}[(top, arch)]


def make(project, tid, **kw) -> Task:
    t = Task(session_ids=[tid], project=str(project), cwd=str(project), **kw)
    taskfile.save(t)
    return t


# ---------------------------------------------------------------- the file follows the status


def test_archive_and_unarchive_move_the_file(project):
    t = actions.new_task(project)
    assert where(project, t.id) == "top"
    actions.archive(t.id)
    assert where(project, t.id) == "archive"
    assert actions.load(t.id).status == "done"  # the lookup finds it there
    assert actions.locate(t.id) == paths.archive_path(project, t.id)
    actions.mark_opened(t.id)  # a change of an archived task stays in the archive
    assert where(project, t.id) == "archive"
    actions.archive(t.id)  # unarchive
    assert where(project, t.id) == "top" and actions.load(t.id).status == "waiting"


def test_the_periodic_reads_take_the_open_tasks_only(project):
    make(project, "open1")
    make(project, "done1", status="done")
    assert [t.id for t in taskfile.all_tasks(project)] == ["open1"]
    assert [t.id for t in taskfile.archived_tasks(project)] == ["done1"]
    assert [t.id for t in actions.all_tasks()] == ["open1"]
    assert [t.id for t in actions.archived_tasks()] == ["done1"]


def test_close_archives_into_the_folder_and_a_prompt_brings_it_back(project):
    put(project, "UserPromptSubmit", prompt="close")
    put(project, "Stop", last_assistant_message="Notes written.\nclosed :-)")
    events.consume(project)
    assert where(project, "s1") == "archive"
    assert taskfile.load(paths.archive_path(project, "s1")).threads[-1].kind == "pending"  # the digest still runs

    put(project, "SessionEnd", reason="exit")  # an event on an archived task: it stays there
    events.consume(project)
    assert where(project, "s1") == "archive"
    assert taskfile.load(paths.archive_path(project, "s1")).live is False

    put(project, "UserPromptSubmit", prompt="one more thing")  # the same task comes back, never a second one
    assert events.consume(project) == ["s1"]
    assert where(project, "s1") == "top"
    t = taskfile.load(paths.task_path(project, "s1"))
    assert (t.status, [th.prompt for th in t.threads]) == ("working", ["close", "one more thing"])


def test_a_stop_in_a_task_archived_by_my_key_brings_it_back(project):
    put(project, "UserPromptSubmit", prompt="a")
    events.consume(project)
    actions.archive("s1")
    assert where(project, "s1") == "archive"
    put(project, "Stop", last_assistant_message="done")
    events.consume(project)
    assert where(project, "s1") == "top" and actions.load("s1").status == "waiting"


def test_a_notification_on_an_archived_task_keeps_it_archived(project):
    put(project, "UserPromptSubmit", prompt="a")
    events.consume(project)
    actions.archive("s1")
    put(project, "Notification", notification_type="idle_prompt", message="waiting")
    events.consume(project)
    assert where(project, "s1") == "archive"


# ---------------------------------------------------------------- migration


def test_migration_moves_the_archived_files_of_the_top_folder(project):
    open_t = make(project, "open1")
    for tid in ("done1", "done2"):  # written before the archive folder existed
        t = Task(session_ids=[tid], project=str(project), cwd=str(project), status="done")
        paths.task_path(project, tid).write_text(taskfile.dumps(t))
        os.utime(paths.task_path(project, tid), (1_700_000_000, 1_700_000_000))
    assert actions.migrate_archive() == 2
    assert (where(project, "done1"), where(project, "done2"), where(project, open_t.id)) == ("archive", "archive", "top")
    assert paths.archive_path(project, "done1").stat().st_mtime == 1_700_000_000  # a rename keeps the time
    assert actions.migrate_archive() == 0


def test_two_copies_after_a_crash_the_top_one_wins(project):
    make(project, "x1", status="done", title="old")  # in the archive
    newer = Task(session_ids=["x1"], project=str(project), cwd=str(project), status="waiting", title="new")
    paths.task_path(project, "x1").write_text(taskfile.dumps(newer))  # an unarchive whose removal did not happen
    assert actions.load("x1").title == "new"
    actions.archive("x1")  # the next save cleans up: one copy, in the archive
    assert where(project, "x1") == "archive" and actions.load("x1").title == "new"


# ---------------------------------------------------------------- project order


def test_a_project_whose_tasks_are_all_archived_keeps_its_place(project, home):
    other = home / "other"
    other.mkdir()
    projects.init(other)
    make(project, "mine", opened=iso(time.time() - 3600))  # opened an hour ago, open
    make(other, "theirs", status="done", opened=iso(time.time() - 60))  # opened a minute ago, then archived
    order = [p["path"] for p in views.project_order(projects.load()["projects"], actions.all_tasks(), actions.archive_times())]
    assert order == [str(other), str(project)]
    # without the archive's times, it would drop to the bottom (the bug that this guards against)
    assert [p["path"] for p in views.project_order(projects.load()["projects"], actions.all_tasks())] == [str(project), str(other)]


def test_the_archive_time_is_the_last_opened_not_the_file_time(project, home):
    other = home / "other"
    other.mkdir()
    projects.init(other)
    make(project, "mine", opened=iso(time.time() - 600))
    t = make(other, "theirs", status="done", live=True, opened=iso(time.time() - 3600))
    first = actions.archive_times()[str(other)]
    actions.set_live(t.id, False)  # the session ends (the quit of the app): the file is written now
    assert actions.archive_times()[str(other)][:2] == first[:2]
    order = [p["path"] for p in views.project_order(projects.load()["projects"], actions.all_tasks(), actions.archive_times())]
    assert order == [str(project), str(other)]  # still by `opened`
    actions.mark_opened(t.id)  # I select the archived task: its project comes first
    order = [p["path"] for p in views.project_order(projects.load()["projects"], actions.all_tasks(), actions.archive_times())]
    assert order[0] == str(other)


# ---------------------------------------------------------------- the state that the backend pushes


def test_the_lean_state_holds_the_open_and_the_touched_tasks(project):
    make(project, "open1")
    make(project, "done1", status="done")
    make(project, "done2", status="done")
    core = Core()
    assert set(core.snapshot()["tasks"]) == {"open1"}
    core._touch("done1", "project:/x", None)
    assert set(core.snapshot()["tasks"]) == {"open1", "done1"}
    core.show_archive = True
    assert set(core.snapshot()["tasks"]) == {"open1", "done1", "done2"}
    core.show_archive = False
    core.selected = "done2"
    assert set(core.snapshot()["tasks"]) == {"open1", "done1", "done2"}
    core._touch("gone")  # a task that no longer exists is forgotten
    core.snapshot()
    assert "gone" not in core.touched


FAKE_CLAUDE = "#!/bin/bash\nsleep 30\n"


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


def test_archive_show_and_the_touched_tasks_over_the_websocket(backend, project):
    core, port = backend
    make(project, "open1")
    make(project, "done1", status="done")
    make(project, "done2", status="done")
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        assert set(json.loads(ctl.recv(timeout=5))["tasks"]) == {"open1"}
        ctl.send(json.dumps({"cmd": "archive.show", "on": True, "ids": []}))
        states(ctl, lambda m: set(m["tasks"]) == {"open1", "done1", "done2"})
        ctl.send(json.dumps({"cmd": "archive.show", "on": False, "ids": ["done2"]}))  # an archived card opened in Dev mode
        states(ctl, lambda m: set(m["tasks"]) == {"open1", "done2"})
        ctl.send(json.dumps({"cmd": "archive", "id": "open1"}))  # archived in Dev mode: it stays in the state
        st = states(ctl, lambda m: m["tasks"].get("open1", {}).get("status") == "done")
        assert set(st["tasks"]) == {"open1", "done2"}
        assert where(project, "open1") == "archive"


def test_the_migration_runs_at_the_start_of_the_backend(project, tmp_path, monkeypatch):
    t = Task(session_ids=["done1"], project=str(project), cwd=str(project), status="done")
    paths.task_path(project, "done1").write_text(taskfile.dumps(t))
    monkeypatch.setenv("BRAIN_FRY_CLAUDE", "/usr/bin/true")
    core = Core()
    start_backend(core)
    try:
        assert where(project, "done1") == "archive"
    finally:
        core.shutdown()


# ---------------------------------------------------------------- the sweep


class FakePty:
    kind, alive, last_output = "claude", True, 0.0

    def __init__(self, key):
        self.key, self.hung = key, False

    def hangup(self):
        self.hung = True


def test_the_idle_close_reaches_an_archived_task_whose_session_runs(project):
    old = iso(time.time() - IDLE_CLOSE - 60)
    make(project, "done1", status="done", live=True, updated=old)
    make(project, "open1", live=True, updated=old)
    make(project, "busy1", status="done", live=True, updated=iso())
    core = Core()
    core.ptys = {k: FakePty(k) for k in ("done1", "open1", "busy1")}
    core._sweep()
    assert {k: s.hung for k, s in core.ptys.items()} == {"done1": True, "open1": True, "busy1": False}


def test_the_sweep_retries_the_digest_of_an_archived_task(project):
    pending = [Thread(n=1, ts="x", kind="pending", prompt="close", reply="closed :-)")]
    make(project, "done1", status="done", threads=pending)
    make(project, "open1", threads=list(pending))
    core = Core()
    assert core._sweep(archive=True) == ["open1", "done1"]  # the first sweep of a run reads the archive
    assert core._sweep() == ["open1"]
    core.failed["done1"] = 0.0  # its digest failed: retried although archived
    assert core._sweep() == ["open1", "done1"]


# ---------------------------------------------------------------- a moved project


def test_a_moved_project_keeps_its_tasks(project, home):
    """The folder that holds a task file is its project: the files of a renamed folder name the old one."""
    (project / "sub").mkdir()
    make(project, "open1")
    make(project, "done1", status="done", live=True)
    taskfile.save(Task(session_ids=["sub1"], project=str(project), cwd=str(project / "sub")))
    taskfile.save(Task(session_ids=["out1"], project=str(project), cwd=str(home / "elsewhere")))
    new = project.with_name("moved")
    project.rename(new)
    projects.init(new)

    got = {t.id: t for t in taskfile.all_tasks(new) + taskfile.archived_tasks(new)}
    assert {t.project for t in got.values()} == {str(new)}
    assert got["open1"].cwd == str(new) and got["sub1"].cwd == str(new / "sub")
    assert got["out1"].cwd == str(home / "elsewhere")  # a session that started outside the project: not moved

    put(new, "SessionEnd", "done1")  # the event of 2026-11-14: its save went to the old folder and failed
    put(new, "UserPromptSubmit", "open1", prompt="go on")
    assert events.consume(new) == ["done1", "open1"]
    assert where(new, "done1") == "archive" and where(new, "open1") == "top"
    assert actions.load("done1").live is False
    assert not project.exists()
    assert f"project: {new}\n" in paths.task_path(new, "open1").read_text()  # the file follows at its save


def test_a_task_file_outside_a_project_keeps_what_it_says(tmp_path):
    assert paths.project_of(tmp_path / "p" / ".brain_fry" / "archive" / "s1.md") == tmp_path / "p"
    assert paths.project_of(tmp_path / "s1.md") is None
    (tmp_path / "s1.md").write_text(taskfile.dumps(Task(session_ids=["s1"], project="/gone", cwd="/gone/sub")))
    assert taskfile.load(tmp_path / "s1.md").project == "/gone"
