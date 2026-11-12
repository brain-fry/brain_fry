import pytest

from brain_fry import actions, events, views
from brain_fry.taskfile import Thread


def test_new_task_is_born_live_in_working(project):
    t = actions.new_task(project)
    got = actions.load(t.id)
    assert (got.prio, got.status, got.live, got.inbox) == ("working", "waiting", True, False)


def test_new_task_starts_with_the_tabs_of_the_previous_task(project, home):
    from brain_fry import projects

    prev = actions.new_task(project)
    actions._update(prev.id, open_files=["a.py", "docs/b.md"], browser_tabs=["https://example.com/"],
                    terminal_tabs=["zsh", "zsh"], status="done")  # fmt: skip  (archived: still copied)
    t = actions.load(actions.new_task(project, like=prev.id).id)
    assert (t.open_files, t.browser_tabs, t.terminal_tabs) == (["a.py", "docs/b.md"], ["https://example.com/"], ["zsh", "zsh"])
    assert (t.status, t.opened) == ("waiting", t.created)  # the backend selects it: opened at birth

    other = home / "other"
    other.mkdir()
    projects.init(other)
    for like in (prev.id, "no-such-task"):  # another project's paths, or no task: nothing copied
        t = actions.load(actions.new_task(other, like=like).id)
        assert (t.open_files, t.browser_tabs, t.terminal_tabs) == ([], [], [])


def test_archive_and_back(project):
    t = actions.new_task(project)
    actions._update(t.id, inbox=True, unread=True)
    assert actions.mark_read(t.id).inbox is True
    a = actions.archive(t.id)
    assert (a.status, a.inbox) == ("done", False)
    assert actions.archive(t.id).status == "waiting"


def test_mark_opened_reads_and_keeps_board_order(project):
    t = actions.new_task(project)
    actions._update(t.id, inbox=True, unread=True, updated="2026-11-01T10:00:00+01:00")
    got = actions.mark_opened(t.id)
    assert (got.unread, got.inbox, got.updated) == (False, True, "2026-11-01T10:00:00+01:00")
    assert got.opened and actions.load(t.id).opened == got.opened
    assert views.task_json(actions.load(t.id), None)["opened"] == got.opened


def test_fork_copies_digest_prio_cwd(project):
    p = actions.new_task(project)
    actions._update(p.id, digest="Goal: x", prio="planning", cwd=str(project / "sub"), title="Parent",
                    open_files=["a.py"], browser_tabs=["https://example.com/"], terminal_tabs=["zsh"])  # fmt: skip
    f = actions.fork_task(p.id)
    got = actions.load(f.id)
    assert (got.parent, got.digest, got.prio, got.cwd, got.title) == (p.id, "Goal: x", "planning", str(project / "sub"), "")
    assert (got.open_files, got.browser_tabs, got.terminal_tabs) == (["a.py"], ["https://example.com/"], ["zsh"])
    assert got.id != p.id and got.opened == got.created


def test_fork_gets_its_own_short_title(project):
    p = actions.new_task(project)
    actions._update(p.id, title="Parent", short_title="Parent topic")
    assert actions.load(actions.fork_task(p.id).id).short_title == ""


def test_rename_sets_the_short_title_too(project):
    t = actions.new_task(project)
    actions._update(t.id, title="Sonnet's title", short_title="Sonnet's")
    got = actions.rename(t.id, "  My own name for it ")
    assert (got.title, got.short_title) == ("My own name for it", "My own name for it")


def test_set_live_false_while_working_goes_to_the_inbox(project):
    """The app quits or crashed in the middle of a turn: the turn was cut off."""
    t = actions.new_task(project)
    actions._update(t.id, status="working", threads=[Thread(n=1, ts="x", prompt="fix it")])
    got = actions.set_live(t.id, False)
    assert (got.live, got.status, got.inbox, got.unread) == (False, "waiting", True, True)
    assert got.threads[0].kind == "interrupted"
    assert views.inbox_line(got) == "interrupted: fix it"


def test_set_live_false_while_idle_stays_out_of_the_inbox(project):
    t = actions.new_task(project)  # never prompted: waiting, not in the inbox
    got = actions.set_live(t.id, False)
    assert (got.live, got.status, got.inbox, got.unread) == (False, "waiting", False, False)


def test_unknown_task_and_bad_prio(project):
    with pytest.raises(actions.NotFound):
        actions.load("nope")
    t = actions.new_task(project)
    with pytest.raises(ValueError):
        actions.set_prio(t.id, "someday")


