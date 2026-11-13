"""The Finder tabs of column 3 (finder.py): listings in Finder's order, absolute paths anywhere on
the disk, and the right-click menu's actions on them."""

import os
import stat
import urllib.error
import urllib.request

import pytest
from test_app_backend import backend, read_pty, request  # noqa: F401  (backend: a fixture)
from websockets.sync.client import connect

from brain_fry import actions, files, finder


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "t"
    (d / "sub").mkdir(parents=True)
    for name in ("b.py", "A.md", "file10.txt", "file9.txt", ".hidden", "sub/x.py"):
        (d / name).write_text("x\n")
    (d / "Thing.app" / "Contents").mkdir(parents=True)
    return d


def test_list_folder_in_finders_order_with_hidden_flagged(tree):
    r = finder.list_folder(str(tree))
    names = [e["name"] for e in r["entries"]]
    # Finder: dotfiles first, case-insensitive, numbers as numbers, folders mixed with files
    assert names == [".hidden", "A.md", "b.py", "file9.txt", "file10.txt", "sub", "Thing.app"]
    by = {e["name"]: e for e in r["entries"]}
    assert by[".hidden"]["hidden"] and not by["A.md"]["hidden"]
    assert by["sub"]["dir"] and not by["b.py"]["dir"]
    assert by["Thing.app"]["pkg"] and not by["Thing.app"]["dir"]  # an app is a file, as in Finder
    assert (by["b.py"]["icon"], by["sub"]["icon"]) == ("e-py", "folder")
    assert r["crumbs"][0]["path"] == "/" and r["crumbs"][-1] == {"path": str(tree), "name": "t", "icon": "folder"}
    assert r["free"].endswith("B")  # "393.57 GB"


def test_the_hidden_flag_of_macos_counts_as_hidden(tree):
    os.chflags(tree / "A.md", stat.UF_HIDDEN)
    assert {e["name"]: e["hidden"] for e in finder.list_folder(str(tree))["entries"]}["A.md"]


def test_paths_must_be_absolute(tree):
    for bad in ("", "t", "relative/x", "/a\0b"):
        with pytest.raises(ValueError):
            finder.list_folder(bad)
    with pytest.raises(NotADirectoryError):
        finder.list_folder(str(tree / "b.py"))
    assert finder.absolute(f"{tree}/sub/../b.py") == tree / "b.py"


def test_info_and_icons(tree):
    i = finder.info(str(tree / "b.py"))
    assert (i["name"], i["icon"]) == ("b.py", "e-py") and i["size"] and i["modified"]
    assert finder.info(str(tree / "Thing.app"))["size"]  # a package: its size, as Finder adds it up
    png = finder.icon_png("e-py", 32)
    assert png.startswith(b"\x89PNG") and finder.icon_png("folder", 256).startswith(b"\x89PNG")
    assert finder.icon_png("p-unknown", 32) is None and finder.icon_png("e-py", 33) is None


def test_rename_trash_paste_on_absolute_paths(tree, tmp_path, monkeypatch):
    trashed = []
    monkeypatch.setattr(files, "_move_to_trash", trashed.append)
    assert finder.rename(str(tree / "b.py"), "c.py") == str(tree / "c.py")
    with pytest.raises(FileExistsError):
        finder.rename(str(tree / "c.py"), "A.md")
    finder.trash(str(tree / "c.py"))
    assert trashed == [tree / "c.py"]
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert finder.paste([str(tree / "A.md")], str(other), cut=False) == [[str(tree / "A.md"), str(other / "A.md")]]
    assert finder.paste([str(tree / "A.md")], str(other), cut=False)[0][1] == str(other / "A copy.md")
    assert finder.paste([str(tree / "sub")], str(other), cut=True) == [[str(tree / "sub"), str(other / "sub")]]
    assert (other / "sub" / "x.py").is_file() and not (tree / "sub").exists()
    with pytest.raises(PermissionError):
        finder.trash("/")


def test_new_file_and_folder_take_a_free_untitled_name(tree):
    assert finder.create(str(tree / "sub"), is_dir=True) == str(tree / "sub" / "untitled folder")
    assert finder.create(str(tree / "sub"), is_dir=True) == str(tree / "sub" / "untitled folder 2")
    assert (tree / "sub" / "untitled folder 2").is_dir()
    assert finder.create(str(tree), is_dir=False) == str(tree / "untitled file")
    assert (tree / "untitled file").read_text() == ""
    assert finder.create(str(tree), is_dir=False) == str(tree / "untitled file 2")
    with pytest.raises(NotADirectoryError):
        finder.create(str(tree / "A.md"), is_dir=False)
    with pytest.raises(ValueError):
        finder.create("relative", is_dir=True)


