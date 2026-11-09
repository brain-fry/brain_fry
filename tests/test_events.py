import json
from itertools import count

from brain_fry import events, paths, taskfile

_seq = count()


def put(project, name, sid="s1", **fields):
    """Drop an event file into the spool, as hook.sh would."""
    ev = {
        "session_id": sid,
        "hook_event_name": name,
        "cwd": str(project),
        "transcript_path": f"/t/{sid}.jsonl",
        "bf_ts": 1_800_000_000 + next(_seq),
        **fields,
    }
    (paths.spool_dir(project) / f"{ev['bf_ts']}-{sid}.json").write_text(json.dumps(ev))


def task(project, sid="s1"):
    return taskfile.load(paths.task_path(project, sid))


def test_full_turn(project):
    put(project, "UserPromptSubmit", prompt="fix the bug")
    assert events.consume(project) == ["s1"]
    t = task(project)
    assert (t.status, t.live, t.inbox, t.unread) == ("working", True, False, False)
    assert t.threads[0].prompt == "fix the bug" and t.threads[0].kind == "working"

    put(project, "Notification", notification_type="permission_prompt", message="needs Bash")
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.unread, t.notification) == ("blocked", True, True, "needs Bash")

    put(project, "Stop", last_assistant_message="Done. Review auth.py?")
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.unread, t.notification) == ("waiting", True, True, None)
    assert len(t.threads) == 1
    assert (t.threads[0].kind, t.threads[0].reply) == ("pending", "Done. Review auth.py?")

    put(project, "SessionEnd", reason="exit")
    events.consume(project)
    t = task(project)
    assert (t.status, t.live, t.inbox) == ("waiting", False, True)  # stays in the inbox

    assert list(paths.spool_dir(project).glob("*")) == []
    assert len(paths.event_log(project).read_text().splitlines()) == 4


def test_answering_clears_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Stop", last_assistant_message="question?")
    put(project, "UserPromptSubmit", prompt="b")
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.unread) == ("working", False, False)
    assert [th.kind for th in t.threads] == ["pending", "working"]


def test_interrupt_then_new_prompt(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "UserPromptSubmit", prompt="b")  # Esc in between: no Stop
    events.consume(project)
    assert [th.kind for th in task(project).threads] == ["interrupted", "working"]


def test_stop_without_prompt_makes_a_new_thread(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Stop", last_assistant_message="one")
    put(project, "Stop", last_assistant_message="background task finished")
    events.consume(project)
    t = task(project)
    assert [(th.prompt, th.reply) for th in t.threads] == [("a", "one"), ("", "background task finished")]


def test_session_end_alone_creates_no_task(project):
    put(project, "SessionEnd", sid="ghost", reason="exit")
    assert events.consume(project) == []
    assert not paths.task_path(project, "ghost").exists()


def test_session_end_while_working_becomes_waiting(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "SessionEnd", reason="exit")
    events.consume(project)
    t = task(project)
    assert (t.status, t.live, t.inbox) == ("waiting", False, False)


def test_idle_notification_is_not_blocked(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Stop", last_assistant_message="x")
    put(project, "Notification", notification_type="idle_prompt", message="waiting")
    events.consume(project)
    assert task(project).status == "waiting"


def test_archived_task_comes_back_on_activity(project):
    put(project, "UserPromptSubmit", prompt="a")
    events.consume(project)
    t = task(project)
    t.status, t.inbox = "done", False
    taskfile.save(t)
    put(project, "Stop", last_assistant_message="x")
    events.consume(project)
    assert task(project).status == "waiting"


def test_order_follows_bf_ts_not_file_names(project):
    spool = paths.spool_dir(project)
    base = {"session_id": "s1", "cwd": str(project)}
    (spool / "b.json").write_text(json.dumps({**base, "hook_event_name": "UserPromptSubmit", "prompt": "a", "bf_ts": 1}))
    (spool / "a.json").write_text(json.dumps({**base, "hook_event_name": "Stop", "last_assistant_message": "r", "bf_ts": 2}))
    events.consume(project)
    assert task(project).threads[0].reply == "r"


def test_broken_event_file_is_set_aside(project):
    (paths.spool_dir(project) / "x.json").write_text("{not json")
    assert events.consume(project) == []
    assert (paths.spool_dir(project) / "x.bad").exists()


def test_one_reader_at_a_time(project):
    """`bf list` while the app's reader runs: the second reader waits, no event is applied twice."""
    import fcntl
    import threading

    put(project, "UserPromptSubmit", prompt="a")
    with paths.consume_lock(project).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # another reader is at work
        result = []
        t = threading.Thread(target=lambda: result.append(events.consume(project)))
        t.start()
        t.join(0.3)
        assert t.is_alive() and not paths.task_path(project, "s1").exists()
    t.join(5)
    assert result == [["s1"]]
    assert events.consume(project) == []
    assert len(paths.event_log(project).read_text().splitlines()) == 1


def test_project_folder_gone(project, tmp_path):
    assert events.consume(tmp_path / "deleted") == []
