import json
from itertools import count

from brain_fry import actions, events, paths, taskfile, views

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


def test_each_event_is_logged_with_what_it_changed(project, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="brain_fry"):
        put(project, "UserPromptSubmit", prompt="fix the bug")
        put(project, "Stop", last_assistant_message="Done.")
        put(project, "SessionEnd", sid="unknown", reason="exit")
        events.consume(project)
    lines = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("event UserPromptSubmit s1 'fix the bug' in proj: new task") for m in lines)
    assert any("event Stop s1" in m and "status working -> waiting" in m and "thread 1 pending" in m for m in lines)
    assert any("event SessionEnd unknown in proj: ignored" in m for m in lines)


# ---- a session that becomes a task in the middle: its earlier turns come from its transcript


def _transcript(project, prompts, last_reply=True):
    """Prompts with promptId p1, p2, ...; every turn has a reply except maybe the last."""
    f = project / "t.jsonl"
    lines = []
    for i, p in enumerate(prompts, 1):
        lines.append({"type": "user", "origin": {"kind": "human"}, "promptId": f"p{i}", "cwd": str(project),
                      "timestamp": f"2026-11-10T08:0{i}:00Z", "message": {"role": "user", "content": p}})
        if i < len(prompts) or last_reply:
            lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": f"reply {i}"}]}})
    f.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return str(f)


def test_first_event_a_stop_imports_the_earlier_turns(project):
    t = _transcript(project, ["a", "b", "c"], last_reply=False)  # c: the turn that just ended
    put(project, "Stop", transcript_path=t, prompt_id="p3", last_assistant_message="final reply of c")
    events.consume(project)
    th = task(project).threads
    assert [(x.n, x.prompt, x.reply, x.kind) for x in th] == [
        (1, "a", "reply 1", "imported"), (2, "b", "reply 2", "imported"), (3, "c", "final reply of c", "pending")]


def test_first_event_a_prompt_that_the_transcript_has_already(project):
    t = _transcript(project, ["a", "b", "new one"], last_reply=False)
    put(project, "UserPromptSubmit", transcript_path=t, prompt_id="p3", prompt="new one")
    events.consume(project)
    th = task(project).threads
    assert [(x.prompt, x.kind) for x in th] == [("a", "imported"), ("b", "imported"), ("new one", "working")]


def test_first_event_a_prompt_that_the_transcript_has_not_yet(project):
    t = _transcript(project, ["a", "b"])
    put(project, "UserPromptSubmit", transcript_path=t, prompt="new one")  # no prompt_id: by text
    events.consume(project)
    assert [(x.n, x.prompt, x.kind) for x in task(project).threads] == [
        (1, "a", "imported"), (2, "b", "imported"), (3, "new one", "working")]


def test_first_event_a_stop_without_prompt(project):
    """A background task that finishes: its prompt_id is not the last prompt's."""
    t = _transcript(project, ["a", "b"])
    put(project, "Stop", transcript_path=t, prompt_id="bg", last_assistant_message="background done")
    events.consume(project)
    assert [(x.prompt, x.reply, x.kind) for x in task(project).threads] == [
        ("a", "reply 1", "imported"), ("b", "reply 2", "imported"), ("", "background done", "pending")]


def test_first_event_blocked_keeps_the_turn_open(project):
    t = _transcript(project, ["a", "b"], last_reply=False)
    put(project, "Notification", transcript_path=t, notification_type="permission_prompt", message="needs Bash")
    put(project, "Stop", transcript_path=t, prompt_id="p2", last_assistant_message="done b")
    events.consume(project)
    assert [(x.prompt, x.reply, x.kind) for x in task(project).threads] == [
        ("a", "reply 1", "imported"), ("b", "done b", "pending")]


def test_later_events_import_nothing(project):
    put(project, "UserPromptSubmit", prompt="first")
    events.consume(project)
    t = _transcript(project, ["first", "x", "y"])
    put(project, "Stop", transcript_path=t, prompt_id="p3", last_assistant_message="done")
    events.consume(project)
    assert [(x.prompt, x.kind) for x in task(project).threads] == [("first", "pending")]


