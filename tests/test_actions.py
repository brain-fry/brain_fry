import pytest

from brain_fry import actions, views
from brain_fry.taskfile import Thread


def test_new_task_is_born_live_in_working(project):
    t = actions.new_task(project)
    got = actions.load(t.id)
    assert (got.prio, got.status, got.live, got.inbox) == ("working", "waiting", True, False)


def test_dismiss_archive_and_back(project):
    t = actions.new_task(project)
    actions._update(t.id, inbox=True, unread=True)
    assert actions.mark_read(t.id).inbox is True
    assert actions.dismiss(t.id).inbox is False
    a = actions.archive(t.id)
    assert (a.status, a.inbox) == ("done", False)
    assert actions.archive(t.id).status == "waiting"


def test_fork_copies_digest_prio_cwd(project):
    p = actions.new_task(project)
    actions._update(p.id, digest="Goal: x", prio="planning", cwd=str(project / "sub"), title="Parent")
    f = actions.fork_task(p.id)
    got = actions.load(f.id)
    assert (got.parent, got.digest, got.prio, got.cwd, got.title) == (p.id, "Goal: x", "planning", str(project / "sub"), "")
    assert got.id != p.id


def test_set_live_false_while_working_becomes_waiting(project):
    t = actions.new_task(project)
    actions._update(t.id, status="working")
    got = actions.set_live(t.id, False)
    assert (got.live, got.status) == (False, "waiting")


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
