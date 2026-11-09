import json

import pytest

from brain_fry import digest, paths, taskfile
from brain_fry.taskfile import Task, Thread


def make(project, threads, **kw) -> Task:
    t = Task(session_ids=["s1"], project=str(project), cwd=str(project), threads=threads, **kw)
    taskfile.save(t)
    return t


def fake(result):
    calls = []

    def call(prompt):
        calls.append(prompt)
        return result

    call.calls = calls
    return call


def test_digest_fills_pending_threads_and_keeps_the_first_title(project):
    t = make(
        project,
        [Thread(n=1, ts="x", kind="review", digest="old", prompt="a", reply="r1"),
         Thread(n=2, ts="y", kind="pending", prompt="b", reply="Should I use A or B?")],
        title="Kept title",
    )  # fmt: skip
    call = fake({"title": "New title", "digest": "Goal: x.", "threads": [
        {"n": 2, "kind": "rule", "digest": "Asks A or B.", "question": "Should I use A or B?"}]})  # fmt: skip
    digest.digest_task(t, call)
    got = taskfile.load(paths.task_path(project, "s1"))
    assert got.title == "Kept title" and got.digest == "Goal: x."
    assert (got.threads[1].kind, got.threads[1].digest, got.threads[1].question) == ("rule", "Asks A or B.", "Should I use A or B?")
    assert got.threads[0].digest == "old"  # not sent, not touched
    assert "## Turn 2" in call.calls[0] and "## Turn 1" not in call.calls[0]


def test_reloads_the_task_after_the_call(project):
    t = make(project, [Thread(n=1, ts="x", kind="pending", prompt="a", reply="r")])

    def call(prompt):  # an event arrives while Sonnet thinks
        fresh = taskfile.load(paths.task_path(project, "s1"))
        fresh.threads.append(Thread(n=2, ts="y", kind="working", prompt="next"))
        fresh.status = "working"
        taskfile.save(fresh)
        return {"title": "T", "digest": "D", "threads": [{"n": 1, "kind": "FYI", "digest": "d", "question": None}]}

    digest.digest_task(t, call)
    got = taskfile.load(paths.task_path(project, "s1"))
    assert got.status == "working" and [th.kind for th in got.threads] == ["FYI", "working"]


def test_wrong_thread_number_with_one_thread(project):
    t = make(project, [Thread(n=3, ts="x", kind="pending", reply="r")])
    digest.digest_task(t, fake({"title": "T", "digest": "D", "threads": [{"n": 1, "kind": "answer", "digest": "d", "question": "q?"}]}))
    assert taskfile.load(paths.task_path(project, "s1")).threads[0].kind == "answer"


def test_skipped_thread_does_not_stay_pending(project):
    t = make(project, [Thread(n=1, ts="x", kind="pending", reply="a"), Thread(n=2, ts="y", kind="pending", reply="b")])
    digest.digest_task(t, fake({"title": "T", "digest": "D", "threads": [{"n": 2, "kind": "review", "digest": "d", "question": None}]}))
    assert [th.kind for th in taskfile.load(paths.task_path(project, "s1")).threads] == ["FYI", "review"]


def test_failure_leaves_the_thread_pending(project):
    make(project, [Thread(n=1, ts="x", kind="pending", reply="a")])

    def call(prompt):
        raise digest.DigestError("boom")

    changed, errors = digest.digest_project(project, call)
    assert changed == [] and errors == ["s1: boom"]
    assert taskfile.load(paths.task_path(project, "s1")).threads[0].kind == "pending"


def test_tools_of_last_turn(tmp_path):
    lines = [
        {"type": "user", "message": {"content": "first prompt"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/old.py"}}]}},
        {"type": "user", "message": {"content": "second prompt"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q\nmore"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "passed"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/a.py"}}]}},
    ]  # fmt: skip
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    assert digest.tools_of_last_turn(str(f)) == ["Edit /a.py", "Bash pytest -q"]
    assert digest.tools_of_last_turn(str(tmp_path / "missing.jsonl")) == []


@pytest.mark.parametrize("text,limit", [("short", 100), ("x" * 1000, 100)])
def test_cut(text, limit):
    out = digest._cut(text, limit)
    assert len(out) <= limit + 20
