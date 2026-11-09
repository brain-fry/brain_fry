import json

from brain_fry import install, paths

EXISTING = {
    "model": "opus",
    "hooks": {
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "/x/persist-session.sh"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "/x/start.sh"}]}],
    },
}


def setup(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(EXISTING))
    gitignore = home / ".gitignore_global"
    gitignore.write_text("*_nosync\n.venv")  # no final newline
    return settings, gitignore


def ours(data, event):
    return [h for g in data["hooks"].get(event, []) for h in g["hooks"] if h["command"] == install.hook_command()]


def test_install_is_idempotent_and_keeps_existing_hooks(home):
    settings, gitignore = setup(home)
    install.install(settings, gitignore)
    install.install(settings, gitignore)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    for event in install.HOOK_EVENTS:
        assert len(ours(data, event)) == 1
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "/x/persist-session.sh"
    assert data["hooks"]["SessionStart"] == EXISTING["hooks"]["SessionStart"]
    assert gitignore.read_text() == "*_nosync\n.venv\n.brain_fry/\n"
    assert len(list(settings.parent.glob("settings.json.bf-backup-*"))) == 1
    assert paths.hook_file().stat().st_mode & 0o111
    assert paths.inbox_prompt_file().exists() and paths.projects_file().exists()


def test_uninstall_removes_only_ours(home):
    settings, gitignore = setup(home)
    install.install(settings, gitignore)
    install.uninstall(settings)
    data = json.loads(settings.read_text())
    assert data["hooks"] == EXISTING["hooks"]
    assert install.uninstall(settings) == []
