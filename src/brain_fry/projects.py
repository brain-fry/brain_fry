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
    """Create <path>/.brain_fry/ and register the project. True if it was not registered yet."""
    path = Path(path).expanduser().resolve()
    if path == Path.home().resolve():
        raise ValueError("the home folder cannot be a project: ~/.brain_fry/ is the global folder")
    paths.spool_dir(path).mkdir(parents=True, exist_ok=True)
    data = load()
    if any(Path(p["path"]) == path for p in data["projects"]):
        return False
    data["projects"].append({"name": path.name, "path": str(path)})
    save(data)
    return True
