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


def test_stop_failure_is_registered(home):
    settings, gitignore = setup(home)
    install.install(settings, gitignore)
    assert len(ours(json.loads(settings.read_text()), "StopFailure")) == 1


def test_an_install_without_a_new_event_is_outdated(home):
    """An install by an older version lacks StopFailure: the banner asks for `bf install`."""
    settings, gitignore = setup(home)
    assert install.hook_state(settings) == (False, False)
    install.install(settings, gitignore)
    assert install.hook_state(settings) == (True, False)
    data = json.loads(settings.read_text())
    del data["hooks"]["StopFailure"]
    settings.write_text(json.dumps(data))
    assert install.missing_events(settings.read_text()) == ["StopFailure"]
    assert install.hook_state(settings) == (True, True)
    assert install.install(settings, gitignore)[-1].endswith("for StopFailure")
    assert install.hook_state(settings) == (True, False)


def test_unreadable_settings_are_not_outdated(home):
    settings, gitignore = setup(home)
    install.install(settings, gitignore)
    settings.write_text(settings.read_text()[:-20])  # half written: cannot tell
    assert install.missing_events(settings.read_text()) == []
    assert install.hook_state(settings) == (True, False)
    settings.write_text(json.dumps({"hooks": {"Stop": "junk"}, "x": install.hook_command()}))
    assert install.hook_state(settings) == (True, True)


def test_uninstall_removes_only_ours(home):
    settings, gitignore = setup(home)
    install.install(settings, gitignore)
    install.uninstall(settings)
    data = json.loads(settings.read_text())
    assert data["hooks"] == EXISTING["hooks"]
    assert install.uninstall(settings) == []
