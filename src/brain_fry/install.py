"""`bf install` / `bf uninstall`: once per machine. See DESIGN_DETAILS.md, "Naming and files"."""

from __future__ import annotations

import json
import shlex
import shutil
import time
from importlib import resources
from pathlib import Path

from . import paths, projects
from .util import write_atomic

# StopFailure since 2026-11-14: a turn that ends on an API error fires it instead of Stop.
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "StopFailure", "Notification", "SessionEnd")
IGNORE_LINE = ".brain_fry/"


def default_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def default_gitignore() -> Path:
    return Path.home() / ".gitignore_global"


def packaged_hook() -> str:
    """The hook.sh of this version of Brain Fry. `bf install` copies it to ~/.brain_fry/."""
    return resources.files("brain_fry").joinpath("hook.sh").read_text()


def hook_command() -> str:
    return shlex.quote(str(paths.hook_file()))


def _ours(hook: dict) -> bool:
    return hook.get("command") == hook_command()


def _has_ours(groups) -> bool:
    """The hook groups of one event in settings.json run our hook."""
    return isinstance(groups, list) and any(
        isinstance(g, dict) and isinstance(h, dict) and _ours(h) for g in groups for h in g.get("hooks") or [])


def missing_events(settings_text: str) -> list[str]:
    """The events of HOOK_EVENTS that the settings.json do not run our hook for: an install
    by an older version of Brain Fry. Empty when the file cannot be read as JSON (cannot tell)."""
    try:
        data = json.loads(settings_text)
    except ValueError:
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    return [e for e in HOOK_EVENTS if not _has_ours(hooks.get(e))]


def hook_state(settings: Path | None = None) -> tuple[bool, bool]:
    """(installed, outdated), for the app's banner. Installed: ~/.brain_fry/hook.sh exists and
    settings.json runs it. Outdated: that copy differs from this version's hook.sh, or an event
    of HOOK_EVENTS lacks it. Either way `bf install` fixes it."""
    settings = settings or default_settings()
    try:
        text = settings.read_text()
        if not (paths.hook_file().exists() and hook_command() in text):
            return False, False
        return True, paths.hook_file().read_text() != packaged_hook() or bool(missing_events(text))
    except OSError:
        return False, False


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
    src = packaged_hook()
    if not hook.exists() or hook.read_text() != src:
        write_atomic(hook, src)
        done.append(f"wrote {hook}")
    hook.chmod(0o755)

    data = json.loads(settings.read_text()) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})
    added = []
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not _has_ours(groups):
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