def test_close_archives_the_task(project):
    put(project, "UserPromptSubmit", prompt="close")
    put(project, "Stop", last_assistant_message="Wrote the findings to DESIGN.md.\n\n**closed :-)**\n")
    events.consume(project)
    t = task(project)
    assert (t.status, t.live, t.inbox, t.unread) == ("done", True, False, False)
    assert t.threads[-1].kind == "pending"  # the digest still runs

    put(project, "SessionEnd", reason="exit")
    events.consume(project)
    assert (task(project).status, task(project).live) == ("done", False)

    put(project, "UserPromptSubmit", prompt="one more thing")  # resumed and prompted: back from the archive
    events.consume(project)
    assert task(project).status == "working"


def test_close_unpins_the_task(project):
    put(project, "UserPromptSubmit", prompt="start")
    events.consume(project)
    actions.pin(task(project).id)
    put(project, "UserPromptSubmit", prompt="close")
    put(project, "Stop", last_assistant_message="Notes written.\nclosed :-)")
    events.consume(project)
    assert (task(project).status, task(project).pinned) == ("done", False)


def test_close_mark_before_a_trailer(project):
    put(project, "UserPromptSubmit", prompt=" Close.\n")
    put(project, "Stop", last_assistant_message="Done.\nclosed :-)\n[needs: fyi] notes written")
    events.consume(project)
    assert task(project).status == "done"


def test_x_closes_like_close(project):
    put(project, "UserPromptSubmit", prompt="X")
    put(project, "Stop", last_assistant_message="Notes written.\nclosed :-)")
    events.consume(project)
    assert task(project).status == "done"


def test_x_inside_a_prompt_does_not_close(project):
    put(project, "UserPromptSubmit", prompt="x axis")
    put(project, "Stop", last_assistant_message="Done.\nclosed :-)")
    events.consume(project)
    assert task(project).status == "waiting"


def test_close_without_the_mark_goes_to_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="close")
    put(project, "Stop", last_assistant_message="Before I close: keep the spike folder?")
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox) == ("waiting", True)


def test_mark_without_close_prompt_goes_to_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="close the dialog when the user clicks outside")
    put(project, "Stop", last_assistant_message="Done.\nclosed :-)")
    events.consume(project)
    assert (task(project).status, task(project).inbox) == ("waiting", True)


def _bg(kind="subagent", status="running", **more):
    return {"id": "a1", "type": kind, "status": status, "description": "Find calm portrait photos", **more}


def test_stop_while_a_background_agent_runs_stays_out_of_the_inbox(project):
    """The sequence seen on 2026-11-10: the reply says "I'll wait for the photos", the agent's
    result comes back as a prompt that Claude Code writes, and that turn's Stop is the end."""
    put(project, "UserPromptSubmit", prompt="find photos")
    put(project, "Stop", last_assistant_message="I'll wait for the photos.", background_tasks=[_bg()])
    events.consume(project)
    t = task(project)
    assert (t.status, t.live, t.inbox, t.unread) == ("working", True, False, False)
    assert (t.threads[0].kind, t.threads[0].reply) == ("pending", "I'll wait for the photos.")  # digested as usual

    put(project, "UserPromptSubmit", prompt='<agent-message from="a1">\n[Subagent hand-back] ...')
    put(project, "Stop", last_assistant_message="Here are 3 photos.", background_tasks=[])
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.unread) == ("waiting", True, True)
    assert [th.kind for th in t.threads] == ["pending", "pending"]


def test_stop_while_a_workflow_runs_stays_out_of_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="review it")
    put(project, "Stop", last_assistant_message="Started.", background_tasks=[_bg("workflow", "pending")])
    events.consume(project)
    assert (task(project).status, task(project).inbox) == ("working", False)


def test_stop_after_a_block_while_an_agent_runs_leaves_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Notification", notification_type="permission_prompt", message="needs Bash")
    put(project, "Stop", last_assistant_message="Waiting for the agent.", background_tasks=[_bg()])
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.notification) == ("working", False, None)


def test_background_shell_or_monitor_holds_the_reply_for_the_digest(project):
    """A dev server, or a loop that waits for another session: the digest tells (test_digest.py)."""
    put(project, "UserPromptSubmit", prompt="start the server")
    put(project, "Stop", last_assistant_message="It runs on :8000.",
        background_tasks=[_bg("shell", command="npm run dev"), _bg("monitor")])  # fmt: skip
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.unread, t.held, t.threads[0].kind) == ("working", False, False, True, "pending")


