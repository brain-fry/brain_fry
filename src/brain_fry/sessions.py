"""Claude Code sessions seen from Claude Code's own files. See DESIGN_DETAILS.md, "Other sessions".

- ~/.claude/projects/<slug>/<session id>.jsonl: one transcript per session. Read for the
  sessions of my projects that are not tasks (the bottom region of the board), and to
  import one as a task.
- ~/.claude/sessions/<pid>.json: one file per running Claude Code process (session id,
  entrypoint, name, status idle, busy or waiting (a dialog waits for me), and when the
  status last changed). Tells which sessions run now, where, and when a dialog closed.

Both formats are internal to Claude Code and may change: everything here is best effort.
CLAUDE_CONFIG_DIR moves ~/.claude, as it does for Claude Code itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import NamedTuple
from datetime import datetime
from pathlib import Path

from . import paths, projects, taskfile
from .logs import short
from .taskfile import PRIOS, Task, Thread
from .util import iso

log = logging.getLogger(__name__)

WHERE = {"claude-vscode": "VS Code", "cli": "a terminal", "sdk-cli": "a script"}
# Text blocks that Claude Code adds to a prompt (IDE context, image captions)
ADDED_BLOCKS = ("<ide_", "<system-reminder>", "[Image")
# Messages that Claude Code writes in my name. Only needed for transcripts that do not
# have `origin` yet (before about 2026-10).
NOT_TYPED = ("<local-command", "<system-reminder>", "[Request interrupted", "This session is being continued",
             "Base directory for this skill", "Your response above was cut off", "Caveat:")  # fmt: skip
COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SESSION_ID_RE = re.compile(r"[0-9a-fA-F-]{8,64}")


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


# ------------------------------------------------------------------ running processes


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def running() -> dict[str, dict] | None:
    """Session id -> {pid, where, status, since, name} for each Claude Code process alive now.
    status: idle, busy, or waiting while a dialog waits for me (a permission, a question:
    `waitingFor` says which; seen in Claude Code 2.1.322). since: when the status last
    changed (epoch seconds), None if the file does not say.
    None when nothing can be said: no such folder, or files in a format we do not know."""
    d = claude_dir() / "sessions"
    if not d.is_dir():
        return None
    out: dict[str, dict] = {}
    files = known = 0
    for f in d.glob("*.json"):
        files += 1
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sid, pid = info.get("sessionId"), info.get("pid")
        if not (isinstance(sid, str) and isinstance(pid, int)):
            continue
        known += 1
        if _alive(pid):
            since = info.get("statusUpdatedAt")
            out[sid] = {
                "pid": pid,
                "where": WHERE.get(info.get("entrypoint"), info.get("entrypoint") or "?"),
                "status": info.get("status") or "",
                "since": since / 1000 if isinstance(since, (int, float)) else None,  # ms in the file
                # "derived" names are the folder name plus a number: not a title
                "name": info.get("name") if info.get("nameSource") != "derived" else None,
            }
    if files and not known:
        return None
    return out


# ------------------------------------------------------------------ transcripts


def human_prompt(e: dict) -> str | None:
    """The prompt that I typed in this transcript entry, or None (tool results, messages
    that Claude Code adds itself, subagents)."""
    if e.get("type") != "user" or e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary") \
            or e.get("isVisibleInTranscriptOnly"):  # fmt: skip
        return None
    origin = e.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return None
    content = (e.get("message") or {}).get("content")
    image = False
    if isinstance(content, str):
        blocks = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        blocks = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
        image = any(isinstance(b, dict) and b.get("type") == "image" for b in content)
    else:
        return None
    text = "\n\n".join(b.strip() for b in blocks if b.strip() and not b.lstrip().startswith(ADDED_BLOCKS))
    if m := COMMAND_RE.search(text):  # a slash command: show it as I typed it
        args = ARGS_RE.search(text)
        return f"{m.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    if not text:
        return "(image)" if image else None
    if origin is None and text.startswith(NOT_TYPED):
        return None
    return text


TAG_LINE_RE = re.compile(r"</?[A-Za-z_][\w-]*(\s[^<>]*)?>")


def _first_line(text: str, width: int = 100) -> str:
    """The first line with words: not a wrapper tag like <pasted_content id="c7bd">."""
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or TAG_LINE_RE.fullmatch(line):
            continue
        return line if len(line) <= width else line[: width - 3] + "..."
    return ""


def _local(ts: str | None) -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return iso()


def cwd_of(path: Path) -> str:
    """Where the session started: the cwd of its first entry that has one."""
    try:
        with path.open("rb") as f:
            for i, raw in enumerate(f):
                if i > 500:
                    break
                if b'"cwd"' not in raw:
                    continue
                try:
                    cwd = json.loads(raw).get("cwd")
                except ValueError:
                    continue
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


class Turns(NamedTuple):
    title: str  # the last /rename, or the name that a hook set
    threads: list[Thread]  # one per prompt that I typed, kind "imported"
    prompt_ids: list[str | None]  # promptId of each prompt: the prompt_id of its hook events


def read_turns(path: Path) -> Turns:
    """One thread per prompt that I typed: the prompt, and as reply the text after the last
    tool call of the turn."""
    title, threads, ids, cur, buf = "", [], [], None, []
    with path.open("rb") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            kind = e.get("type")
            if kind == "custom-title" and e.get("customTitle"):
                title = str(e["customTitle"])
            elif kind == "user":
                text = human_prompt(e)
                if text is not None:
                    if cur:
                        cur.reply = "\n\n".join(buf)
                    cur = Thread(n=len(threads) + 1, ts=_local(e.get("timestamp")), kind="imported", prompt=text)
                    threads.append(cur)
                    ids.append(e.get("promptId"))
                    buf = []
            elif kind == "assistant" and cur is not None and not e.get("isSidechain"):
                content = (e.get("message") or {}).get("content")
                for b in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        buf = []  # text before a tool call is narration, not the reply
                    elif b.get("type") == "text" and (b.get("text") or "").strip():
                        buf.append(b["text"].strip())
    if cur:
        cur.reply = "\n\n".join(buf)
    return Turns(title, threads, ids)


@dataclass
class _Summary:
    """What the board shows of a transcript, kept up to date by reading only what was appended."""

    ino: int = 0
    size: int = 0  # bytes read so far, up to the last complete line
    entrypoint: str = ""
    title: str = ""
    prompts: int = 0
    first_prompt: str = ""
    last_prompt: str = ""

    def feed(self, raw: bytes) -> None:
        if b'"user"' not in raw and b"customTitle" not in raw and (self.entrypoint or b"entrypoint" not in raw):
            return  # most lines (assistant, tool results) are skipped without parsing
        try:
            e = json.loads(raw)
        except ValueError:
            return
        if not self.entrypoint and isinstance(e.get("entrypoint"), str):
            self.entrypoint = e["entrypoint"]
        if e.get("type") == "custom-title" and e.get("customTitle"):
            self.title = str(e["customTitle"])
        elif (text := human_prompt(e)) is not None:
            self.prompts += 1
            self.first_prompt = self.first_prompt or _first_line(text)
            self.last_prompt = _first_line(text)


_summaries: dict[str, _Summary] = {}
_cwds: dict[str, str] = {}


def _summary(path: Path, st: os.stat_result) -> _Summary:
    s = _summaries.get(str(path))
    if s is None or s.ino != st.st_ino or st.st_size < s.size:  # new, replaced or rewritten
        s = _Summary(ino=st.st_ino)
    if st.st_size > s.size:
        with path.open("rb") as f:
            f.seek(s.size)
            chunk = f.read(st.st_size - s.size)
        end = chunk.rfind(b"\n") + 1  # a line being written stays for the next read
        for raw in chunk[:end].splitlines():
            s.feed(raw)
        s.size += end
    _summaries[str(path)] = s
    return s


def project_for(cwd: str, roots: list[Path]) -> Path | None:
    """The registered project that holds cwd; the nearest one when they are nested."""
    p = Path(cwd)
    best = None
    for r in roots:
        if (p == r or r in p.parents) and (best is None or len(r.parts) > len(best.parts)):
            best = r
    return best


def scan(task_ids: set[str], live: dict[str, dict] | None) -> list[dict]:
    """The sessions of my projects that are not tasks, most recent first. Skips sessions
    without a prompt and scripted runs (`claude -p`)."""
    roots = projects.project_paths()
    base = claude_dir() / "projects"
    if not roots or not base.is_dir():
        return []
    out, seen = [], set()
    for f in base.glob("*/*.jsonl"):
        sid = f.stem
        key = str(f)
        if key not in _cwds:
            _cwds[key] = cwd_of(f)
        cwd = _cwds[key]
        root = project_for(cwd, roots) if cwd else None
        if root is None or sid in task_ids:
            continue
        try:
            st = f.stat()
            s = _summary(f, st)
        except OSError:
            continue
        seen.add(key)
        if not s.prompts or s.entrypoint == "sdk-cli":
            continue
        run = (live or {}).get(sid)
        out.append({
            "id": sid,
            "title": s.title or (run or {}).get("name") or s.first_prompt or "(untitled)",
            "project": root.name,
            "projectPath": str(root),
            "cwd": cwd,
            "updated": iso(st.st_mtime),
            "mtime": st.st_mtime,
            "prompts": s.prompts,
            "firstPrompt": s.first_prompt,
            "lastPrompt": s.last_prompt,
            "running": run,
        })  # fmt: skip
    for key in set(_summaries) - seen:  # deleted transcripts, sessions that became tasks, projects no longer registered
        del _summaries[key]
    out.sort(key=lambda o: -o["mtime"])
    return out


def find(session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    for f in (claude_dir() / "projects").glob(f"*/{session_id}.jsonl"):
        return f
    raise FileNotFoundError(f"no transcript for session {session_id}")


def adopt(session_id: str, prio: str = "working", run: dict | None = None) -> Task:
    """Make a session that started elsewhere a task: its turns become threads, the last one
    pending, so that the digest writes a title and a digest. `run`: its process, if one runs."""
    if prio not in PRIOS:
        raise ValueError(f"unknown prio {prio!r}")
    path = find(session_id)
    cwd = cwd_of(path)
    root = project_for(cwd, projects.project_paths()) if cwd else None
    if root is None:
        raise ValueError(f"session {short(session_id)} is not in a Brain Fry project (cwd {cwd or '?'})")
    title, threads, _ = read_turns(path)
    if threads:
        threads[-1].kind = "pending" if threads[-1].reply else "interrupted"
    now = iso()
    task = Task(
        session_ids=[session_id], project=str(root), cwd=cwd, title=title or ((run or {}).get("name") or ""),
        transcript_path=str(path), prio=prio,
        status="working" if run and run.get("status") == "busy" else "waiting",
        live=bool(run), created=threads[0].ts if threads else now, updated=now, threads=threads,
    )  # fmt: skip
    with taskfile.LOCK:
        if paths.task_path(root, session_id).exists():
            raise ValueError(f"session {short(session_id)} is already a task")
        taskfile.save(task)
    log.info("task %s %r adopted into %s, prio %s: %d thread(s) imported from %s%s", short(task.id),
             task.display_title[:50], root.name, prio, len(threads), path,
             f"; runs now in {run['where']} (pid {run['pid']}, {run['status']})" if run else "")  # fmt: skip
    return task
