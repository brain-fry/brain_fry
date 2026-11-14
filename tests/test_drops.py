"""Files and folders dropped from Finder: the paths that pywebview recorded (app/drops.py).
The drop itself needs a window: tests/window/drop/drop_check.py."""

import logging
import unicodedata

from test_app_backend import backend, request  # noqa: F401  (backend: a fixture)
from websockets.sync.client import connect

from brain_fry.app import drops


def test_match_gives_each_name_its_path_and_tells_folders(tmp_path):
    (tmp_path / "some folder").mkdir()
    (tmp_path / "a.png").write_text("x")
    held = [f"{tmp_path}/a.png", f"{tmp_path}/some folder/", "/elsewhere/other.txt"]  # a folder comes with a slash
    assert drops.match(["some folder", "a.png"], held) == [
        {"path": f"{tmp_path}/some folder", "dir": True},
        {"path": f"{tmp_path}/a.png", "dir": False},
    ]
    assert drops.match(["missing.txt"], held) == []
    assert drops.match([], held) == []


def test_match_takes_the_path_recorded_last_and_each_one_once():
    held = ["/old/a.txt", "/new/a.txt"]
    assert [f["path"] for f in drops.match(["a.txt"], held)] == ["/new/a.txt"]
    assert [f["path"] for f in drops.match(["a.txt", "a.txt", "a.txt"], held)] == ["/new/a.txt", "/old/a.txt"]


def test_match_compares_names_whatever_their_unicode_form():
    nfd, nfc = unicodedata.normalize("NFD", "café.txt"), unicodedata.normalize("NFC", "café.txt")
    assert nfd != nfc
    assert [f["path"] for f in drops.match([nfc], [f"/x/{nfd}"])] == [f"/x/{nfd}"]
    assert [f["path"] for f in drops.match([nfd], [f"/x/{nfc}"])] == [f"/x/{nfc}"]


def test_take_empties_the_record(monkeypatch):
    state = {"num_listeners": 0, "paths": [("a.txt", "/x/a.txt"), ("b.txt", "/x/b.txt")]}
    monkeypatch.setattr(drops, "_state", lambda: state)
    assert drops.enable() and state["num_listeners"] == 1
    assert [f["path"] for f in drops.take(["a.txt"])] == ["/x/a.txt"]
    assert state["paths"] == []  # b.txt belonged to a drop that nobody asked for: not given to the next one
    assert drops.take(["b.txt"]) == []


def test_without_pywebviews_record_nothing_is_found(monkeypatch):
    monkeypatch.setattr(drops, "_state", lambda: None)
    assert drops.enable() is False
    assert drops.take(["a.txt"]) == []


def test_drop_take_through_the_core(backend, tmp_path, monkeypatch, caplog):  # noqa: F811
    core, port = backend
    (tmp_path / "dropped dir").mkdir()
    state = {"num_listeners": 1, "paths": [("secret-name.txt", f"{tmp_path}/secret-name.txt"), ("dropped dir", f"{tmp_path}/dropped dir/")]}
    monkeypatch.setattr(drops, "_state", lambda: state)
    caplog.set_level(logging.DEBUG)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        reply = request(ctl, 1, cmd="drop.take", names=["secret-name.txt", "dropped dir"])
        assert reply["data"] == [
            {"path": f"{tmp_path}/secret-name.txt", "dir": False},
            {"path": f"{tmp_path}/dropped dir", "dir": True},
        ]
        assert request(ctl, 2, cmd="drop.take", names=["secret-name.txt"])["data"] == []
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("brain_fry"))  # not the websocket library's frames
    assert "drop: 2 dropped, 2 with a path (1 folders)" in ours
    assert "command drop.take names=<2>" in ours
    assert "secret-name" not in ours  # a path dropped on Claude Code becomes a part of a prompt
