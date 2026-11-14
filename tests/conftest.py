import pytest


@pytest.fixture(autouse=True)
def _no_forgotten_sessions(monkeypatch):
    """events.FORGOTTEN (the sessions of deleted tasks) lives as long as the process: each test starts with none."""
    from brain_fry import events

    monkeypatch.setattr(events, "FORGOTTEN", set())


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake $HOME with its own ~/.brain_fry/, so that no test touches the real ones."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("BRAIN_FRY_HOME", str(h / ".brain_fry"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)  # ~/.claude is the fake one too
    return h


@pytest.fixture
def project(home):
    from brain_fry import projects

    p = home / "proj"
    p.mkdir()
    projects.init(p)
    return p
