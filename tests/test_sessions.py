"""Claude Code's process files and transcripts, in a fake ~/.claude."""

import json
import os
import subprocess

from brain_fry import sessions, taskfile, views

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


def test_read_turns(home, project):
    """One thread per prompt that I typed; the reply is the text after the last tool call."""
    sub = project / "src"
    title, threads, ids = sessions.read_turns(write(home, str(sub), transcript(str(sub))))
    assert title == "42 - auth bug"
    assert [(th.n, th.prompt, th.reply, th.kind) for th in threads] == [
        (1, "fix the bug\nplease", "Fixed it. Review auth.py?", "imported"),
        (2, "/model sonnet", "Set model.", "imported"),
        (3, "and now the tests", "", "imported"),
    ]
    assert threads[0].ts.startswith("2026-11-09T") and ids == [None, None, None]


def test_imported_threads_survive_a_round_trip(home, project):
    threads = sessions.read_turns(write(home, str(project), transcript(str(project)))).threads
    task = taskfile.Task(session_ids=[SID], project=str(project), cwd=str(project), threads=threads)
    assert taskfile.loads(taskfile.dumps(task)) == task
