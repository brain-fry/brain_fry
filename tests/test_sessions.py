"""Other sessions: Claude Code's transcripts and process files, in a fake ~/.claude."""

import json
import os
import subprocess

import pytest

from brain_fry import actions, paths, projects, sessions, taskfile, views

SID = "11111111-2222-3333-4444-555555555555"


def user(content, ts="2026-11-09T08:00:00.000Z", origin="human", **kw):
    e = {"type": "user", "timestamp": ts, "cwd": kw.pop("cwd", None), "entrypoint": kw.pop("entrypoint", "claude-vscode"),
         "message": {"role": "user", "content": content}, **kw}
    if origin:
        e["origin"] = {"kind": origin}
    return e


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(t):
    return {"type": "text", "text": t}


def tool(name="Bash"):
    return {"type": "tool_use", "name": name, "input": {"command": "ls"}}


def transcript(cwd):
    """The cases seen in real transcripts (2026-08 to 11)."""
    return [
        {"type": "file-history-snapshot", "snapshot": {}},  # no cwd
        user([text("<ide_opened_file>The user opened auth.py</ide_opened_file>"), text("fix the bug\nplease")], cwd=cwd),
        assistant(text("Looking."), tool()),
        user([{"type": "tool_result", "content": "a.py"}], origin=None, cwd=cwd),
        assistant(text("Fixed it. Review auth.py?")),
        {"type": "custom-title", "customTitle": "42 - auth bug"},
        user("# Some skill body", origin=None, isMeta=True, cwd=cwd),
        user("<command-name>/model</command-name><command-args>sonnet</command-args>", origin=None, cwd=cwd),
        assistant(text("Set model.")),
        user("[Request interrupted by user]", origin=None, cwd=cwd),
        user("<task-notification>done</task-notification>", origin="task-notification", cwd=cwd),
        user("and now the tests", ts="2026-11-09T09:00:00.000Z", cwd=cwd),
        assistant(text("Running them."), tool()),  # cut off: no reply after the last tool call
    ]


def write(home, cwd, entries, sid=SID, slug="-slug"):
    d = home / ".claude" / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return f


@pytest.fixture(autouse=True)
def fresh_cache():
    sessions._summaries.clear()
    sessions._cwds.clear()


def test_scan_lists_the_sessions_of_my_projects_that_are_not_tasks(home, project):
    sub = project / "src"
    write(home, str(sub), transcript(str(sub)))
    write(home, "/elsewhere", transcript("/elsewhere"), sid="22222222-0000-0000-0000-000000000000", slug="-other")
    write(home, str(project), [user("x", origin="peer", cwd=str(project))], sid="33333333-0000-0000-0000-000000000000")
    write(home, str(project), [user("scripted", cwd=str(project), entrypoint="sdk-cli")], sid="44444444-0000-0000-0000-000000000000")
    [o] = sessions.scan(set(), None)
    assert (o["id"], o["title"], o["project"], o["cwd"], o["prompts"]) == (SID, "42 - auth bug", "proj", str(sub), 3)
    assert (o["firstPrompt"], o["lastPrompt"], o["running"]) == ("fix the bug", "and now the tests", None)
    assert sessions.scan({SID}, None) == []


def test_scan_reads_only_complete_appended_lines(home, project):
    f = write(home, str(project), transcript(str(project)))
    assert sessions.scan(set(), None)[0]["prompts"] == 3
    line = json.dumps(user("one more", cwd=str(project)))
    with f.open("a") as out:
        out.write(line[:20])  # Claude Code is writing this line
    assert sessions.scan(set(), None)[0]["prompts"] == 3
    with f.open("a") as out:
        out.write(line[20:] + "\n")
    [o] = sessions.scan(set(), None)
    assert (o["prompts"], o["lastPrompt"]) == (4, "one more")


