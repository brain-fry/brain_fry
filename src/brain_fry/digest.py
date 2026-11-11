"""The digest: Sonnet via `claude -p` turns pending threads into inbox entries.
See DESIGN_DETAILS.md, "Digest".

One call per task covers all its pending threads (normally one). The call runs without
the task-file lock; the task is reloaded afterwards, because events may have changed it
in the meantime.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from importlib import resources
from pathlib import Path

from . import actions, paths, taskfile
from .taskfile import Task
from .logs import short
from .util import child_env, claude_bin

log = logging.getLogger(__name__)

KINDS = ["review", "answer", "rule", "FYI", "wait", "blocked"]
WAIT = ("wait: Claude waits for something other than the owner (a background command, another agent or "
        "session, a timer) and will go on by itself when it comes; nothing is needed from the owner now. "
        "Use it rather than FYI when the reply says that Claude will go on by itself.")  # fmt: skip
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "short_title": {"type": "string"},
        "digest": {"type": "string"},
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    # described here too, so that an inbox_prompt.md older than `wait` still gets it
                    "kind": {"type": "string", "enum": KINDS, "description": "See the instructions. " + WAIT},
                    "digest": {"type": "string"},
                    "question": {"type": ["string", "null"]},
                },
                "required": ["n", "kind", "digest", "question"],
            },
        },
    },
    "required": ["title", "short_title", "digest", "threads"],
}
MAX_PROMPT, MAX_REPLY, MAX_TOOLS = 4_000, 8_000, 40


class DigestError(Exception):
    pass


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit // 2] + "\n[...cut...]\n" + text[-limit // 2 :]


def _describe(name: str, inp: dict) -> str:
    for key in ("file_path", "notebook_path", "path", "pattern", "url", "command", "description"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name} {value.strip().splitlines()[0][:100]}"
    return name


def tools_of_last_turn(transcript_path: str) -> list[str]:
    """Tool names and their main argument, from the last real prompt on. Best effort:
    the transcript format is internal to Claude Code, and the file may be gone."""
    try:
        lines = Path(transcript_path).read_text().splitlines()
    except (OSError, ValueError):
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    start = 0
    for i, e in enumerate(entries):
        content = (e.get("message") or {}).get("content") if e.get("type") == "user" else None
        if isinstance(content, str) or (
            isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        ):
            start = i
    out: list[str] = []
    for e in entries[start:]:
        if e.get("type") != "assistant":
            continue
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                d = _describe(b.get("name", "?"), b.get("input") or {})
                if d not in out:
                    out.append(d)
    return out[:MAX_TOOLS]


def build_prompt(task: Task, instructions: str) -> tuple[str, list[int]]:
    pending = [t for t in task.threads if t.kind == "pending"]
    parts = [instructions.strip(), "", "# Current digest", "", task.digest or "(none yet)", "", "# Turns"]
    for t in pending:
        parts += ["", f"## Turn {t.n}", "", "Prompt:", _cut(t.prompt, MAX_PROMPT) or "(no prompt: the turn started on its own)"]
        if t is pending[-1] and (tools := tools_of_last_turn(task.transcript_path)):
            parts += ["", "Tools used:"] + [f"- {x}" for x in tools]
        parts += ["", "Reply:", _cut(t.reply, MAX_REPLY) or "(empty)"]
    return "\n".join(parts) + "\n", [t.n for t in pending]


def call_sonnet(prompt: str, timeout: float = 240) -> dict:
    cmd = [
        claude_bin(), "-p", "--model", "sonnet",
        "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
        "--no-session-persistence",
        "--settings", json.dumps({"disableAllHooks": True}),
    ]  # fmt: skip
    cwd = paths.bf_home()  # avoids loading a project's CLAUDE.md
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
            cwd=cwd, env=child_env(BRAIN_FRY_INTERNAL="1"),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DigestError(str(e)) from e
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise DigestError(f"exit {r.returncode}: {(r.stderr or r.stdout)[-500:]}") from e
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise DigestError(str(out.get("result") or out.get("subtype") or out)[:500])
    return out["structured_output"]


def apply_result(task: Task, result: dict, sent: list[int]) -> None:
    if not task.title and result.get("title", "").strip():
        task.title = result["title"].strip()
    if not task.short_title:  # set once, as the title
        task.short_title = short_title_of(result, task)
    if result.get("digest", "").strip():
        task.digest = result["digest"].strip()
    by_n = {r.get("n"): r for r in result.get("threads") or []}
    if len(sent) == 1 and sent[0] not in by_n and len(by_n) == 1:
        by_n = {sent[0]: next(iter(by_n.values()))}  # the model got the number wrong
    for t in task.threads:
        if t.n in sent and t.kind == "pending":
            r = by_n.get(t.n)
            if r is None:
                t.kind = "FYI"  # never loop on a thread that the model skipped
                continue
            t.kind = r.get("kind") if r.get("kind") in KINDS else "FYI"
            t.digest = (r.get("digest") or "").strip()
            t.question = (r.get("question") or "").strip() or None
    last = task.threads[-1] if task.threads else None
    if task.held and last and last.n in sent:  # the reply of a held Stop: inbox or not
        actions.settle_hold(task, waits=last.kind == "wait")


def short_title_of(result: dict, task: Task) -> str:
    """Sonnet's short title, else the first three words of the title: never empty."""
    return (result.get("short_title") or "").strip() or " ".join(task.display_title.split()[:3])


