import pytest


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