def test_task_json(project):
    t = actions.new_task(project)
    t = actions._update(t.id, threads=[Thread(n=1, ts="x", kind="answer", digest="Asks X.", question="X?", prompt="do it\nmore")])
    j = views.task_json(t, None)
    assert (j["line"], j["question"], j["threads"][0]["prompt"], j["outside"]) == ("Asks X.", "X?", "do it", True)
    assert views.task_json(t, 2)["outside"] is False


def _block(project, tid, ts):
    events.apply(project, {"session_id": tid, "hook_event_name": "Notification", "bf_ts": ts,
                           "notification_type": "permission_prompt", "message": "Claude needs your permission"})


def test_end_block_takes_the_task_out_of_the_inbox(project):
    t = actions.new_task(project)
    _block(project, t.id, 1000.0)
    assert actions.end_block(t.id, "busy", 994.0) is None  # a process status from before the block
    got = actions.load(t.id)
    assert (got.status, got.inbox, got.unread) == ("blocked", True, True)
    assert actions.end_block(t.id, "busy", 1040.0)  # I granted it: the agent goes on
    got = actions.load(t.id)
    assert (got.status, got.inbox, got.unread, got.notification) == ("working", False, False, None)
    assert actions.end_block(t.id, "busy", 1050.0) is None  # no longer blocked


def test_end_block_after_a_refusal(project):
    t = actions.new_task(project)
    _block(project, t.id, 1000.0)
    actions.end_block(t.id, "idle", 1040.0)  # I refused: the turn ended
    got = actions.load(t.id)
    assert (got.status, got.inbox) == ("waiting", False)


def test_pin_keeps_a_task_in_the_inbox_until_unpinned(project):
    t = actions.new_task(project)
    assert not views.in_inbox(actions.load(t.id))
    p = actions.pin(t.id)
    assert p.pinned and views.in_inbox(p) and views.task_json(p, None)["pinned"] is True
    events.apply(project, {"session_id": t.id, "hook_event_name": "UserPromptSubmit", "prompt": "go", "bf_ts": 1})
    answered = actions.load(t.id)
    assert (answered.inbox, answered.pinned) == (False, True) and views.in_inbox(answered)  # answering keeps it
    u = actions.pin(t.id)
    assert not u.pinned and not views.in_inbox(u)


def test_archive_unpins_and_an_archived_task_cannot_be_pinned(project):
    t = actions.new_task(project)
    actions.pin(t.id)
    a = actions.archive(t.id)
    assert (a.status, a.pinned) == ("done", False) and not views.in_inbox(a)
    with pytest.raises(ValueError):
        actions.pin(t.id)
    assert actions.archive(t.id).pinned is False  # unarchived: back unpinned


def test_inbox_order_puts_quiet_pinned_tasks_last(project):
    def make(updated, **flags):
        t = actions.new_task(project)
        return actions._update(t.id, updated=updated, **flags)

    quiet = make("2026-11-11T12:00:00", pinned=True)
    waiting = make("2026-11-11T10:00:00", inbox=True)
    blocked = make("2026-11-11T09:00:00", inbox=True, status="blocked")
    pinned_waiting = make("2026-11-11T11:00:00", pinned=True, inbox=True)
    got = sorted([quiet, waiting, blocked, pinned_waiting], key=views.inbox_order)
    assert [t.id for t in got] == [blocked.id, pinned_waiting.id, waiting.id, quiet.id]


def test_only_the_working_column_is_in_the_inbox(project):
    """Renaud, 2026-11-11: a task in planning (or a hidden column) waits, even pinned or with a
    reply; its flags stay, so it comes back to the inbox when it moves back to working."""
    t = actions.new_task(project)
    actions._update(t.id, inbox=True, unread=True, pinned=True)
    for prio in ("planning", "later", "more_later", "never"):
        assert not views.in_inbox(actions.set_prio(t.id, prio))
    back = actions.set_prio(t.id, "working")
    assert (back.inbox, back.unread, back.pinned) == (True, True, True) and views.in_inbox(back)


def test_park_others_moves_the_working_tasks_of_the_other_projects(project, home):
    from brain_fry import projects

    other = home / "other"
    other.mkdir()
    projects.init(other)
    mine = actions.new_task(project)
    theirs = actions._update(actions.new_task(other).id, inbox=True)
    archived = actions._update(actions.new_task(other).id, status="done")
    later = actions.set_prio(actions.new_task(other).id, "later")
    assert sorted(actions.park_others(project)) == sorted([theirs.id, archived.id])
    prios = {t.id: actions.load(t.id).prio for t in (mine, theirs, archived, later)}
    assert prios == {mine.id: "working", theirs.id: "planning", archived.id: "planning", later.id: "later"}
    assert actions.load(theirs.id).inbox and not views.in_inbox(actions.load(theirs.id))
    assert actions.park_others(project) == []