def needs_call(task: Task) -> bool:
    """Pending threads."""
    return any(th.kind == "pending" for th in task.threads)


def pending_tasks(project: Path) -> list[Task]:
    return [t for t in taskfile.all_tasks(project) if needs_call(t)]


def digest_task(task: Task, call=call_sonnet) -> Task:
    instructions = paths.inbox_prompt_file()
    if instructions.exists():
        text = instructions.read_text()
    else:
        text = resources.files("brain_fry").joinpath("inbox_prompt.md").read_text()
    prompt, sent = build_prompt(task, text)
    log.info("digest %s %r: sending thread(s) %s, %d chars", short(task.id), task.display_title[:50], sent, len(prompt))
    t0 = time.monotonic()
    try:
        result = call(prompt)
    except DigestError as e:
        log.warning("digest %s failed after %.1f s: %s", short(task.id), time.monotonic() - t0, e)
        _release(task)
        raise
    with taskfile.LOCK:
        fresh = taskfile.load(paths.task_path(Path(task.project), task.id))
        held = fresh.held
        apply_result(fresh, result, sent)
        taskfile.save(fresh)
    kinds = {t.n: t.kind for t in fresh.threads if t.n in sent}
    log.info("digest %s done in %.1f s: kinds %s, title %r, short title %r", short(task.id), time.monotonic() - t0,
             kinds, fresh.display_title[:50], fresh.short_title[:40])  # fmt: skip
    if held and not fresh.held:
        log.info("task %s: held reply settled: kind %s, inbox %s, status %s", short(task.id),
                 fresh.threads[-1].kind, fresh.inbox, fresh.status)  # fmt: skip
    return fresh


def _release(task: Task) -> None:
    """The digest failed: a held reply goes to the inbox all the same (it must never wait
    for a digest that does not come)."""
    with taskfile.LOCK:
        fresh = taskfile.load(paths.task_path(Path(task.project), task.id))
        if fresh.held:
            actions.settle_hold(fresh, waits=False)
            taskfile.save(fresh)
            log.info("task %s: the digest failed: the held reply goes to the inbox", short(task.id))


def digest_project(project: Path, call=call_sonnet) -> tuple[list[str], list[str]]:
    """Digest every task that needs it (`needs_call`). Returns (changed ids, error messages)."""
    changed, errors = [], []
    for task in pending_tasks(project):
        try:
            digest_task(task, call)
            changed.append(task.id)
        except DigestError as e:
            errors.append(f"{task.id[:8]}: {e}")
    return changed, errors
