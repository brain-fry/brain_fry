from brain_fry.taskfile import Task, Thread, dumps, loads

NASTY = """Here is the plan.

## 3 | 2026-11-09T14:03:12+01:00 | review
**Reply**
# Threads
# Digest
---
> already quoted
>no space

```python
def f():
    return 1
```

trailing spaces
\ttab and unicode: é ü → 🙂"""


def make_task() -> Task:
    return Task(
        session_ids=["abc-123"],
        project="/Users/x/proj",
        cwd="/Users/x/proj/sub",
        title="Fix: the login bug",
        transcript_path="/Users/x/.claude/projects/p/abc-123.jsonl",
        prio="planning",
        status="blocked",
        live=True,
        inbox=True,
        unread=False,
        notification="Claude needs your permission to use Bash",
        created="2026-11-09T14:00:00+01:00",
        updated="2026-11-09T14:03:12+01:00",
        open_files=["src/a.py"],
        digest="Goal: fix login.\n\nDone: the fix.",
        threads=[
            Thread(n=1, ts="2026-11-09T14:00:00+01:00", kind="review", digest="Fixed it.", prompt="fix it", reply=NASTY),
            Thread(n=2, ts="2026-11-09T14:03:12+01:00", kind="answer", question="Which one?\nA or B?", prompt=NASTY),
            Thread(n=3, ts="2026-11-09T14:05:00+01:00", kind="working"),
        ],
    )


def test_round_trip():
    t = make_task()
    assert loads(dumps(t)) == t


def test_round_trip_is_stable():
    text = dumps(make_task())
    assert dumps(loads(text)) == text


def test_empty_task():
    t = Task(session_ids=["s"], project="/p", cwd="/p")
    assert loads(dumps(t)) == t


def test_unquoted_timestamp_from_a_hand_edit():
    text = dumps(make_task()).replace("created: '2026-11-09T14:00:00+01:00'", "created: 2026-11-09T14:00:00+01:00")
    assert "created: 2026-11-09T14:00:00+01:00" in text
    assert isinstance(loads(text).created, str)


def test_display_title_falls_back_to_first_prompt():
    t = Task(session_ids=["s"], project="/p", cwd="/p", threads=[Thread(n=1, ts="x", prompt="\n  do the thing\nmore")])
    assert t.display_title == "do the thing"
    t.title = "Named"
    assert t.display_title == "Named"
