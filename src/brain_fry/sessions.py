"""Claude Code's own files. See DESIGN_DETAILS.md, "Claude Code's process
files and transcripts".

- ~/.claude/sessions/<pid>.json: one file per running Claude Code process (session id,
  entrypoint, name, status idle, busy or waiting (a dialog waits for me), and when the
  status last changed). Tells which sessions run now, where, and when a dialog closed.
- ~/.claude/projects/<slug>/<session id>.jsonl: one transcript per session. Read for the
  earlier turns of a session that becomes a task in the middle (events._import_history).

Both formats are internal to Claude Code and may change: everything here is best effort.
CLAUDE_CONFIG_DIR moves ~/.claude, as it does for Claude Code itself.
"""

from __future__ import annotations

import json
import os
import re
from typing import NamedTuple
from datetime import datetime
from pathlib import Path

from .taskfile import Thread
from .util import iso

WHERE = {"claude-vscode": "VS Code", "cli": "a terminal", "sdk-cli": "a script"}
# Text blocks that Claude Code adds to a prompt (IDE context, image captions)
ADDED_BLOCKS = ("<ide_", "<system-reminder>", "[Image")
# Messages that Claude Code writes in my name. Only needed for transcripts that do not
# have `origin` yet (before about 2026-10).
NOT_TYPED = ("<local-command", "<system-reminder>", "[Request interrupted", "This session is being continued",
             "Base directory for this skill", "Your response above was cut off", "Caveat:")  # fmt: skip
COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)


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


def _local(ts: str | None) -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return iso()


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