def test_projects_ordered_by_last_opened_task():
    ps = [{"name": n, "path": f"/p/{n}"} for n in "abcde"]
    task = lambda p, opened="", updated="": taskfile.Task(session_ids=["x"], project=p, cwd=p, opened=opened, updated=updated)  # noqa: E731
    tasks = [
        task("/p/b", opened="2026-11-10T09:00:00+01:00", updated="2026-11-10T12:00:00+01:00"),
        task("/p/b", updated="2026-11-10T13:00:00+01:00"),  # never opened: does not move b
        task("/p/d", opened="2026-11-10T10:00:00+01:00"),
        task("/p/c", updated="2026-11-10T14:00:00+01:00"),  # never opened: after every opened project
        task("/p/e", updated="2026-11-09T14:00:00+01:00"),
    ]
    ranked = views.project_order(ps, tasks)
    assert [p["name"] for p in ranked] == ["d", "b", "c", "e", "a"]  # a: no task, projects.json order
    assert ranked[0]["last"] == "2026-11-10T10:00:00+01:00" and ranked[-1]["last"] is None
    assert [p["name"] for p in views.project_order(ps, [])] == list("abcde")


def test_running_sessions(home, tmp_path):
    assert sessions.running() is None  # no folder: nothing can be said
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True)
    assert sessions.running() == {}
    dead = subprocess.Popen(["true"])
    dead.wait()
    (d / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": SID, "entrypoint": "claude-vscode",
                                                       "status": "idle", "name": "42 - auth bug", "nameSource": "hook"}))
    (d / f"{dead.pid}.json").write_text(json.dumps({"pid": dead.pid, "sessionId": "gone", "entrypoint": "cli"}))
    assert sessions.running() == {SID: {"pid": os.getpid(), "where": "VS Code", "status": "idle", "since": None,
                                        "name": "42 - auth bug"}}
    (d / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": SID, "entrypoint": "cli",
                                                       "status": "waiting", "waitingFor": "permission",
                                                       "statusUpdatedAt": 1794310088122}))
    assert sessions.running()[SID]["status"] == "waiting" and sessions.running()[SID]["since"] == 1794310088.122
    for f in d.iterdir():
        f.write_text(json.dumps({"something": "else"}))  # a format that we do not know
    assert sessions.running() is None


def test_adopt_imports_the_turns(home, project):
    sub = project / "src"
    write(home, str(sub), transcript(str(sub)))
    task = sessions.adopt(SID, "planning")
    assert (task.id, task.project, task.cwd, task.prio, task.title) == (SID, str(project), str(sub), "planning", "42 - auth bug")
    assert (task.status, task.live, task.inbox) == ("waiting", False, False)
    t = actions.load(SID)
    assert [(th.prompt, th.reply, th.kind) for th in t.threads] == [
        ("fix the bug\nplease", "Fixed it. Review auth.py?", "imported"),
        ("/model sonnet", "Set model.", "imported"),
        ("and now the tests", "", "interrupted"),
    ]
    assert t.created.startswith("2026-11-09T") and t.transcript_path.endswith(f"{SID}.jsonl")
    with pytest.raises(ValueError, match="already a task"):
        sessions.adopt(SID)


def test_adopt_a_session_that_runs_elsewhere(home, project):
    entries = transcript(str(project))[:6]  # ends with a reply: the last turn is digested
    write(home, str(project), entries)
    task = sessions.adopt(SID, "working", {"pid": 1, "where": "VS Code", "status": "busy", "name": None})
    assert (task.live, task.status) == (True, "working")
    assert [th.kind for th in task.threads] == ["pending"]


def test_adopt_refuses_sessions_outside_my_projects(home, project):
    write(home, "/elsewhere", transcript("/elsewhere"))
    with pytest.raises(ValueError, match="not in a Brain Fry project"):
        sessions.adopt(SID)
    with pytest.raises(ValueError, match="not a session id"):
        sessions.adopt("../../etc/passwd")


def test_nearest_project_wins(home, project):
    inner = project / "inner"
    projects.init(inner)
    assert sessions.project_for(str(inner / "x"), projects.project_paths()) == inner
    assert sessions.project_for(str(project / "x"), projects.project_paths()) == project
    assert sessions.project_for("/elsewhere", projects.project_paths()) is None


def test_imported_threads_survive_a_round_trip(home, project):
    write(home, str(project), transcript(str(project)))
    sessions.adopt(SID)
    task = taskfile.load(paths.task_path(project, SID))
    assert taskfile.loads(taskfile.dumps(task)) == task


def test_first_line_skips_wrapper_tags():
    assert sessions._first_line('<pasted_content id="c7bd">\nTraceback (most recent call last):\n</pasted_content>') \
        == "Traceback (most recent call last):"
    assert sessions._first_line("<b>bold</b> text") == "<b>bold</b> text"
