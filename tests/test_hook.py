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
    assert f"Stop s1 --> {proj}" in (home / ".brain_fry" / "logs" / "hook.log").read_text()


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


def write_selection(home: Path, **over) -> dict:
    sel = {"session_id": "s1", "pid": os.getpid(), "path": "src/x.py", "from": 51, "to": 52,
           "text": "a = 1\nb = 2", "cut": False, "dirty": False, "ts": 0}  # fmt: skip
    sel.update(over)
    (home / ".brain_fry").mkdir(exist_ok=True)
    (home / ".brain_fry" / "selection.json").write_text(json.dumps(sel))
    return sel


def prompt(home: Path, proj: Path, sid="s1", event="UserPromptSubmit") -> subprocess.CompletedProcess:
    return run_hook(home, {"session_id": sid, "hook_event_name": event, "cwd": str(proj), "prompt": "why?"})


def test_prints_the_selection_with_a_prompt_to_its_task(home):
    proj = home / "proj"
    (proj / ".brain_fry").mkdir(parents=True)
    write_selection(home)
    r = prompt(home, proj)
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout == ("The user selected the lines 51 to 52 from src/x.py:\na = 1\nb = 2\n\n"
                        "This may or may not be related to the current task.\n")  # fmt: skip
    assert len(spooled(proj)) == 1  # the event is written as before
    assert f"UserPromptSubmit s1 --> {proj} + selection src/x.py 51-52" in (home / ".brain_fry" / "logs" / "hook.log").read_text()


def test_selection_one_line_unsaved_and_cut(home):
    proj = home / "proj"
    (proj / ".brain_fry").mkdir(parents=True)
    write_selection(home, to=51, dirty=True, cut=True, text="x")
    out = prompt(home, proj).stdout
    assert out.startswith("The user selected line 51 from src/x.py (in the editor, with unsaved changes")
    assert "x\n[... cut here: read the file for the rest]\n" in out


def test_selection_is_not_printed_elsewhere(home):
    proj = home / "proj"
    (proj / ".brain_fry").mkdir(parents=True)
    write_selection(home)
    assert prompt(home, proj, sid="other").stdout == ""  # another session (VS Code, another task)
    assert prompt(home, proj, event="Stop").stdout == ""  # only a prompt carries it
    dead = subprocess.Popen(["true"])
    dead.wait()
    write_selection(home, pid=dead.pid)  # the app that wrote it is gone (a crash)
    assert prompt(home, proj).stdout == ""
    assert len(spooled(proj)) == 3  # the events are written all the same