def test_next_event_ends_the_hold(project):
    put(project, "UserPromptSubmit", prompt="wait")
    put(project, "Stop", last_assistant_message="I'll wait.", background_tasks=[_bg("shell")])
    put(project, "UserPromptSubmit", prompt="<task-notification>\n...")  # the shell ended: the next turn
    events.consume(project)
    assert (task(project).held, task(project).status) == (False, "working")
    put(project, "Stop", last_assistant_message="Done.", background_tasks=[])
    events.consume(project)
    t = task(project)
    assert (t.held, t.status, t.inbox) == (False, "waiting", True)


def test_session_end_during_a_hold_goes_to_the_inbox(project):
    """The shell died with the session: nothing will wake the agent."""
    put(project, "UserPromptSubmit", prompt="wait")
    put(project, "Stop", last_assistant_message="I'll wait.", background_tasks=[_bg("shell")])
    put(project, "SessionEnd", reason="other")
    events.consume(project)
    t = task(project)
    assert (t.held, t.status, t.inbox, t.unread, t.live) == (False, "waiting", True, True, False)


def test_agents_and_shells_together_wait_for_the_agents(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Stop", last_assistant_message="Waiting.", background_tasks=[_bg(), _bg("shell")])
    events.consume(project)
    assert (task(project).status, task(project).inbox, task(project).held) == ("working", False, False)


def test_finished_or_odd_background_entries_do_not_hold_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Stop", last_assistant_message="done", background_tasks=[_bg(status="completed"), "junk", {}])
    events.consume(project)
    assert task(project).inbox is True


def test_close_while_an_agent_runs_still_archives(project):
    put(project, "UserPromptSubmit", prompt="close")
    put(project, "Stop", last_assistant_message="Notes written.\nclosed :-)", background_tasks=[_bg()])
    events.consume(project)
    assert task(project).status == "done"


def test_session_end_mid_turn_goes_to_the_inbox(project):
    """A restart of the app kills the turn in progress (2 of 44 sessions on 2026-11-10).
    The task comes back in the inbox, to be resumed with "Continue"."""
    put(project, "UserPromptSubmit", prompt="fix the bug")
    put(project, "SessionEnd", reason="other")
    events.consume(project)
    t = task(project)
    assert (t.status, t.live, t.inbox, t.unread) == ("waiting", False, True, True)
    assert t.threads[0].kind == "interrupted"
    assert views.inbox_line(t) == "interrupted: fix the bug"

    put(project, "UserPromptSubmit", prompt="continue")  # resumed
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox) == ("working", False)
    assert [th.kind for th in t.threads] == ["interrupted", "working"]


def test_session_end_while_blocked_goes_to_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Notification", notification_type="permission_prompt", message="needs Bash")
    put(project, "SessionEnd", reason="other")
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.notification, t.threads[0].kind) == ("waiting", True, None, "interrupted")


def test_session_end_while_agents_run_goes_to_the_inbox(project):
    """The agents died with the session: their results will never start a turn."""
    put(project, "UserPromptSubmit", prompt="find photos")
    put(project, "Stop", last_assistant_message="I'll wait for the photos.", background_tasks=[_bg()])
    put(project, "SessionEnd", reason="other")
    events.consume(project)
    t = task(project)
    assert (t.status, t.inbox, t.unread) == ("waiting", True, True)
    assert t.threads[0].kind == "pending"  # its reply is kept and digested
    assert views.inbox_line(t) == "I'll wait for the photos."


def test_session_end_after_the_app_marked_it_ended(project):
    """At quit, the app ends the task (set_live) before it reads the hook's SessionEnd,
    which it applies at the next start. The event then changes nothing."""
    put(project, "UserPromptSubmit", prompt="a")
    events.consume(project)
    actions.set_live(task(project).id, False)
    actions.mark_read(task(project).id)  # I looked at it after the restart
    put(project, "SessionEnd", reason="other")
    events.consume(project)
    t = task(project)
    assert (t.status, t.live, t.inbox, t.unread, t.threads[0].kind) == ("waiting", False, True, False, "interrupted")


def test_session_end_when_idle_stays_out_of_the_inbox(project):
    put(project, "UserPromptSubmit", prompt="a")
    put(project, "Stop", last_assistant_message="done")
    events.consume(project)
    actions._update(task(project).id, inbox=False, unread=False)  # as if read and set aside
    put(project, "SessionEnd", reason="other")
    events.consume(project)
    t = task(project)
    assert (t.status, t.live, t.inbox, t.unread) == ("waiting", False, False, False)
