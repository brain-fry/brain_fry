"""Files of a project, for the explorer (2a) and the file tabs (3a). See DESIGN_DETAILS.md "App".

Every path is relative to the project and must stay inside it. A file's `version` is
"<mtime_ns>-<size>": cheap to poll, and enough to notice that the agent changed it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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
    p = resolve(project, rel)
    ctype = VIEW_TYPES.get(_ext(rel))
    if not ctype or not p.is_file():
        raise FileNotFoundError(rel)
    return p, ctype


def open_external(p: Path) -> None:
    """With the default app of macOS, for the files that 3a does not show."""
    subprocess.run(["open", str(p)], check=True, capture_output=True, timeout=10)


def read_file(project: str | Path, rel: str) -> dict:
    """The text of a file for the editor. For an image or a PDF only its kind and version:
    the viewer loads it from `/file/...`."""
    p = resolve(project, rel)
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
    p = resolve(project, rel)
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
            out[rel] = version(resolve(project, rel))
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
