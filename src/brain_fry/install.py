"""`bf install` / `bf uninstall`: once per machine. See DESIGN.md, "Naming and files"."""

from __future__ import annotations

import json
import shlex
import shutil
import time
from importlib import resources
from pathlib import Path

from . import paths, projects
from .util import write_atomic

HOOK_EVENTS = ("UserPromptSubmit", "Stop", "Notification", "SessionEnd")
IGNORE_LINE = ".brain_fry/"


def default_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def default_gitignore() -> Path:
    return Path.home() / ".gitignore_global"


def hook_command() -> str:
    return shlex.quote(str(paths.hook_file()))


def _ours(hook: dict) -> bool:
    return hook.get("command") == hook_command()


def _backup(path: Path) -> Path:
    dst = path.with_name(f"{path.name}.bf-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


def install(settings: Path | None = None, gitignore: Path | None = None) -> list[str]:
    """Idempotent. Returns what it did, one line per action."""
    settings, gitignore = settings or default_settings(), gitignore or default_gitignore()
    done: list[str] = []
    paths.bf_home().mkdir(parents=True, exist_ok=True)

    hook = paths.hook_file()
    src = resources.files("brain_fry").joinpath("hook.sh").read_text()
    if not hook.exists() or hook.read_text() != src:
        write_atomic(hook, src)
        done.append(f"wrote {hook}")
    hook.chmod(0o755)

    data = json.loads(settings.read_text()) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})
    added = []
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not any(_ours(h) for g in groups for h in g.get("hooks", [])):
            groups.append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]})
            added.append(event)
    if added:
        if settings.exists():
            done.append(f"backed up {settings} to {_backup(settings)}")
        settings.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(settings, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        done.append(f"added the hook to {settings} for {', '.join(added)}")

    text = gitignore.read_text() if gitignore.exists() else ""
    if IGNORE_LINE not in text.splitlines():
        with gitignore.open("a") as f:
            f.write(("" if not text or text.endswith("\n") else "\n") + IGNORE_LINE + "\n")
        done.append(f"added {IGNORE_LINE} to {gitignore}")

    prompt = paths.inbox_prompt_file()
    if not prompt.exists():
        write_atomic(prompt, resources.files("brain_fry").joinpath("inbox_prompt.md").read_text())
        done.append(f"wrote {prompt}")

    if not paths.projects_file().exists():
        projects.save(projects.load())
        done.append(f"wrote {paths.projects_file()}")
    return done


def uninstall(settings: Path | None = None) -> list[str]:
    """Removes the hook entries from settings.json. Leaves ~/.brain_fry/ and the task files."""
    settings = settings or default_settings()
    if not settings.exists():
        return []
    data = json.loads(settings.read_text())
    hooks = data.get("hooks", {})
    removed = []
    for event in HOOK_EVENTS:
        groups = hooks.get(event, [])
        kept = []
        for g in groups:
            g_hooks = [h for h in g.get("hooks", []) if not _ours(h)]
            if len(g_hooks) != len(g.get("hooks", [])):
                removed.append(event)
            if g_hooks:
                kept.append({**g, "hooks": g_hooks})
        if groups:
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not removed:
        return []
    backup = _backup(settings)
    write_atomic(settings, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return [f"backed up {settings} to {backup}", f"removed the hook from {settings} for {', '.join(removed)}"]
