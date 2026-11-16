"""Files of a project, for the explorer (2a) and the file tabs (3a). See DESIGN_DETAILS.md "2a, 3a, 3b: files and terminals".

Every path is relative to the project and must stay inside it. A file's `version` is
"<mtime_ns>-<size>": cheap to poll, and enough to notice that the agent changed it.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

HIDDEN = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
          ".ruff_cache", ".DS_Store", ".brain_fry", ".idea", ".tox"}  # fmt: skip
MAX_BYTES = 2 * 1024 * 1024

# The files that 3a shows in a viewer and not in the editor, by extension, with the type that
# `/file/...` serves them with (server.py): their bytes go over HTTP, not as text over /ctl.
# SVG is text: it stays in the editor. Images are what WebKit decodes.
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
               "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp", "ico": "image/x-icon",
               "tif": "image/tiff", "tiff": "image/tiff", "heic": "image/heic", "heif": "image/heif"}  # fmt: skip
VIEW_TYPES = {**IMAGE_TYPES, "pdf": "application/pdf"}
MAX_VIEW_BYTES = 200 * 1024 * 1024


class Conflict(Exception):
    """The file changed on disk since the version that the editor started from."""


def resolve(project: str | Path, rel: str) -> Path:
    root = Path(project).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"outside the project: {rel}")
    return p


# The file tabs of column 3 name a file of the project by its path relative to the project, and any
# other file by its absolute path (asked by Renaud on 2026-11-14; DESIGN_DETAILS.md "2a, 3a, 3b",
# files outside the project). A relative path never starts with "/". The explorer (2a) and its
# commands stay inside the project: they use `resolve`, the tabs use `locate`.


def tab_path(project: str | Path, path: str | Path) -> str:
    """How a tab names the file at `path` (absolute; the open panel's or the save panel's choice,
    a link, `bf`): relative to the project when it is inside it, else absolute."""
    root = Path(project).resolve()
    p = Path(path).resolve()
    return p.relative_to(root).as_posix() if root in p.parents else str(p)


def locate(project: str | Path, path: str) -> Path:
    """The file of a tab: `path` relative to the project (which it must stay inside), or absolute."""
    return Path(path).resolve() if path.startswith("/") else resolve(project, path)


LINE_FRAGMENT = re.compile(r"L(\d+)(?:-L?\d+)?")  # #L2, #L2-5, #L2-L5: line 2


def link_target(project: str | Path, cwd: str | None, target: str) -> dict:
    """A file link of a terminal, as Claude Code writes it (OSC 8, from a Markdown link):
    `hello.py#L2`, `docs/a%20b.md`, `/abs/x.py`, `file:///abs/x.py`. A relative target is
    relative to the session's cwd. Returns {"path": as a tab names it (tab_path), "line": n or None}.
    See DESIGN_DETAILS.md "Browser (3a)", file links in 4a."""
    s = target.strip()
    if s[:5].lower() == "file:":
        u = urlsplit(s)
        s = u.path + (f"#{u.fragment}" if u.fragment else "")
    path, _, fragment = s.partition("#")
    m = LINE_FRAGMENT.fullmatch(fragment)
    path = unquote(path)
    if not path:
        raise ValueError(f"not a file: {target}")
    base = Path(cwd) if cwd and Path(cwd).is_dir() else Path(project)
    p = (base / path).resolve()  # an absolute path replaces the base
    if not p.exists():
        raise FileNotFoundError(f"no such file: {target}")
    if p.is_dir():
        raise ValueError(f"a folder, not a file: {target}")
    return {"path": tab_path(project, p), "line": int(m.group(1)) if m else None}


LINE_SUFFIX = re.compile(r"(.+?):(\d+)(?::\d+)?")  # app.py:42, app.py:42:7 (the column is ignored)
TEXT_LINE = re.compile(r"(.+?):(\d+)(?:[-:]\d+)?")  # also app.py:42-50, as Claude writes a range
MAX_TEXT_PATHS = 64


def text_paths(project: str | Path, base: str | None, targets: list[str]) -> list[dict | None]:
    """The paths that a terminal shows as plain text (Claude's `docs/a.md`, `src/x.py:42`,
    `~/.brain_fry/logs/bf.log`; `git status` in 3b), as the front end cut them out of a line.
    A relative path is relative to `base` (the session's cwd, a shell's folder), else to the
    project. For each target: {"path": as a tab names it, "line": n or None} when it names a
    file, else None (missing, a folder). Cmd+click opens it; see DESIGN_DETAILS.md "Browser (3a)",
    plain paths in the terminals."""
    roots = [Path(project)]
    if base and Path(base).is_dir() and Path(base).resolve() != Path(project).resolve():
        roots.insert(0, Path(base))

    def find(s: str) -> Path | None:
        try:
            p = Path(s).expanduser()
        except RuntimeError:  # ~someone who does not exist
            return None
        for root in roots:
            q = root / p  # an absolute path replaces the root
            try:
                if q.is_file():
                    return q.resolve()
            except OSError:  # a name too long
                return None
            if p.is_absolute():
                return None
        return None

    out: list[dict | None] = []
    for t in targets[:MAX_TEXT_PATHS]:
        s, line = str(t).strip()[:1024], None
        p = find(s) if s else None
        if p is None and (m := TEXT_LINE.fullmatch(s)):
            p, line = find(m.group(1)), int(m.group(2))
        out.append({"path": tab_path(project, p), "line": line} if p else None)
    return out


def bf_target(project: str | Path, target: str) -> dict:
    """A file named by `bf <file>` in a terminal tab (shell_init.bash): an absolute path, which
    may end in `:line` or `:line:col` when no file has that name. Returns {"path": as a tab
    names it (tab_path), "line": n or None, "new": True when the file does not exist yet but its folder
    does (it opens empty, and its first save writes it, as VS Code's `code new.txt`)}.
    See DESIGN_DETAILS.md "2a, 3a, 3b", `bf <file>`."""
    if not Path(target).is_absolute():
        raise ValueError(f"not an absolute path: {target}")
    p, line = Path(target), None
    if not p.exists():
        m = LINE_SUFFIX.fullmatch(target)
        if m and Path(m.group(1)).is_file():
            p, line = Path(m.group(1)), int(m.group(2))
    p = p.resolve()
    shown = tab_path(project, p)
    if p.is_dir():
        raise ValueError(f"{p.name} is a folder, not a file.")
    if not p.exists() and not p.parent.is_dir():
        raise FileNotFoundError(f"no such folder: {tab_path(project, p.parent)}")
    return {"path": shown, "line": line, "new": not p.exists()}


def version(p: Path) -> str | None:
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return f"{st.st_mtime_ns}-{st.st_size}"


def list_dir(project: str | Path, rel: str = "") -> list[dict]:
    d = resolve(project, rel)
    out = []
    for e in os.scandir(d):
        if e.name in HIDDEN:
            continue
        try:
            is_dir = e.is_dir()
        except OSError:
            continue
        out.append({"name": e.name, "path": str(Path(rel) / e.name) if rel else e.name, "dir": is_dir})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return out


def git_head(p: Path) -> str | None:
    """The file as in git HEAD, or None (not in a repo, untracked, new)."""
    try:
        top = subprocess.run(["git", "-C", str(p.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)  # fmt: skip
        if top.returncode != 0:
            return None
        rel = p.relative_to(Path(top.stdout.strip()).resolve())
        r = subprocess.run(["git", "-C", top.stdout.strip(), "show", f"HEAD:{rel.as_posix()}"],
                           capture_output=True, timeout=5)  # fmt: skip
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8")
    except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None


def _ext(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def view_kind(rel: str) -> str | None:
    """'pdf', 'image', or None for a file that the editor shows (or refuses)."""
    ext = _ext(rel)
    return "pdf" if ext == "pdf" else "image" if ext in IMAGE_TYPES else None


def viewable(project: str | Path, rel: str) -> tuple[Path, str]:
    """The file and its content type, for `/file/...`. Only the files of the viewers: the route
    must not serve a page (HTML, SVG) that would run in the origin of the app."""
    p = locate(project, rel)
    ctype = VIEW_TYPES.get(_ext(rel))
    if not ctype or not p.is_file():
        raise FileNotFoundError(rel)
    return p, ctype


def open_external(p: Path) -> None:
    """With the default app of macOS, for the files that 3a does not show."""
    subprocess.run(["open", str(p)], check=True, capture_output=True, timeout=10)


def open_with(p: Path, app: str) -> None:
    """Open With (the right-click menu): `p` in that app, an `.app` bundle (`open -a`)."""
    a = Path(app)
    if a.suffix != ".app" or not a.is_dir():
        raise ValueError(f"not an app: {a.name}")
    subprocess.run(["open", "-a", str(a), str(p)], check=True, capture_output=True, timeout=10)


def reveal_in_finder(p: Path) -> None:
    """Open in Finder for a file: a Finder window on its folder, the file selected (`open -R`)."""
    subprocess.run(["open", "-R", str(p)], check=True, capture_output=True, timeout=10)


def read_file(project: str | Path, rel: str) -> dict:
    """The text of a file for the editor. For an image or a PDF only its kind and version:
    the viewer loads it from `/file/...`."""
    p = locate(project, rel)
    size = p.stat().st_size
    kind = view_kind(rel)
    if kind:
        return {"path": rel, "kind": kind, "size": size, "version": version(p)}
    if size > MAX_BYTES:
        return {"path": rel, "tooLarge": True, "size": size}
    data = p.read_bytes()
    if b"\x00" in data[:8192]:
        return {"path": rel, "binary": True, "size": size}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": rel, "binary": True, "size": size}
    return {"path": rel, "text": text, "version": version(p), "head": git_head(p)}


def write_file(project: str | Path, rel: str, text: str, base: str | None, force: bool = False) -> dict:
    """Atomic write that keeps the file's mode. Refuses (Conflict) when the file changed on
    disk since version `base`, unless `force`."""
    p = locate(project, rel)
    if not force and version(p) != base:
        raise Conflict(rel)
    mode = p.stat().st_mode if p.exists() else None
    tmp = p.with_name(f".{p.name}.bf-tmp")
    tmp.write_bytes(text.encode("utf-8"))
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, p)
    return {"path": rel, "version": version(p)}


def versions(project: str | Path, rels: list[str]) -> dict[str, str | None]:
    out = {}
    for rel in rels:
        try:
            out[rel] = version(locate(project, rel))
        except PermissionError:
            out[rel] = None
    return out


# ---------------------------------------------------------------- the right-click menu of 2a

def entry(project: str | Path, rel: str) -> Path:
    """The entry `rel` itself, not what it points to: a symlink is renamed, moved or trashed as a
    link, never its target. Its folder must be inside the project; the project root is no entry."""
    parent, _, name = rel.strip("/").rpartition("/")
    if name in ("", ".", ".."):
        raise PermissionError(f"not an entry of the project: {rel!r}")
    p = resolve(project, parent) / name
    if not os.path.lexists(p):
        raise FileNotFoundError(f"no such file or folder: {rel}")
    return p


def _join(folder: str, name: str) -> str:
    folder = folder.strip("/")
    return f"{folder}/{name}" if folder else name


def _same(a: Path, b: Path) -> bool:
    """One entry under two names: a change of case on a case-insensitive disk (macOS)."""
    sa, sb = os.lstat(a), os.lstat(b)
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def rename(project: str | Path, rel: str, name: str) -> str:
    """Gives the file or folder `rel` a new name in its folder. Returns its new path."""
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"not a valid name: {name!r}")
    src = entry(project, rel)
    if name == src.name:
        return rel.strip("/")
    dst = src.with_name(name)
    if os.path.lexists(dst) and not _same(src, dst):
        raise FileExistsError(f"{name} already exists here")
    os.rename(src, dst)
    return _join(rel.strip("/").rpartition("/")[0], name)


def create(project: str | Path, folder: str, name: str, is_dir: bool) -> str:
    """New File... / New Folder...: an empty file, or a folder, `name` in `folder` ("" is the
    project folder). As in VS Code, a name with `/` also makes the folders on the way ("a/b.py").
    Never replaces anything. Returns the new path."""
    parts = [p for p in name.strip().split("/") if p]
    if not parts or "\0" in name or any(p in (".", "..") for p in parts):
        raise ValueError(f"not a valid name: {name!r}")
    base = resolve(project, folder)
    if not base.is_dir():
        raise NotADirectoryError(f"not a folder: {folder}")
    rel = _join(folder, "/".join(parts))
    target = base.joinpath(*parts)
    if os.path.lexists(target):
        raise FileExistsError(f"{'/'.join(parts)} already exists here")
    d = base
    for p in parts[:-1]:
        d = d / p
        if os.path.lexists(d) and not d.is_dir():
            raise NotADirectoryError(f"{p} is a file, not a folder")
    resolve(project, rel)  # the folders on the way may be links that lead out of the project
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_dir:
        target.mkdir()
    else:
        target.open("x").close()
    return rel


def trash(project: str | Path, rel: str) -> None:
    """Moves the file or folder `rel` to the Trash of macOS, from where Finder can put it back."""
    _move_to_trash(entry(project, rel))


def _move_to_trash(p: Path) -> None:
    from Foundation import NSURL, NSFileManager  # noqa: PLC0415 (pyobjc, a pywebview dependency)

    ok, _, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(p)), None, None)  # fmt: skip
    if not ok:
        raise OSError(f"could not move {p.name} to the Trash: {err.localizedDescription() if err else 'unknown error'}")


def free_name(folder: Path, name: str, is_dir: bool) -> str:
    """`name`, or the name that Finder and VS Code give a copy: "a copy.py", "a copy 2.py"."""
    if not os.path.lexists(folder / name):
        return name
    dot = name.rfind(".")
    stem, ext = (name[:dot], name[dot:]) if dot > 0 and not is_dir else (name, "")
    n = 1
    while True:
        cand = f"{stem} copy{f' {n}' if n > 1 else ''}{ext}"
        if not os.path.lexists(folder / cand):
            return cand
        n += 1


def paste(src_project: str | Path, rels: list[str], dst_project: str | Path, dst_dir: str, cut: bool) -> list[list[str]]:
    """Copies, or moves (`cut`), the entries `rels` of `src_project` into the folder `dst_dir` of
    `dst_project`. Returns [old path, new path] pairs. A copy never replaces a file: it takes a
    free name. A move onto an existing name is refused; a move to where the entry already is
    does nothing (no pair)."""
    dest = resolve(dst_project, dst_dir)
    if not dest.is_dir():
        raise NotADirectoryError(f"not a folder: {dst_dir}")
    pairs = []
    for rel in rels:
        src = entry(src_project, rel)
        is_dir = src.is_dir() and not src.is_symlink()
        if is_dir and (dest == src.resolve() or src.resolve() in dest.parents):
            raise ValueError(f"cannot paste the folder {src.name} into itself")
        if cut and src.parent == dest:
            continue
        name = src.name if cut else free_name(dest, src.name, is_dir)
        target = dest / name
        if cut:
            if os.path.lexists(target):
                raise FileExistsError(f"{name} already exists in {dst_dir.strip('/') or 'the project folder'}")
            shutil.move(src, target)
        elif is_dir:
            shutil.copytree(src, target, symlinks=True)
        else:
            shutil.copy2(src, target, follow_symlinks=False)
        pairs.append([rel.strip("/"), _join(dst_dir, name)])
    return pairs


ZIP_MIN_TIME = 315576000  # 1980-01-02: a zip cannot hold an older date


def zip_entry(project: str | Path, rel: str) -> str:
    """Zip (the right-click menu of 2a): the file or folder `rel` into "<name>.zip" next to it,
    or "<name> 2.zip", "<name> 3.zip", as Finder's Compress names it. Never replaces anything.
    A folder's entries start with its name, so unzipping gives the folder back. The zip holds
    what the explorer shows: the HIDDEN names are left out (.git, .venv, .DS_Store...), and so
    are the files that are neither regular files nor links (sockets, pipes); a link is stored as
    a link, never its target. A zip that fails is removed. Returns the zip's path."""
    src = entry(project, rel)
    n = 1
    while True:
        name = f"{src.name}{f' {n}' if n > 1 else ''}.zip"
        target = src.parent / name
        try:
            fh = open(target, "xb")  # noqa: SIM115 (the name is taken at once: a second Zip takes the next name)
            break
        except FileExistsError:
            n += 1
    try:
        with fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
            _zip_add(z, src, src.name)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return _join(rel.strip("/").rpartition("/")[0], name)


def _zip_add(z: zipfile.ZipFile, p: Path, arc: str) -> None:
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        info = zipfile.ZipInfo(arc, time.localtime(max(st.st_mtime, ZIP_MIN_TIME))[:6])
        info.create_system = 3  # Unix: unzip, ditto and Archive Utility make the link again
        info.external_attr = (st.st_mode & 0xFFFF) << 16
        z.writestr(info, os.readlink(p))
    elif stat.S_ISDIR(st.st_mode):
        z.write(p, f"{arc}/")
        for e in sorted(os.scandir(p), key=lambda e: e.name):
            if e.name not in HIDDEN:
                _zip_add(z, Path(e.path), f"{arc}/{e.name}")
    elif stat.S_ISREG(st.st_mode):
        z.write(p, arc)


def path_text(project: str | Path, rel: str, relative: bool) -> str:
    """For "Copy path" and "Copy relative path": the path as the explorer shows it (symlinks
    not followed), absolute or relative to the project folder."""
    resolve(project, rel)  # inside the project
    rel = rel.strip("/")
    return rel if relative else str(Path(project) / rel) if rel else str(Path(project))


def copy_to_clipboard(text: str) -> None:
    """pbcopy reads its input in the encoding of the locale: UTF-8, or accents come out wrong."""
    env = {**os.environ, "LC_ALL": "en_US.UTF-8"}
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=env, check=True, timeout=5)
