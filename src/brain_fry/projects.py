"""~/.brain_fry/projects.json: the registered projects (and views, ignored in v1)."""

import json
from pathlib import Path

from . import paths
from .util import write_atomic


def load() -> dict:
    f = paths.projects_file()
    if not f.exists():
        return {"projects": [], "views": {}}
    return json.loads(f.read_text())


def save(data: dict) -> None:
    paths.bf_home().mkdir(parents=True, exist_ok=True)
    write_atomic(paths.projects_file(), json.dumps(data, indent=2) + "\n")


def project_paths() -> list[Path]:
    return [Path(p["path"]) for p in load()["projects"]]


def init(path: Path) -> bool:
    """Create <path>/.brain_fry/ and register the project, or put a removed project back in
    the lists. True if it was not registered yet, or was removed from the lists."""
    path = Path(path).expanduser().resolve()
    if path == Path.home().resolve():
        raise ValueError("the home folder cannot be a project: ~/.brain_fry/ is the global folder")
    paths.spool_dir(path).mkdir(parents=True, exist_ok=True)
    data = load()
    for p in data["projects"]:
        if Path(p["path"]) == path:
            if p.pop("active", True) is not False:
                return False
            save(data)
            return True
    data["projects"].append({"name": path.name, "path": str(path)})
    save(data)
    return True


def deactivate(path: str) -> None:
    """Remove a project from the lists of the app (dropdown, Cmd+P, Cmd+N): `"active": false`
    in projects.json. It stays registered: its events are still applied, its tasks still shown."""
    data = load()
    for p in data["projects"]:
        if Path(p["path"]) == Path(path):
            p["active"] = False
            save(data)
            return
    raise ValueError(f"not a registered project: {path}")
