"""Files of a project, for the explorer (2a) and the file tabs (3a). See DESIGN.md "App".

Every path is relative to the project and must stay inside it. A file's `version` is
"<mtime_ns>-<size>": cheap to poll, and enough to notice that the agent changed it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

HIDDEN = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
          ".ruff_cache", ".DS_Store", ".brain_fry", ".idea", ".tox"}  # fmt: skip
MAX_BYTES = 2 * 1024 * 1024


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


def read_file(project: str | Path, rel: str) -> dict:
    p = resolve(project, rel)
    size = p.stat().st_size
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
