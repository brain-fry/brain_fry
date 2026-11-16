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


def test_delete_moves_the_task_file_to_the_trash(project, tmp_path, monkeypatch):
    trash = tmp_path / "Trash"
    trash.mkdir()
    monkeypatch.setattr("brain_fry.files._move_to_trash", lambda p: p.rename(trash / p.name))  # a real call fills my Trash
    t, a = actions.new_task(project), actions.new_task(project)
    actions.archive(a.id)  # its file is in archive/
    for x in (t, a):
        assert actions.delete(x.id).id == x.id
        with pytest.raises(actions.NotFound):
            actions.load(x.id)
    assert sorted(f.name for f in trash.iterdir()) == sorted([f"{t.id}.md", f"{a.id}.md"])
    assert events.FORGOTTEN == {t.id, a.id}  # their sessions create no task again in this run
    with pytest.raises(actions.NotFound):
        actions.delete("no-such-task")


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
    def make(rank, **flags):
        t = actions.new_task(project)
        return actions._update(t.id, rank=rank, **flags)

    quiet = make(1.0, pinned=True)
    waiting = make(4.0, inbox=True)
    blocked = make(5.0, inbox=True, status="blocked")
    pinned_waiting = make(3.0, pinned=True, inbox=True)
    got = sorted([quiet, waiting, blocked, pinned_waiting], key=views.inbox_order)
    assert [t.id for t in got] == [blocked.id, pinned_waiting.id, waiting.id, quiet.id]


def test_inbox_follows_the_board_inside_a_color(project):
    """Renaud, 2026-11-16: of two green tasks, the one higher on the board is higher in the inbox,
    whatever their last update."""
    old = actions._update(actions.new_task(project).id, inbox=True, updated="2026-11-16T09:00:00+01:00")
    new = actions._update(actions.new_task(project).id, inbox=True, updated="2026-11-16T10:00:00+01:00")
    actions.place(old.id, new.id)
    assert [t.id for t in sorted(actions.all_tasks(), key=views.inbox_order)] == [old.id, new.id]
    actions.place(old.id, new.id, after=True)
    assert [t.id for t in sorted(actions.all_tasks(), key=views.inbox_order)] == [new.id, old.id]


def column(prio="working"):
    return [t.id for t in sorted((t for t in actions.all_tasks() if t.prio == prio), key=views.column_order)]


def test_a_new_task_lands_on_top_of_its_column(project):
    a = actions.new_task(project)
    b = actions.new_task(project)
    actions.place(a.id, b.id)  # a above b, in the second of their birth
    c = actions.new_task(project)
    assert column() == [c.id, a.id, b.id]
    assert actions.load(c.id).rank is not None  # written by the first save: an update does not move it
    actions._update(b.id, updated="2099-01-01T00:00:00+01:00")
    assert column() == [c.id, a.id, b.id]


def test_place_moves_a_task_above_or_below_another(project):
    ids = [actions._update(actions.new_task(project).id, rank=float(r)).id for r in range(4)]
    a, b, c, d = ids
    actions.place(d, b)  # d just above b
    assert column() == [a, d, b, c]
    actions.place(a, c, after=True)  # a to the bottom
    assert column() == [d, b, c, a]
    actions.place(c, d)  # c to the top
    assert column() == [c, d, b, a]
    with pytest.raises(ValueError):
        actions.place(a, a)
    other = actions.set_prio(actions.new_task(project).id, "planning")
    with pytest.raises(ValueError):
        actions.place(a, other.id)  # not in its column


def test_place_numbers_the_column_again_when_the_gap_is_too_small(project):
    a, b, c = (actions._update(actions.new_task(project).id, rank=r).id for r in (0.0, 0.0001, 0.0002))
    actions.place(c, b)
    assert column() == [a, c, b]
    assert [actions.load(i).rank for i in (a, c, b)] == [0.0, 1.0, 2.0]
    tied = [actions._update(actions.new_task(project).id, prio="next", rank=5.0).id for _ in range(3)]
    top, mid, bottom = column("next")
    actions.place(bottom, top)  # a tie gets an order too
    assert column("next") == [bottom, top, mid] and sorted(tied) == sorted(column("next"))


def test_another_column_puts_the_task_on_top(project):
    a = actions.set_prio(actions.new_task(project).id, "next")
    b = actions.set_prio(actions.new_task(project).id, "next")
    w = actions.new_task(project)
    actions.place(a.id, b.id)
    assert column("next") == [a.id, b.id]
    actions.set_prio(w.id, "next")
    assert column("next") == [w.id, a.id, b.id]
    rank = actions.load(b.id).rank
    assert actions.set_prio(b.id, "next").rank == rank  # its own column: nothing moves
    assert actions.set_prio(b.id, "working", rank=7.5).rank == 7.5  # the Undo of park: back at its place


def test_migrate_rank_keeps_the_board_of_the_last_updates(project):
    from brain_fry import taskfile

    old = actions._update(actions.new_task(project).id, updated="2026-11-15T09:00:00+01:00")
    recent = actions._update(actions.new_task(project).id, updated="2026-11-15T10:00:00+01:00")
    for t in (old, recent):  # files written before ranks
        path = taskfile.path_of(project, t.id)
        path.write_text("\n".join(line for line in path.read_text().split("\n") if not line.startswith("rank:")))
        assert actions.load(t.id).rank is None
    assert actions.migrate_rank() == 2
    assert column() == [recent.id, old.id]
    assert actions.migrate_rank() == 0


