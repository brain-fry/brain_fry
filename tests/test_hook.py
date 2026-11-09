import json
import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).parents[1] / "src" / "brain_fry" / "hook.sh"


def run_hook(home: Path, payload: dict, **env) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k != "BRAIN_FRY_INTERNAL"}
    full_env.update(HOME=str(home), **env)
    return subprocess.run(["bash", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=full_env)


def spooled(project: Path) -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted((project / ".brain_fry" / "events").glob("*.json"))]


def test_writes_one_event_in_the_nearest_project(home):
    proj = home / "proj"
    (proj / ".brain_fry").mkdir(parents=True)
    sub = proj / "src" / "deep"
    sub.mkdir(parents=True)
    r = run_hook(home, {"session_id": "s1", "hook_event_name": "Stop", "cwd": str(sub), "last_assistant_message": "a\nb"})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    [ev] = spooled(proj)
    assert ev["session_id"] == "s1" and ev["last_assistant_message"] == "a\nb"
    assert ev["bf_project"] == str(proj) and isinstance(ev["bf_ts"], float)
    assert not list((proj / ".brain_fry" / "events").glob(".*.tmp"))


def test_nested_project_wins(home):
    outer, inner = home / "outer", home / "outer" / "inner"
    (outer / ".brain_fry").mkdir(parents=True)
    (inner / ".brain_fry").mkdir(parents=True)
    run_hook(home, {"session_id": "s", "hook_event_name": "Stop", "cwd": str(inner)})
    assert len(spooled(inner)) == 1 and spooled(outer) == []


def test_ignores_the_global_folder_in_home(home):
    (home / ".brain_fry").mkdir()
    (home / "no_project").mkdir()
    r = run_hook(home, {"session_id": "s", "hook_event_name": "Stop", "cwd": str(home / "no_project")})
    assert r.returncode == 0
    assert not (home / ".brain_fry" / "events").exists()


def test_internal_sessions_are_ignored(home):
    proj = home / "proj"
    (proj / ".brain_fry").mkdir(parents=True)
    run_hook(home, {"session_id": "s", "hook_event_name": "Stop", "cwd": str(proj)}, BRAIN_FRY_INTERNAL="1")
    assert spooled(proj) == []


def test_garbage_input_is_harmless(home):
    r = subprocess.run(["bash", str(HOOK)], input="not json", capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
    assert (r.returncode, r.stdout) == (0, "")
