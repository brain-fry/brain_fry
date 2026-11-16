"""A task file, <project>/.brain_fry/<session-id>.md. See DESIGN_DETAILS.md, "Task file".

Layout: YAML frontmatter, then "# Digest", then "# Threads" with one "## n | ts | kind"
section per thread (TT). All free text (digests, prompts, replies) is written as
blockquote lines ("> ..."). So no content can be mistaken for structure, whatever
headings, fences or "---" lines it holds. The parser only reads lines that do not
start with ">" as structure.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

import yaml

from . import paths
from .util import write_atomic

log = logging.getLogger(__name__)

PRIOS = ("working", "next", "planning", "later", "more_later", "never")
PARTS = ("digest", "question", "prompt", "reply")

# Held around every load-modify-save of a task file: the reader, the digest worker and
# the app's actions run in different threads of one process.
LOCK = threading.RLock()

# The frontmatter is read with libyaml's safe loader when PyYAML was built with it (5 times
# faster: 13 ms instead of 71 ms for 133 task files, 2026-11-14), else with the pure-Python one.
# Both build the same values with the same safe constructor. Never an unsafe loader: a task file
# is data. The writer stays pure Python (`dumps`): libyaml writes an emoji as "\U0001F600".
_YAML_LOADER = yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader

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
    # the place in its column, the smallest on top (views.rank, actions.place); the inbox follows it
    # inside a color. Set by the first save (save) or, for a file older than ranks, at start (actions.migrate_rank)
    rank: float | None = None
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
    # where each tab of column 3 sits: {pane, act, at, cols, order} (gridRecord in grid.js, actions.set_grid)
    grid: dict = field(default_factory=dict)
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
    meta = yaml.load(text[4:end], Loader=_YAML_LOADER) or {}
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
    if not isinstance(task.grid, dict):  # `grid:` left empty by a hand edit
        task.grid = {}
    task.notes = "\n".join(notes)
    task.digest = "\n".join(digest)
    for t in threads:
        parts = {p: "\n".join(t.pop(p)) for p in PARTS if p in t}
        if "question" in parts and not parts["question"]:
            parts["question"] = None
        task.threads.append(Thread(**t, **parts))
    return task


def load(path: Path) -> Task:
    task = loads(Path(path).read_text())
    if (home := paths.project_of(path)) and not _same_folder(task.project, home):
        _rehome(task, home)
    return task


# A project folder that I move or rename takes its task files along, and their `project` and `cwd`
# still name the old place (2026-11-14: serendipity --> brain_fry; `save` wrote to the old folder
# and failed on every event). So the folder that holds the file is the task's project, whatever
# the file says. Corrected in memory at each load; the file gets it at its next save.
_moved: set[tuple[str, str]] = set()  # (old, new), to log each move once per run


def _same_folder(a: str, b: Path) -> bool:
    if Path(a) == b:
        return True
    try:
        return os.path.samefile(a, b)  # two spellings of one folder (a symlink, /private/var)
    except OSError:  # the old folder is gone
        return False


def _rehome(task: Task, home: Path) -> None:
    old = Path(task.project)
    task.project = str(home)
    try:
        task.cwd = str(home / Path(task.cwd).relative_to(old))
    except ValueError:  # no cwd, or a session that started outside the old folder: nothing moved
        pass
    if (old.as_posix(), str(home)) not in _moved:
        _moved.add((old.as_posix(), str(home)))
        log.info("task files of %s name the project %s: the folder was moved, and the folder wins", home.name, old.name)


# The file follows the status (DESIGN_DETAILS.md, "Task file"): an archived task (status done) lives
# in <project>/.brain_fry/archive/<id>.md, any other in <project>/.brain_fry/<id>.md. The app reads
# the open ones every few seconds; the archive only on demand.


def path_of(project: Path, task_id: str) -> Path:
    """Where the task's file is: the top folder, else the archive. The top folder when it is in neither."""
    top = paths.task_path(project, task_id)
    if top.exists():
        return top
    archived = paths.archive_path(project, task_id)
    return archived if archived.exists() else top


def save(task: Task) -> Path:
    """Writes the task where its status puts it, and removes the copy at the other place (an
    archive, an unarchive). Written first, removed second: a crash in between leaves two copies,
    and the top folder's wins (path_of, migrate). A task without a rank: a new file gets the time of
    its first save, to the microsecond (`created` keeps the second), so that it lands above every task
    born or moved before it; an older file, its birth."""
    project = Path(task.project)
    path, other = paths.task_path(project, task.id), paths.archive_path(project, task.id)
    if task.rank is None:
        old = path.exists() or other.exists()
        task.rank = -((_epoch(task.created) if old else 0.0) or time.time())
    if task.status == "done":
        path, other = other, path
        path.parent.mkdir(exist_ok=True)
    write_atomic(path, dumps(task))
    other.unlink(missing_ok=True)
    return path


def _read_all(folder: Path) -> list[Task]:
    out = []
    for f in sorted(folder.glob("*.md")):
        try:
            out.append(load(f))
        except (ValueError, yaml.YAMLError, TypeError):
            continue  # not a task file, or a broken one: skip, never crash the reader
    return out


def all_tasks(project: Path) -> list[Task]:
    """The open tasks of the project: not the archive."""
    return _read_all(paths.project_dir(project))


def archived_tasks(project: Path) -> list[Task]:
    return _read_all(paths.archive_dir(project))


def migrate(project: Path) -> int:
    """At the start of the app: the archived task files of the top folder (written before the archive
    folder existed, or left by a crash in save) move to the archive. A rename keeps the file's time.
    Returns how many moved."""
    moved = 0
    for task in all_tasks(project):
        if task.status == "done":
            with LOCK:
                src = paths.task_path(Path(project), task.id)
                dst = paths.archive_path(Path(project), task.id)
                dst.parent.mkdir(exist_ok=True)
                src.replace(dst)
            moved += 1
    return moved


# The last use of a project's archived tasks, for the order of the projects (views.project_order):
# their `opened` and `updated`, read from the head of each file with a regex (no YAML), again only
# when the archive folder changed (every save renames a file in it, so its time moves). Not the
# files' own times: a session that ends (the quit of the app, the close after 4 h) writes its
# archived task, hours after I last used it.
_HEAD = re.compile(r"^(opened|updated): '?([^'\n]*)'?$", re.M)
_last_use: dict[str, tuple[int, tuple | None]] = {}  # archive folder --> (its mtime, last use)


def archive_last_use(project: Path) -> tuple[float, float, str] | None:
    """(opened, updated, its ISO text) of the archived task that I used last, as epochs; or None."""
    folder = paths.archive_dir(project)
    try:
        version = folder.stat().st_mtime_ns
    except OSError:
        return None
    cached = _last_use.get(str(folder))
    if cached and cached[0] == version:
        return cached[1]
    best = None
    for f in folder.glob("*.md"):
        try:
            with f.open() as fh:
                head = fh.read(16384)
        except OSError:
            continue
        end = head.find("\n---\n", 3)  # the end of the frontmatter
        found = dict(_HEAD.findall(head[:end] if end > 0 else head))
        cand = (_epoch(found.get("opened", "")), _epoch(found.get("updated", "")), found.get("opened") or found.get("updated", ""))
        if best is None or cand[:2] > best[:2]:
            best = cand
    _last_use[str(folder)] = (version, best)
    return best


def _epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0
