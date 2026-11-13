"""A task file, <project>/.brain_fry/<session-id>.md. See DESIGN_DETAILS.md, "Task file".

Layout: YAML frontmatter, then "# Digest", then "# Threads" with one "## n | ts | kind"
section per thread (TT). All free text (digests, prompts, replies) is written as
blockquote lines ("> ..."). So no content can be mistaken for structure, whatever
headings, fences or "---" lines it holds. The parser only reads lines that do not
start with ">" as structure.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

import yaml

from . import paths
from .util import write_atomic

PRIOS = ("working", "next", "planning", "later", "more_later", "never")
PARTS = ("digest", "question", "prompt", "reply")

# Held around every load-modify-save of a task file: the reader, the digest worker and
# the app's actions run in different threads of one process.
LOCK = threading.RLock()

THREAD_RE = re.compile(r"^## (\d+) \| (\S+) \| (\S+)$")
PART_RE = re.compile(r"^\*\*(Digest|Question|Prompt|Reply)\*\*$")


@dataclass
class Thread:
    n: int
    ts: str
    kind: str = "working"
    digest: str = ""
    question: str | None = None
    prompt: str = ""
    reply: str = ""


@dataclass
class Task:
    session_ids: list[str]
    project: str
    cwd: str
    title: str = ""
    short_title: str = ""  # 2 or 3 words, the heading of the task's card on the board
    transcript_path: str = ""
    prio: str = "working"
    status: str = "waiting"
    live: bool = False
    inbox: bool = False
    unread: bool = False
    pinned: bool = False  # stays in the inbox until I unpin it (or archive it)
    # a Stop that left a background shell or monitor running: out of the inbox until the
    # digest tells whether the agent waits for it (events.py, actions.settle_hold)
    held: bool = False
    notification: str | None = None
    parent: str | None = None
    created: str = ""
    updated: str = ""
    opened: str = ""  # last selected in Dev mode: the project dropdown lands on it
    open_files: list[str] = field(default_factory=list)
    terminal_tabs: list[str] = field(default_factory=list)
    browser_tabs: list[str] = field(default_factory=list)  # URLs of the browser tabs of 3a
    finder_tabs: list[dict] = field(default_factory=list)  # Finder tabs of column 3: {root, path} (absolute)
    notes: str = ""  # what I wrote when I created the task in Organize mode (actions.plan_task)
    digest: str = ""
    threads: list[Thread] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.session_ids[0]

    @property
    def display_title(self) -> str:
        """The title, or until the first digest, the first line of the first prompt."""
        if self.title:
            return self.title
        for t in self.threads:
            if t.prompt.strip():
                line = t.prompt.strip().splitlines()[0]
                return line if len(line) <= 70 else line[:69] + "..."
        return "(untitled)"


BODY_FIELDS = ("notes", "digest", "threads")
META_FIELDS = tuple(f.name for f in fields(Task) if f.name not in BODY_FIELDS)


def _quote(text: str | None) -> list[str]:
    if not text:
        return []
    return [f"> {line}" if line else ">" for line in text.split("\n")]


def dumps(task: Task) -> str:
    meta = {k: getattr(task, k) for k in META_FIELDS}
    out = ["---", yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10_000).rstrip(), "---"]
    if task.notes:  # only planned tasks have notes: the other files stay as they were
        out += ["", "# Notes", ""] + _quote(task.notes)
    out += ["", "# Digest", ""] + _quote(task.digest)
    out += ["", "# Threads"]
    for t in task.threads:
        out += ["", f"## {t.n} | {t.ts} | {t.kind}"]
        for part in PARTS:
            value = getattr(t, part)
            if value:
                out += ["", f"**{part.capitalize()}**"] + _quote(value)
    return "\n".join(out) + "\n"


def loads(text: str) -> Task:
    if not text.startswith("---\n"):
        raise ValueError("no YAML frontmatter")
    end = text.index("\n---\n", 3)
    meta = yaml.safe_load(text[4:end]) or {}
    for k, v in meta.items():  # a hand edit may drop the quotes around a timestamp
        if isinstance(v, (datetime, date)):
            meta[k] = v.isoformat()

    notes: list[str] = []
    digest: list[str] = []
    threads: list[dict] = []
    target: list[str] | None = None
    for line in text[end + 5 :].split("\n"):
        if line.startswith(">"):
            if target is not None:
                target.append(line[2:] if line.startswith("> ") else line[1:])
        elif line == "# Notes":
            target = notes
        elif line == "# Digest":
            target = digest
        elif line == "# Threads":
            target = None
        elif m := THREAD_RE.match(line):
            threads.append({"n": int(m[1]), "ts": m[2], "kind": m[3]})
            target = None
        elif (m := PART_RE.match(line)) and threads:
            target = threads[-1].setdefault(m[1].lower(), [])
        # blank lines and anything else: separators, ignored

    known = {f.name for f in fields(Task)}
    task = Task(**{k: v for k, v in meta.items() if k in known})
    task.notes = "\n".join(notes)
    task.digest = "\n".join(digest)
    for t in threads:
        parts = {p: "\n".join(t.pop(p)) for p in PARTS if p in t}
        if "question" in parts and not parts["question"]:
            parts["question"] = None
        task.threads.append(Thread(**t, **parts))
    return task


def load(path: Path) -> Task:
    return loads(Path(path).read_text())


def save(task: Task) -> Path:
    path = paths.task_path(Path(task.project), task.id)
    write_atomic(path, dumps(task))
    return path


def all_tasks(project: Path) -> list[Task]:
    out = []
    for f in sorted(paths.project_dir(project).glob("*.md")):
        try:
            out.append(load(f))
        except (ValueError, yaml.YAMLError, TypeError):
            continue  # not a task file, or a broken one: skip, never crash the reader
    return out