def test_copy_path_relative_only_inside_the_project(tree, monkeypatch):
    copied = []
    monkeypatch.setattr(files, "copy_to_clipboard", copied.append)
    assert finder.copy_path(str(tree / "sub" / "x.py"), str(tree), relative=True) == "sub/x.py"
    assert finder.copy_path(str(tree / "sub" / "x.py"), None, relative=False) == str(tree / "sub" / "x.py")
    with pytest.raises(PermissionError):
        finder.copy_path("/etc", str(tree), relative=True)
    assert copied == ["sub/x.py", str(tree / "sub" / "x.py")]


def test_finder_tabs_are_saved_and_copied_to_a_new_task_and_a_fork(project):
    t = actions.new_task(project)
    tabs = [{"root": str(project), "path": f"{project}/a.py"}, {"root": "/", "path": None}]
    assert actions.set_finder_tabs(t.id, tabs).finder_tabs == tabs
    assert actions.load(t.id).finder_tabs == tabs  # through the task file
    assert actions.load(actions.new_task(project, like=t.id).id).finder_tabs == tabs
    assert actions.load(actions.fork_task(t.id).id).finder_tabs == tabs
    with pytest.raises(ValueError):
        actions.set_finder_tabs(t.id, [{"root": "relative"}])


def test_backend_commands_and_icon_route(backend, project, tmp_path, monkeypatch):
    """Over the real websocket: list a folder outside the project, its icons over HTTP, rename,
    save the tabs, and a terminal in a folder anywhere on the disk."""
    core, port = backend
    fake_shell = tmp_path / "fake_shell"
    fake_shell.write_text('#!/bin/bash\necho "SHELL in $(pwd -P)"\nsleep 30\n')
    fake_shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_shell))
    out = tmp_path / "outside"
    (out / "deep").mkdir(parents=True)
    (out / "n.py").write_text("x\n")
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        r = request(ctl, 1, cmd="finder.list", path=str(out))["data"]
        assert [e["name"] for e in r["entries"]] == ["deep", "n.py"]
        icon = urllib.request.urlopen(f"http://127.0.0.1:{port}/icon/{r['entries'][1]['icon']}.png?s=32")
        assert icon.read().startswith(b"\x89PNG") and "max-age" in icon.headers["Cache-Control"]
        for bad in ("/icon/p-nope.png?s=32", "/icon/e-py.png?s=7", "/icon/..%2Fx.png?s=32"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}")
        assert request(ctl, 2, cmd="finder.rename", path=str(out / "n.py"), name="m.py")["data"] == str(out / "m.py")
        assert not request(ctl, 3, cmd="finder.list", path="relative")["ok"]
        assert request(ctl, 7, cmd="finder.new", dir=str(out / "deep"), folder=True)["data"] == str(out / "deep" / "untitled folder")
        tabs = [{"root": str(out), "path": str(out / "m.py")}]
        assert request(ctl, 4, cmd="finder.tabs.set", id=t.id, tabs=tabs)["ok"]
        assert actions.load(t.id).finder_tabs == tabs
        key = request(ctl, 5, cmd="shell.new", id=t.id, rows=10, cols=80, folder=str(out / "m.py"))["data"]["key"]
        with connect(f"ws://127.0.0.1:{port}/pty/{key}") as pty:
            assert f"SHELL in {out.resolve()}\r".encode() in read_pty(pty, b"\r")
        assert not request(ctl, 6, cmd="shell.new", id=t.id, rows=10, cols=80, folder=str(out / "nope" / "x"))["ok"]


def test_a_slow_thumbnail_does_not_hold_the_other_commands(backend, project, monkeypatch):
    """qlmanage may hang until its timeout (a .bin of random bytes): the commands sent after it
    must not wait for it (server.CONCURRENT)."""
    import json
    import time

    core, port = backend
    monkeypatch.setattr(finder, "thumbnail", lambda path: time.sleep(1.5) or "data:,")
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"req": 1, "cmd": "finder.thumb", "path": str(project)}))
        ctl.send(json.dumps({"req": 2, "cmd": "finder.list", "path": str(project)}))
        ctl.send(json.dumps({"req": 3, "cmd": "projects"}))
        order = []
        while len(order) < 3:
            m = json.loads(ctl.recv(timeout=5))
            if m["type"] == "reply":
                order.append(m["req"])
        assert order[-1] == 1
