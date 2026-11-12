from brain_fry import views
from brain_fry.taskfile import Task, Thread


def task(**kw) -> Task:
    return Task(session_ids=["s1"], project="/p", cwd="/p", **kw)


def test_goal_line_takes_the_goal_without_its_label():
    digest = "Goal: stop forgetting what a task is about.\nDone: a mockup.\nOpen: the build."
    assert views.goal_line(digest) == "stop forgetting what a task is about."


def test_goal_line_without_a_goal_takes_the_first_line():
    assert views.goal_line("\n  The app now starts faster.\nDone: x") == "The app now starts faster."
    assert views.goal_line("") == ""


def test_first_prompt_drops_the_paste_tags_and_image_refs():
    prompt = '\n\n<pasted_content id="1224">\nThe inbox is on the left.\n\nI keep switching. [Image #2]\n</pasted_content id="1224">\n'
    t = task(title="Task reminder", threads=[Thread(n=1, ts="x", prompt=prompt), Thread(n=2, ts="y", prompt="later")])
    assert views.first_prompt(t) == "The inbox is on the left. I keep switching."


def test_first_prompt_is_cut():
    t = task(title="T", threads=[Thread(n=1, ts="x", prompt="word " * 100)])
    got = views.first_prompt(t, width=40)
    assert len(got) == 40 and got.endswith("...")


def test_first_prompt_is_empty_until_the_title():
    """The title shown is then the prompt's first line already."""
    assert views.first_prompt(task(threads=[Thread(n=1, ts="x", prompt="fix the build")])) == ""


def test_first_prompt_of_a_planned_task_is_its_notes():
    assert views.first_prompt(task(title="Plan", notes="Look at\nthe logs")) == "Look at the logs"