def test_only_the_working_column_is_in_the_inbox(project):
    """Renaud, 2026-11-11: a task in planning (or next, or a hidden column) waits, even pinned or with a
    reply; its flags stay, so it comes back to the inbox when it moves back to working."""
    t = actions.new_task(project)
    actions._update(t.id, inbox=True, unread=True, pinned=True)
    for prio in ("next", "planning", "later", "more_later", "never"):
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
    assert actions.park_others(project) == [theirs.id]  # open tasks only: the archive is out of all_tasks (2026-11-14)
    assert column("next")[0] == theirs.id  # on top of next
    prios = {t.id: actions.load(t.id).prio for t in (mine, theirs, archived, later)}
    assert prios == {mine.id: "working", theirs.id: "next", archived.id: "working", later.id: "later"}
    assert actions.load(theirs.id).inbox and not views.in_inbox(actions.load(theirs.id))
    assert actions.park_others(project) == []


def test_park_others_keeps_the_order_of_the_moved_tasks(project, home):
    from brain_fry import projects

    other = home / "other"
    other.mkdir()
    projects.init(other)
    low, high = (actions._update(actions.new_task(other).id, rank=r).id for r in (2.0, 1.0))
    queued = actions.set_prio(actions.new_task(project).id, "next")
    assert actions.park_others(project) == [high, low]
    assert column("next") == [high, low, queued.id]


def inbox():
    return views.inbox_list(actions.all_tasks())


def test_waiting_for_hides_a_task_until_the_other_is_archived(project):
    """Renaud, 2026-11-16: "As long as the other item is not closed (meaning archived), the other one
    does not show up." Its flags stay, so it comes back with its reply; the link stays too."""
    a = actions._update(actions.new_task(project).id, inbox=True, unread=True)
    b = actions._update(actions.new_task(project).id, inbox=True)
    assert set(inbox()) == {a.id, b.id}
    actions.wait_for(a.id, b.id)
    assert inbox() == [b.id]
    assert views.working_list(actions.all_tasks()) == [b.id]  # not a row of "All working" either: only the board shows it
    actions.archive(b.id)
    back = actions.load(a.id)
    assert inbox() == [a.id] and (back.inbox, back.unread, back.waiting_for) == (True, True, b.id)
    actions.archive(b.id)  # unarchived (without its inbox flag, which the archive cleared): it holds the task back again
    assert inbox() == []


def test_waiting_for_a_deleted_task_holds_nothing_back(project, tmp_path, monkeypatch):
    trash = tmp_path / "Trash"
    trash.mkdir()
    monkeypatch.setattr("brain_fry.files._move_to_trash", lambda p: p.rename(trash / p.name))
    a = actions._update(actions.new_task(project).id, inbox=True)
    b = actions.new_task(project)
    actions.wait_for(a.id, b.id)
    assert inbox() == []
    actions.delete(b.id)
    assert inbox() == [a.id]


def test_red_and_pinned_tasks_wait_too(project):
    b = actions.new_task(project)
    red = actions._update(actions.new_task(project).id, inbox=True, status="blocked")
    pinned = actions._update(actions.new_task(project).id, pinned=True)
    for t in (red, pinned):
        actions.wait_for(t.id, b.id)
    assert inbox() == []


def test_waiting_for_follows_a_chain(project):
    a, b, c = (actions._update(actions.new_task(project).id, inbox=True) for _ in range(3))
    actions.wait_for(a.id, b.id)
    actions.wait_for(b.id, c.id)
    assert inbox() == [c.id]
    actions.archive(c.id)
    assert inbox() == [b.id]
    actions.archive(b.id)
    assert inbox() == [a.id]


def test_waiting_for_refuses_itself_an_archived_task_and_a_loop(project):
    a, b, c = (actions.new_task(project) for _ in range(3))
    with pytest.raises(ValueError):
        actions.wait_for(a.id, a.id)
    actions.archive(c.id)
    with pytest.raises(ValueError):
        actions.wait_for(a.id, c.id)
    with pytest.raises(actions.NotFound):
        actions.wait_for(a.id, "no-such-task")
    actions.archive(c.id)
    actions.wait_for(b.id, c.id)
    actions.wait_for(c.id, a.id)
    with pytest.raises(ValueError):  # a --> b --> c --> a would hide all three for good
        actions.wait_for(a.id, b.id)
    assert actions.load(a.id).waiting_for is None


def test_stop_waiting_and_what_the_front_end_gets(project):
    a = actions._update(actions.new_task(project).id, inbox=True)
    b = actions.new_task(project)
    actions.wait_for(a.id, b.id)
    tasks = actions.all_tasks()
    open_ids = views.open_set(tasks)
    assert views.task_json(actions.load(a.id), None, open_ids=open_ids)["waitingFor"] == b.id
    assert views.task_json(actions.load(b.id), None, open_ids=open_ids)["waitingFor"] is None
    actions.archive(b.id)  # the link holds nothing back: the front end shows no badge
    assert views.task_json(actions.load(a.id), None, open_ids=views.open_set(actions.all_tasks()))["waitingFor"] is None
    actions.archive(b.id)
    actions.wait_for(a.id, None)
    assert actions.load(a.id).waiting_for is None and inbox() == [a.id]


def test_a_fork_does_not_wait(project):
    a = actions.new_task(project)
    actions.wait_for(a.id, actions.new_task(project).id)
    assert actions.fork_task(a.id).waiting_for is None
