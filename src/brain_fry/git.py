"""Git for Review mode (DESIGN_DETAILS.md, "Review mode"): the worktrees, branches, status, log
with its graph, diffs, the staging of files, hunks and lines, discard with an undo, and commits
of the repository that holds a task's folder.

Every call goes through the git CLI, so that it behaves as the git that agents and Renaud run
(worktrees, hooks, config, the index lock). `GIT_OPTIONAL_LOCKS=0` on every call: without it,
`git status` refreshes the index and holds index.lock for a moment, and an agent's `git add` or
`git commit` at that moment fails with "index.lock exists".
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from . import files
from .util import child_env

log = logging.getLogger(__name__)

TIMEOUT = 30  # s
COMMIT_TIMEOUT = 60  # s: a signing key that waits for a passphrase must not hang the app
LOG_PAGE = 1000  # commits loaded at a time
MAX_UNTRACKED = 1000  # untracked files listed one by one; beyond that, by folder
MAX_DIFF_LINES = 5000  # lines of a diff shown before "Show all"
MAX_TEXT = 2 * 2**20  # an untracked file is shown in full up to 2 MB, as the editor of 3a opens it
LOCK_RETRIES = 5  # x 0.2 s, when an agent runs git and holds index.lock
COUNTS_EVERY = 10  # s: the numbers of changed files of the other worktrees (panel 1)
# user config that would change the output that we parse
CONFIG = ["-c", "core.quotepath=off", "-c", "color.ui=false", "-c", "diff.noprefix=false",
          "-c", "diff.mnemonicPrefix=false", "-c", "diff.relative=false", "-c", "log.showSignature=false",
          "-c", "core.pager=cat", "-c", "status.relativePaths=false"]  # fmt: skip
SHA = re.compile(r"[0-9a-f]{4,64}")
HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
WHOLE_FILE = (b"new file mode", b"deleted file mode", b"rename from", b"copy from", b"Binary files",
              b"GIT binary patch", b"Subproject commit", b"similarity index")  # fmt: skip


class GitError(Exception):
    pass


class Stale(GitError):
    """The hunk on screen is not in the diff any more: the file changed since it was shown."""


class NotRepo(GitError):
    pass


def _env() -> dict[str, str]:
    return child_env(GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true", GIT_PAGER="cat", GIT_LITERAL_PATHSPECS="1",
                     GIT_OPTIONAL_LOCKS="0", LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8")  # fmt: skip


def run(tree: str | Path, *args: str, stdin: bytes = b"", timeout: float = TIMEOUT, ok: tuple[int, ...] = (0,)) -> bytes:
    """git in `tree`; its stdout. A failure raises GitError with git's messages (for a commit,
    the output of the hooks too). Retries for about 1 s while index.lock is held by another git."""
    cmd = ["git", "-C", str(tree), *CONFIG, *args]
    for attempt in range(LOCK_RETRIES + 1):
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout, env=_env())
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {args[0]} took more than {timeout:.0f} s and was stopped") from e
        if p.returncode in ok:
            return p.stdout
        err = "\n".join(x.decode("utf-8", "replace").strip() for x in (p.stderr, p.stdout) if x.strip())
        if "index.lock" in err and attempt < LOCK_RETRIES:
            time.sleep(0.2)
            continue
        if "index.lock" in err:
            err = "Another git process holds the index (index.lock): an agent may be running git. Try again."
        raise GitError(err or f"git {args[0]} failed with code {p.returncode}")
    raise AssertionError("unreachable")


def _s(b: bytes) -> str:
    """Paths and names: bytes that are not UTF-8 survive the round trip back to git."""
    return b.decode("utf-8", "surrogateescape")


def _rel(path: str) -> str:
    """A path of the repository, as the front end sends it back: relative, inside the tree."""
    p = str(path)
    if not p or "\0" in p or os.path.isabs(p):
        raise ValueError(f"not a path of the repository: {p!r}")
    norm = os.path.normpath(p)
    if norm == ".." or norm.startswith("../"):
        raise ValueError(f"not a path of the repository: {p!r}")
    return p


# ------------------------------------------------------------------ the repository and its worktrees


def toplevel(path: str | Path) -> Path | None:
    try:
        return Path(_s(run(path, "rev-parse", "--show-toplevel")).strip())
    except (GitError, OSError):
        return None


def worktrees(top: Path) -> list[dict]:
    """`git worktree list`: the main tree first. Bare entries are left out."""
    out = run(top, "worktree", "list", "--porcelain", "-z")
    trees: list[dict] = []
    cur: dict | None = None
    for field in out.split(b"\0"):
        if not field:
            if cur:
                trees.append(cur)
            cur = None
            continue
        key, _, val = _s(field).partition(" ")
        if key == "worktree":
            cur = {"path": val, "head": None, "branch": None, "detached": False, "prunable": False, "bare": False}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = None if set(val) <= {"0"} else val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key in ("detached", "bare", "prunable"):
            cur[key] = True
    if cur:
        trees.append(cur)
    for i, t in enumerate(trees):
        t["name"] = "main" if i == 0 else Path(t["path"]).name
    return [t for t in trees if not t["bare"]]


def _same(a: str | Path, b: str | Path) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def _inside(child: str | Path, parent: str | Path) -> bool:
    c, p = Path(child).resolve(), Path(parent).resolve()
    return c == p or p in c.parents


def pick(cwd: str | Path, want: str | None = None) -> tuple[Path, list[dict], dict]:
    """(top, worktrees, the current one): `want` if it is a live worktree of the repository of
    `cwd`, else the worktree that holds `cwd`."""
    top = toplevel(cwd)
    if top is None:
        raise NotRepo(f"{cwd} is not in a git repository")
    trees = worktrees(top)
    if want:
        for t in trees:
            if _same(t["path"], want) and not t["prunable"]:
                return top, trees, t
    holding = [t for t in trees if not t["prunable"] and _inside(cwd, t["path"])]
    holding.sort(key=lambda t: -len(Path(t["path"]).resolve().parts))  # the deepest: a worktree inside the main tree
    if holding:
        return top, trees, holding[0]
    return top, trees, next((t for t in trees if _same(t["path"], top)), trees[0])


def _git_dir(tree: Path) -> Path:
    return Path(_s(run(tree, "rev-parse", "--absolute-git-dir")).strip())


def operation(tree: Path) -> str | None:
    """A merge, rebase, cherry-pick or revert in progress in this worktree."""
    gd = _git_dir(tree)
    if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        return "rebase"
    for f, name in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
        if (gd / f).exists():
            return name
    return None


# ------------------------------------------------------------------ status


def status(tree: Path) -> dict:
    """The staged, unstaged and conflicted files of a worktree (`--porcelain=v2`)."""
    out = run(tree, "status", "--porcelain=v2", "-z", "-uall")
    st = _parse_status(out)
    if sum(1 for f in st["unstaged"] if f["st"] == "?") > MAX_UNTRACKED:  # by folder (-unormal)
        folded = _parse_status(run(tree, "status", "--porcelain=v2", "-z", "-unormal"))
        st["unstaged"] = [f for f in st["unstaged"] if f["st"] != "?"] + [f for f in folded["unstaged"] if f["st"] == "?"]
    return st


def _parse_status(out: bytes) -> dict:
    staged, unstaged, conflicts = [], [], []
    recs = out.split(b"\0")
    i = 0
    while i < len(recs):
        r = _s(recs[i])
        i += 1
        if not r:
            continue
        if r[0] == "1":
            _, xy, _sub, *_rest, path = r.split(" ", 8)
            _add(staged, unstaged, xy, path, None)
        elif r[0] == "2":
            _, xy, _sub, *_rest, path = r.split(" ", 9)
            orig = _s(recs[i])
            i += 1
            _add(staged, unstaged, xy, path, orig)
        elif r[0] == "u":
            _, _xy, _sub, *_rest, path = r.split(" ", 10)
            conflicts.append({"path": path, "st": "U"})
        elif r[0] == "?":
            unstaged.append({"path": r[2:], "st": "?"})
    by_path = lambda f: f["path"]  # noqa: E731
    return {"staged": sorted(staged, key=by_path), "unstaged": sorted(unstaged, key=by_path),
            "conflicts": sorted(conflicts, key=by_path)}  # fmt: skip


def _add(staged: list, unstaged: list, xy: str, path: str, orig: str | None) -> None:
    x, y = xy[0], xy[1]
    if x != ".":
        staged.append({"path": path, "orig": orig if x in "RC" else None, "st": x})
    if y != ".":
        unstaged.append({"path": path, "st": y})


_counts: dict[str, tuple[float, int]] = {}


def changed_count(tree: str, fresh: dict | None = None) -> int | None:
    """The number of changed files of a worktree, at most every COUNTS_EVERY s per tree."""
    if fresh is not None:
        n = len({f["path"] for k in ("staged", "unstaged", "conflicts") for f in fresh[k]})
        _counts[tree] = (time.time(), n)
        return n
    at, n = _counts.get(tree, (0.0, -1))
    if time.time() - at < COUNTS_EVERY and n >= 0:
        return n
    try:
        n = len({f["path"] for k in ("staged", "unstaged", "conflicts") for f in status(Path(tree))[k]})
    except GitError:
        return None
    _counts[tree] = (time.time(), n)
    return n


# ------------------------------------------------------------------ branches and refs


def branches(top: Path) -> list[dict]:
    fmt = "%(refname:short)%00%(upstream:track,nobracket)%00%(worktreepath)"
    out = []
    for line in _s(run(top, "for-each-ref", f"--format={fmt}", "--sort=refname", "refs/heads")).splitlines():
        name, track, wt = (line.split("\0") + ["", ""])[:3]
        out.append({"name": name, "track": track, "worktree": wt or None})
    return out


def refs(top: Path) -> list[tuple[str, str, str]]:
    """(commit, kind, name) for local and remote branches and tags; an annotated tag is peeled."""
    fmt = "%(objectname)%00%(*objectname)%00%(refname)"
    out = []
    for line in _s(run(top, "for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes", "refs/tags")).splitlines():
        oid, peeled, ref = (line.split("\0") + ["", ""])[:3]
        if ref.startswith("refs/heads/"):
            out.append((oid, "local", ref[len("refs/heads/"):]))
        elif ref.startswith("refs/remotes/"):
            if not ref.endswith("/HEAD"):
                out.append((oid, "remote", ref[len("refs/remotes/"):]))
        elif ref.startswith("refs/tags/"):
            out.append((peeled or oid, "tag", ref[len("refs/tags/"):]))
    return out


def refs_sig(top: Path, trees: list[dict]) -> str:
    """Changes when a branch, a tag or the HEAD of a worktree moves: the log must reload."""
    raw = repr((refs(top), [(t["path"], t["head"], t["branch"]) for t in trees]))
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()[:16]


# ------------------------------------------------------------------ the log and its graph


def log_rows(top: Path, trees: list[dict], cur: dict, all_branches: bool = True, count: int = LOG_PAGE) -> dict:
    """The first `count` commits, newest first in date order, after a first row for the
    uncommitted changes of the current worktree; each row with its lanes (see `lanes`)."""
    count = min(int(count), 20 * LOG_PAGE)
    heads = [t["head"] for t in trees if t["head"] and not t["prunable"]]
    rows: list[dict] = []
    if cur["head"]:
        revs = ["--branches", "--remotes", *heads] if all_branches else [cur["head"]]
        fmt = "%H%x1f%P%x1f%an%x1f%at%x1f%s"
        out = run(top, "log", "--date-order", "-z", f"-n{int(count)}", f"--format={fmt}", *revs, "--")
        for rec in out.split(b"\0"):
            if not rec.strip():
                continue
            h, p, a, t, s = (_s(rec).strip("\n").split("\x1f") + [""] * 5)[:5]
            rows.append({"h": h, "p": p.split() if p else [], "a": a, "t": int(t or 0), "s": s})
    more = len(rows) >= count
    labels: dict[str, list[dict]] = {}
    for oid, kind, name in refs(top):
        labels.setdefault(oid, []).append({"n": name, "k": kind, "head": kind == "local" and name == cur["branch"]})
    for t in trees:
        if t["head"]:
            labels.setdefault(t["head"], []).append({"n": f"wt: {t['name']}", "k": "wtgone" if t["prunable"] else "wt"})
    for r in rows:
        r["refs"] = labels.get(r["h"], [])
    wip = {"h": "wip", "p": [cur["head"]] if cur["head"] else [], "a": "", "t": 0, "s": "Uncommitted changes", "refs": []}
    rows = [wip, *rows]
    lanes(rows)
    return {"rows": rows, "more": more}


def lanes(rows: list[dict]) -> None:
    """Adds to each row its lane (`lane`), its color and the segments that its cell of the graph
    draws (`segs`: [x1, y1, x2, y2, color, dashed], x in lanes, y 0 top, 1 middle, 2 bottom).
    A lane is a line of descent that waits for the commit `active[i]`; a commit continues the
    lowest lane that waits for it (the others end in it), and its first parent continues that
    lane; another parent joins a lane that already waits for it, or opens one. Lanes are never
    shifted, so that lines stay straight; trailing free lanes are dropped. The first row (the
    uncommitted changes) draws a dashed line down to the HEAD commit."""
    active: list[str | None] = []
    colors: list[int] = []
    dashed: list[bool] = []
    next_color = 0

    def free_slot() -> int:
        for j, s in enumerate(active):
            if s is None:
                return j
        active.append(None)
        colors.append(0)
        dashed.append(False)
        return len(active) - 1

    for r in rows:
        h = r["h"]
        waiting = [j for j, s in enumerate(active) if s == h]
        if waiting:
            idx = waiting[0]
        else:
            idx = free_slot()
            colors[idx] = next_color
            next_color += 1
        before = list(active)
        segs = []
        for j, s in enumerate(before):
            if s is None:
                continue
            if s == h:
                segs.append([j, 0, idx, 1, colors[j], dashed[j]])  # into the node
            else:
                segs.append([j, 0, j, 2, colors[j], dashed[j]])  # passes by
        for j in waiting:
            active[j] = None
            dashed[j] = False
        node_color = colors[idx]
        is_wip = h == "wip"
        for n, p in enumerate(r["p"]):
            if n == 0:  # the first parent continues the lane, even if another lane waits for it too:
                k = idx  # both end in it, and the line of the first parents stays straight
                active[k] = p
                colors[k] = node_color
                dashed[k] = is_wip
            elif p in active:  # another lane already waits for it: join that lane
                k = active.index(p)
            else:
                k = free_slot()
                active[k] = p
                colors[k] = next_color
                next_color += 1
                dashed[k] = False
            segs.append([idx, 1, k, 2, colors[k], is_wip])
        while active and active[-1] is None:
            active.pop()
            colors.pop()
            dashed.pop()
        r["lane"] = idx
        r["color"] = node_color
        r["segs"] = segs
        r["w"] = max(len(before), len(active), idx + 1)


def commit_info(top: Path, sha: str) -> dict:
    """The message, parents, author and changed files of one commit (against its first parent)."""
    if not SHA.fullmatch(sha or ""):
        raise ValueError(f"not a commit: {sha!r}")
    out = _s(run(top, "show", "-s", "--format=%H%x00%P%x00%an%x00%at%x00%B", sha))
    h, p, an, at, body = (out.split("\0") + [""] * 5)[:5]
    parents = p.split()
    args = ["diff-tree", "-r", "-z", "-M", "--name-status", "--no-commit-id"]
    args += [f"{h}^1", h] if parents else ["--root", h]
    recs = run(top, *args).split(b"\0")
    fl = []
    i = 0
    while i < len(recs):
        code = _s(recs[i])
        i += 1
        if not code:
            continue
        if code[0] in "RC":
            fl.append({"st": code[0], "orig": _s(recs[i]), "path": _s(recs[i + 1])})
            i += 2
        else:
            fl.append({"st": code[0], "path": _s(recs[i])})
            i += 1
    return {"h": h, "parents": parents, "author": an, "t": int(at or 0), "message": body.strip("\n"), "files": fl}


# ------------------------------------------------------------------ diffs


def raw_diff(tree: Path, side: str, path: str, orig: str | None = None, sha: str | None = None) -> bytes:
    """The unified diff of one file. side: unstaged (index --> work tree), untracked (empty -->
    work tree), staged (HEAD --> index), conflict, or commit (first parent --> `sha`)."""
    path = _rel(path)
    opts = ["--no-ext-diff", "--no-color", "-U3"]
    if side in ("unstaged", "conflict"):
        return run(tree, "diff", *opts, "--", path)
    if side == "untracked":
        return run(tree, "diff", "--no-index", *opts, "--", "/dev/null", path, ok=(0, 1))
    if side == "staged":
        return run(tree, "diff", "--cached", "-M", *opts, "--", *([_rel(orig)] if orig else []), path)
    if side == "commit":
        if not SHA.fullmatch(sha or ""):
            raise ValueError(f"not a commit: {sha!r}")
        parents = _s(run(tree, "rev-list", "--parents", "-n1", sha)).split()[1:]
        revs = [f"{sha}^1", sha] if parents else ["--root", sha]
        return run(tree, "diff-tree", "-p", "-M", "--no-commit-id", *opts, *revs, "--", *([_rel(orig)] if orig else []), path)
    raise ValueError(f"unknown side {side!r}")


def split(raw: bytes) -> tuple[list[bytes], list[list[bytes]]]:
    """(the file's header lines, its hunks: each a list of lines, the @@ line first)."""
    header: list[bytes] = []
    hunks: list[list[bytes]] = []
    for ln in raw.splitlines(keepends=True):
        if ln.startswith(b"@@"):
            hunks.append([ln])
        elif hunks:
            hunks[-1].append(ln)
        else:
            header.append(ln)
    return header, hunks


def digest(hunk: list[bytes]) -> str:
    """Identifies a hunk by its lines, not by its position, which moves when the file changes
    above it."""
    return hashlib.sha1(b"".join(hunk[1:])).hexdigest()[:16]


def diff(tree: Path, side: str, path: str, orig: str | None = None, sha: str | None = None, full: bool = False) -> dict:
    """A diff for 3b: the hunks with their lines ([kind, old number, new number, text]), whether
    hunks and lines can be staged, and a signature that changes with the diff."""
    if side == "untracked":
        p = Path(tree) / _rel(path)
        if p.is_dir():
            return {"sig": f"dir:{path}", "folder": True, "hunks": [], "hunkable": False}
        if p.is_file() and p.stat().st_size > MAX_TEXT:
            return {"sig": f"big:{p.stat().st_size}", "tooBig": True, "hunks": [], "hunkable": False}
    raw = raw_diff(tree, side, path, orig, sha)
    header, hunks = split(raw)
    head = b"".join(header)
    info = {
        "binary": b"Binary files" in head or b"GIT binary patch" in head,
    }
    whole = any(m in head for m in WHOLE_FILE)
    hunkable = side in ("unstaged", "staged") and not whole and bool(hunks)
    out, shown = [], 0
    for h in hunks:
        lines = _lines(h)
        shown += len(lines)
        if not full and shown > MAX_DIFF_LINES and out:
            break
        out.append({"header": _s(h[0]).rstrip("\n"), "digest": digest(h), "lines": lines})
    total = sum(len(h) - 1 for h in hunks)
    return {"sig": hashlib.sha1(raw).hexdigest()[:16], "hunks": out, "hunkable": hunkable, "info": info,
            "truncated": len(out) < len(hunks), "total": total, "header": [_s(x).rstrip("\n") for x in header]}  # fmt: skip


def _lines(hunk: list[bytes]) -> list[list]:
    m = HUNK.match(hunk[0])
    old = int(m.group(1)) if m else 0
    new = int(m.group(3)) if m else 0
    out = []
    for ln in hunk[1:]:
        c = ln[:1].decode("ascii", "replace") or " "
        text = ln[1:].rstrip(b"\n").rstrip(b"\r").decode("utf-8", "replace")
        if c == "\\":
            out.append(["\\", "", "", text.strip()])
        elif c == "-":
            out.append(["-", old, "", text])
            old += 1
        elif c == "+":
            out.append(["+", "", new, text])
            new += 1
        else:
            out.append([" ", old, new, text])
            old += 1
            new += 1
    return out


# ------------------------------------------------------------------ patches: hunks and lines


def patch_of(header: list[bytes], hunk: list[bytes], picked: set[int] | None, reverse: bool) -> bytes:
    """A patch of one hunk, or of the lines `picked` in it (indexes of its lines after the @@
    line), as git gui builds it. Forward (stage): an unpicked - line becomes context and an
    unpicked + line is dropped. Reverse (unstage, discard): an unpicked + line becomes context
    and an unpicked - line is dropped. Applied with --recount, so the counts of the @@ line do
    not matter. Only the lines that name the file are kept from the header: a change of mode
    is not part of a hunk."""
    head = [ln for ln in header if ln.startswith((b"diff --git ", b"--- ", b"+++ "))]
    body = hunk[1:]
    if picked is not None:
        if not any(i in picked and body[i][:1] in (b"+", b"-") for i in range(len(body))):
            raise ValueError("no changed line is selected")
        out, kept = [], True
        for i, ln in enumerate(body):
            c = ln[:1]
            if c == b"\\":  # "No newline at end of file" belongs to the line before it
                if kept:
                    out.append(ln)
                continue
            if c not in (b"+", b"-") or i in picked:
                out.append(ln)
                kept = True
            elif (c == b"-") != reverse:
                out.append(b" " + ln[1:])
                kept = True
            else:
                kept = False
        body = out
    return b"".join([*head, hunk[0], *body])


def _find(tree: Path, side: str, path: str, dig: str) -> tuple[list[bytes], list[bytes]]:
    header, hunks = split(raw_diff(tree, side, path))
    for h in hunks:
        if digest(h) == dig:
            return header, h
    raise Stale(f"{path} changed since its diff was shown: the diff is reloaded")


def apply_hunk(tree: Path, project: str | Path, action: str, path: str, dig: str, lines: list[int] | None = None) -> dict:
    """Stage, unstage or discard one hunk (or some of its lines) of the diff on screen. The diff
    is computed again and the hunk found by its lines: if it changed, nothing is applied."""
    side = "staged" if action == "unstage" else "unstaged"
    header, hunk = _find(tree, side, path, dig)
    patch = patch_of(header, hunk, set(map(int, lines)) if lines else None, reverse=action != "stage")
    opts = ["apply", "--recount", "--whitespace=nowarn"]
    if action == "stage":
        run(tree, *opts, "--cached", "-", stdin=patch)
        return {}
    if action == "unstage":
        run(tree, *opts, "--cached", "-R", "-", stdin=patch)
        return {}
    if action == "discard":
        run(tree, *opts, "-R", "--check", "-", stdin=patch)  # fails before the copy is saved
        name = _save_discarded(project, path, patch)
        run(tree, *opts, "-R", "-", stdin=patch)
        return {"undo": name}
    raise ValueError(f"unknown action {action!r}")


def stage_files(tree: Path, paths: list[str]) -> None:
    run(tree, "add", "-A", "--", *map(_rel, paths))


def unstage_files(tree: Path, paths: list[str], has_head: bool) -> None:
    if has_head:
        run(tree, "restore", "--staged", "--", *map(_rel, paths))
    else:  # no commit yet: nothing to restore from
        run(tree, "rm", "-r", "-q", "--cached", "--", *map(_rel, paths))


def discard_file(tree: Path, project: str | Path, path: str, untracked: bool) -> dict:
    """The work tree's changes of a file go back to the index. A copy of what is removed is kept
    for Undo; an untracked file goes to the Trash of macOS."""
    path = _rel(path)
    if untracked:
        files._move_to_trash(Path(tree) / path)
        return {"trashed": True}
    patch = run(tree, "diff", "--binary", "--no-ext-diff", "--no-color", "--", path)
    if not patch:
        return {}
    name = _save_discarded(project, path, patch)
    run(tree, "restore", "--worktree", "--", path)
    return {"undo": name}


def discarded_dir(project: str | Path) -> Path:
    return Path(project) / ".brain_fry" / "discarded"


def _save_discarded(project: str | Path, path: str, patch: bytes) -> str:
    d = discarded_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y%m%d-%H%M%S") + "-" + (re.sub(r"[^A-Za-z0-9._-]+", "_", Path(path).name) or "file")
    name, n = f"{base}.patch", 2
    while (d / name).exists():
        name, n = f"{base}-{n}.patch", n + 1
    (d / name).write_bytes(patch)
    return name


def undo_discard(tree: Path, project: str | Path, name: str) -> None:
    if "/" in name or name.startswith(".") or not name.endswith(".patch"):
        raise ValueError(f"not a discarded patch: {name!r}")
    run(tree, "apply", "--recount", "--whitespace=nowarn", str(discarded_dir(project) / name))


def commit(tree: Path, message: str) -> dict:
    if not message.strip():
        raise GitError("The message is empty.")
    run(tree, "commit", "-q", "-F", "-", stdin=message.encode("utf-8"), timeout=COMMIT_TIMEOUT)
    return {"sha": _s(run(tree, "rev-parse", "--short", "HEAD")).strip()}


# ------------------------------------------------------------------ the commands of the front end


def state(top: Path, trees: list[dict], cur: dict, tasks: list[dict]) -> dict:
    """Everything that panels 1 and 2b show, and the signature that tells the front end when to
    reload the log (refs)."""
    tree = Path(cur["path"])
    st = status(tree)
    changed_count(cur["path"], st)
    out_trees = []
    for t in trees:
        mine = [x for x in tasks if x.get("cwd") and _inside(x["cwd"], t["path"])
                and not any(_inside(x["cwd"], o["path"]) and len(o["path"]) > len(t["path"]) for o in trees)]  # fmt: skip
        out_trees.append({**t, "count": None if t["prunable"] else changed_count(t["path"]),
                          "tasks": [{"id": x["id"], "status": x["status"], "title": x["title"]} for x in mine]})  # fmt: skip
    return {
        "repo": True,
        "tree": cur["path"],
        "trees": out_trees,
        "branch": cur["branch"],
        "staged": st["staged"],
        "unstaged": st["unstaged"],
        "conflicts": st["conflicts"],
        "op": operation(tree),
        "branches": branches(top),
        "refsSig": refs_sig(top, trees),
    }


def command(cmd: str, cwd: str, project: str, msg: dict, tasks: list[dict] | None = None):
    """One `git.*` command of Review mode, for the repository of the task's folder `cwd`."""
    try:
        top, trees, cur = pick(cwd, msg.get("tree"))
    except NotRepo:
        if cmd == "git.state":
            return {"repo": False}
        raise
    tree = Path(cur["path"])
    if cmd == "git.state":
        return state(top, trees, cur, tasks or [])
    if cmd == "git.log":
        return log_rows(top, trees, cur, bool(msg.get("all", True)), max(1, int(msg.get("count") or LOG_PAGE)))
    if cmd == "git.commit_info":
        return commit_info(top, str(msg.get("sha")))
    if cmd == "git.diff":
        return diff(tree, str(msg.get("side")), str(msg.get("path")), msg.get("orig"), msg.get("sha"), bool(msg.get("full")))
    if cmd in ("git.stage", "git.unstage", "git.discard"):
        action = cmd.split(".")[1]
        path = str(msg.get("path") or "")
        if msg.get("digest"):  # a hunk, or lines of it
            r = apply_hunk(tree, project, action, path, str(msg["digest"]), msg.get("lines"))
            what = f"{len(msg['lines'])} line(s)" if msg.get("lines") else "a hunk"
            log.info("%s: %s of %s in %s", action, what, path, tree.name)
            return r
        paths = [str(p) for p in (msg.get("paths") or [])]
        if action == "stage":
            stage_files(tree, paths)
        elif action == "unstage":
            unstage_files(tree, paths, bool(cur["head"]))
        else:
            r = discard_file(tree, project, path, bool(msg.get("untracked")))
            log.info("discard: %s in %s%s", path, tree.name, " (to the Trash)" if r.get("trashed") else "")
            return r
        log.info("%s: %s in %s", action, ", ".join(paths)[:300], tree.name)
        return {}
    if cmd == "git.undo_discard":
        undo_discard(tree, project, str(msg.get("name")))
        log.info("undo of the discard %s in %s", msg.get("name"), tree.name)
        return {}
    if cmd == "git.commit":
        r = commit(tree, str(msg.get("message") or ""))
        log.info("commit %s on %s in %s", r["sha"], cur["branch"] or "a detached HEAD", tree.name)
        return r
    raise ValueError(f"unknown command {cmd!r}")
