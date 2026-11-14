"""The search of the explorer (2a): the names and the text of the files of a whole project, for
`fs.search` (app/core.py). See DESIGN_DETAILS.md "2a, 3a, 3b: files and terminals", "Explorer search".

The walk sees what the tree shows: the folders of `files.HIDDEN` stay hidden, a symlinked folder
is not followed. Names are the names of files, not of folders. The text of a file is searched as
a whole (one regular expression over all of it, not line by line: about 1 s for 28 MB), and only
for what the editor can open: not over `files.MAX_BYTES`, not binary (a NUL in the first 8192
bytes), not UTF-8 failing, not an image or a PDF (`files.VIEW_TYPES`).

The query is never logged: it is free text, like a prompt. Only counts and the duration are.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from . import files

log = logging.getLogger(__name__)

MAX_FILES = 2000  # files in one result; beyond, `more` is true
SNIFF = 8192  # a NUL in the first bytes: a binary file, as files.read_file

_LOCK = threading.Lock()
_LATEST: dict[str, int] = {}  # owner id --> the number of its newest search: an older one stops early
_SEQ = 0


def matcher(query: str, case: bool = False, word: bool = False, regex: bool = False) -> re.Pattern:
    """The pattern of a query: a plain substring unless `regex`, case ignored unless `case`,
    whole words as `\\b(?:...)\\b`. An invalid regular expression raises ValueError, with re's
    message (which does not hold the query)."""
    if not query:
        raise ValueError("nothing to search")
    src = query if regex else re.escape(query)
    if word:
        src = rf"\b(?:{src})\b"
    try:
        return re.compile(src, re.MULTILINE | (0 if case else re.IGNORECASE))
    except re.error as e:
        raise ValueError(f"Not a valid regular expression: {e}") from None


def _glob(g: str) -> re.Pattern:
    """One pattern of include / exclude, tested on the path relative to the project, case
    ignored (the disk of macOS): `*.py` a name, `tests/` a folder anywhere, `src/brain_fry` a
    piece of the path, `**` any depth, `?` one character, a leading `/` the project folder."""
    anchored = g.startswith("/")
    g = g.strip("/") if anchored else g
    folder = g.endswith("/")
    g = g.rstrip("/")
    out, i = [], 0
    while i < len(g):
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif g[i] == "*":
            out.append("[^/]*")
            i += 1
        elif g[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(g[i]))
            i += 1
    head = "^" if anchored else "(?:^|/)"
    return re.compile(head + "".join(out) + ("/" if folder else "(?:$|/)"), re.IGNORECASE)


def globs(text: str) -> list[re.Pattern]:
    """Comma-separated patterns (see _glob); blanks are ignored."""
    return [_glob(g) for g in (s.strip() for s in (text or "").split(",")) if g.strip("/")]


def _utf16(s: str) -> int:
    """The length of `s` in UTF-16 units, as JavaScript counts it (the editor is JS)."""
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


def _spans(rx: re.Pattern, name: str) -> list[list[int]]:
    """The matches in a name, in UTF-16 units: [[start, end], ...]. Empty matches do not count."""
    out = []
    for m in rx.finditer(name):
        if m.end() > m.start():
            a = _utf16(name[: m.start()])
            out.append([a, a + _utf16(m.group())])
    return out


def _text_of(path: str, rel: str, size: int) -> str | None:
    """The text that the editor would show, or None (too large, binary, not UTF-8, a viewer's file)."""
    if size > files.MAX_BYTES or files.view_kind(rel):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\x00" in data[:SNIFF]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _in_text(rx: re.Pattern, text: str) -> tuple[int, dict | None]:
    """The number of lines that hold a match, and where the first match is: {line (from 1),
    col, len} with col and len in UTF-16 units. One search per matching line: after a match,
    the next search starts at the next line."""
    count, first, pos, n = 0, None, 0, len(text)
    search = rx.search
    while pos <= n:
        m = search(text, pos)
        if not m:
            break
        s, e = m.span()
        if s == e:  # an empty match (`\b`, `x*`) is no match, as in VS Code
            pos = s + 1
            continue
        count += 1
        if first is None:
            start = text.rfind("\n", 0, s) + 1
            first = {"line": text.count("\n", 0, s) + 1, "col": _utf16(text[start:s]), "len": _utf16(m.group())}
        nl = text.find("\n", s)
        if nl < 0:
            break
        pos = nl + 1
    return count, first


def walk(project: str | Path):
    """The files of the project in the order of the tree (folders first, then by lowercase name,
    as files.list_dir), depth first: (relative path, path, size, symlink). Hidden folders and
    files are skipped, symlinked folders are not followed, an unreadable folder is skipped."""
    root = str(Path(project).resolve())

    def scan(folder: str, rel: str):
        try:
            with os.scandir(folder) as it:
                entries = []
                for e in it:
                    if e.name in files.HIDDEN:
                        continue
                    try:
                        is_dir = e.is_dir()  # as the tree: a link to a folder shows as a folder
                    except OSError:
                        continue
                    entries.append((not is_dir, e.name.lower(), e, is_dir))
        except OSError:
            return
        entries.sort(key=lambda x: (x[0], x[1]))
        for _, _, e, is_dir in entries:
            path = f"{rel}/{e.name}" if rel else e.name
            if is_dir:
                if not e.is_symlink():
                    yield ("dir", path, e.path, 0, False)
                continue
            try:
                link = e.is_symlink()
                size = e.stat().st_size if e.is_file() else -1
            except OSError:
                continue
            if size >= 0:
                yield ("file", path, e.path, size, link)

    stack = [scan(root, "")]
    while stack:
        try:
            kind, rel, path, size, link = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if kind == "dir":
            pruned = yield ("dir", rel, path, 0, False)
            if not pruned:
                stack.append(scan(path, rel))
        else:
            yield ("file", rel, path, size, link)


def search(project: str | Path, query: str, *, case: bool = False, word: bool = False, regex: bool = False,
           names: bool = True, text: bool = True, include: str = "", exclude: str = "",
           limit: int = MAX_FILES, cancelled=None) -> dict:  # fmt: skip
    """The files that match, in the order of the tree: {"files": [{"path", "name": [[start, end],
    ...] (the matches in the name, UTF-16 units; empty: the name does not match), "count" (lines
    that hold a match, 0: none), "line", "col", "len" (the first match in the text, if any)}],
    "more": more than `limit` files match, "searched": files looked at, "cancelled"}.
    `cancelled()`: true when a newer search makes this one useless; it stops at the next file."""
    rx = matcher(query, case, word, regex)
    incs, excs = globs(include), globs(exclude)
    root = str(Path(project).resolve())
    out, more, searched, stopped = [], False, 0, False
    gen = walk(project)
    send = None
    while True:
        try:
            kind, rel, path, size, link = gen.send(send)
        except StopIteration:
            break
        send = None
        if kind == "dir":
            send = any(r.search(rel + "/") for r in excs)  # an excluded folder: not walked at all
            continue
        if cancelled and cancelled():
            stopped = True
            break
        if incs and not any(r.search(rel) for r in incs):
            continue
        if any(r.search(rel) for r in excs):
            continue
        searched += 1
        name = rel.rsplit("/", 1)[-1]
        spans = _spans(rx, name) if names else []
        count, first = 0, None
        if text:
            body = None
            # a link is read only when its target is in the project: the editor opens only those
            if not link or os.path.realpath(path).startswith(root + os.sep):
                body = _text_of(path, rel, size)
            if body is not None:
                count, first = _in_text(rx, body)
        if not spans and not count:
            continue
        if len(out) >= limit:
            more = True
            break
        hit = {"path": rel, "name": spans, "count": count}
        if first:
            hit.update(first)
        out.append(hit)
    return {"files": out, "more": more, "searched": searched, "cancelled": stopped}


def command(project_of, tid: str, msg: dict) -> dict:
    """`fs.search` (blocking: core.py runs it in a thread, beside the other commands of the
    connection, server.CONCURRENT). `project_of(tid)`: the project folder. The query is `text`,
    which core's log line hides; this function logs counts and the duration only. A newer search
    of the same owner stops this one. Every failure is raised as one of the errors that server.py
    logs without the raw message (it holds the query)."""
    global _SEQ
    t0 = time.monotonic()
    with _LOCK:
        _SEQ += 1
        me = _SEQ
        _LATEST[tid] = me
    try:
        try:
            project = project_of(tid)
        except KeyError as e:  # actions.NotFound: an unknown task, a project that is not registered
            raise PermissionError(f"not a project of Brain Fry: {e.args[0] if e.args else tid}") from None
        r = search(project, str(msg.get("text") or ""), case=bool(msg.get("case")), word=bool(msg.get("word")),
                   regex=bool(msg.get("regex")), names=msg.get("names", True) is not False,
                   text=msg.get("content", True) is not False, include=str(msg.get("include") or ""),
                   exclude=str(msg.get("exclude") or ""), cancelled=lambda: _LATEST.get(tid) != me)  # fmt: skip
    except (ValueError, PermissionError, FileNotFoundError, NotADirectoryError):
        raise
    except Exception as e:
        log.exception("search failed")  # the traceback, never the query
        raise ValueError(f"The search failed: {type(e).__name__}") from None
    finally:
        with _LOCK:
            if _LATEST.get(tid) == me:
                del _LATEST[tid]
    texts = sum(1 for f in r["files"] if f["count"])
    log.info("search in %s: %d files (%d by their text%s), %d searched, %.0f ms%s", Path(project).name, len(r["files"]),
             texts, ", more" if r["more"] else "", r["searched"], (time.monotonic() - t0) * 1000,
             ", stopped by a newer search" if r["cancelled"] else "")  # fmt: skip
    return {"files": r["files"], "more": r["more"]}
