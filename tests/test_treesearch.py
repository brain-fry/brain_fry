"""The explorer's search (treesearch.py, `fs.search`): names and text of a whole project."""

import json
import logging
import os
import time

import pytest
from websockets.sync.client import connect

from brain_fry import actions, files, treesearch
from brain_fry.app.core import Core
from brain_fry.app.server import start_backend


def write(root, rel, text="", mode="w"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "wb":
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")
    return p


def paths(r):
    return [f["path"] for f in r["files"]]


def hit(r, path):
    return next(f for f in r["files"] if f["path"] == path)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "proj"
    write(root, "src/finder.py", "import os\n\ndef finder():\n    return Finder()\n")
    write(root, "src/app.py", "x = 1\n# no match here\n")
    write(root, "src/sub/deep.txt", "a finder\nfinder again, finder\nnothing\n")
    write(root, "docs/Guide.md", "Use the FINDER.\n")
    write(root, "README.md", "finders keepers\n")
    write(root, "finder/inside.txt", "nothing at all\n")  # a folder named like the query: folders do not match
    return root


def test_names_and_text_in_the_order_of_the_tree(tree):
    r = treesearch.search(tree, "finder")
    # folders first, then by lowercase name, as files.list_dir; depth first
    assert paths(r) == ["docs/Guide.md", "src/sub/deep.txt", "src/finder.py", "README.md"]
    assert hit(r, "src/finder.py")["name"] == [[0, 6]]
    assert hit(r, "src/finder.py")["count"] == 2  # two lines hold a match (Finder: case ignored)
    assert hit(r, "src/sub/deep.txt")["count"] == 2  # one line with two matches counts once
    assert hit(r, "src/sub/deep.txt")["name"] == []
    assert hit(r, "README.md")["count"] == 1
    assert r["more"] is False


def test_names_only_and_text_only(tree):
    assert paths(treesearch.search(tree, "finder", text=False)) == ["src/finder.py"]
    r = treesearch.search(tree, "finder", names=False)
    assert hit(r, "src/finder.py")["name"] == [] and hit(r, "src/finder.py")["count"] == 2
    assert paths(treesearch.search(tree, "guide", names=False)) == []


def test_match_case_whole_word_and_regexp(tree):
    assert paths(treesearch.search(tree, "FINDER", case=True)) == ["docs/Guide.md"]
    r = treesearch.search(tree, "finder", word=True)
    assert "README.md" not in paths(r)  # "finders" is not the word
    assert hit(r, "src/finder.py")["name"] == [[0, 6]]  # "finder.py": the dot ends the word
    r = treesearch.search(tree, r"def \w+\(", regex=True)
    assert paths(r) == ["src/finder.py"] and hit(r, "src/finder.py")["line"] == 3
    # a query with regex characters is a plain substring unless regex is on
    write(tree, "rx.txt", "a.b\naxb\n")
    assert hit(treesearch.search(tree, "a.b"), "rx.txt")["count"] == 1
    assert hit(treesearch.search(tree, "a.b", regex=True), "rx.txt")["count"] == 2
    # whole word with a regexp: the alternation is grouped
    assert hit(treesearch.search(tree, "axb|zzz", regex=True, word=True), "rx.txt")["count"] == 1


def test_an_invalid_regexp_is_a_value_error_without_the_query():
    with pytest.raises(ValueError) as e:
        treesearch.matcher("secret(", regex=True)
    assert "secret" not in str(e.value) and "regular expression" in str(e.value)
    with pytest.raises(ValueError):
        treesearch.matcher("")


def test_empty_matches_do_not_count(tree):
    write(tree, "e.txt", "abc\n")
    r = treesearch.search(tree, r"z*", regex=True)
    assert r["files"] == []


def test_include_and_exclude(tree):
    write(tree, "tests/test_finder.py", "finder\n")
    write(tree, "vendor/lib/finder.min.js", "finder\n")
    assert paths(treesearch.search(tree, "finder", include="*.py")) == ["src/finder.py", "tests/test_finder.py"]
    assert paths(treesearch.search(tree, "finder", include="tests/")) == ["tests/test_finder.py"]
    assert paths(treesearch.search(tree, "finder", include="src/sub")) == ["src/sub/deep.txt"]
    assert paths(treesearch.search(tree, "finder", include="src/**/*.txt")) == ["src/sub/deep.txt"]
    assert paths(treesearch.search(tree, "finder", include="src/**/*.py")) == ["src/finder.py"]  # ** also no folder
    assert "vendor/lib/finder.min.js" not in paths(treesearch.search(tree, "finder", exclude="vendor/"))
    r = treesearch.search(tree, "finder", exclude="*.min.js, docs/, *.md")
    assert paths(r) == ["src/sub/deep.txt", "src/finder.py", "tests/test_finder.py"]
    assert paths(treesearch.search(tree, "finder", include="/src")) == ["src/sub/deep.txt", "src/finder.py"]
    assert paths(treesearch.search(tree, "finder", include="SRC/FINDER.PY")) == ["src/finder.py"]  # case ignored
    assert treesearch.search(tree, "finder", include=" , ")["files"]  # blanks: no pattern


def test_an_excluded_folder_is_not_walked(tree, monkeypatch):
    seen = []
    real = treesearch._text_of
    monkeypatch.setattr(treesearch, "_text_of", lambda path, rel, size: seen.append(rel) or real(path, rel, size))
    treesearch.search(tree, "finder", exclude="src/")
    assert not [p for p in seen if p.startswith("src/")]


def test_hidden_folders_binary_large_and_viewer_files(tree):
    write(tree, ".git/finder.txt", "finder\n")
    write(tree, "node_modules/x/finder.js", "finder\n")
    write(tree, "bin/data.bin", b"finder\x00\x01", mode="wb")
    write(tree, "bin/finder.dat", b"\x00finder", mode="wb")
    write(tree, "big.txt", "finder\n" + "x" * files.MAX_BYTES)
    write(tree, "latin.txt", "finder caf\xe9\n".encode("latin-1"), mode="wb")
    write(tree, "img/finder.png", b"finder", mode="wb")
    write(tree, "img/shot.png", b"finder", mode="wb")
    write(tree, "doc.pdf", b"finder", mode="wb")
    r = treesearch.search(tree, "finder")
    got = paths(r)
    assert not [p for p in got if p.startswith((".git/", "node_modules/"))]
    assert "bin/data.bin" not in got and "big.txt" not in got and "latin.txt" not in got  # their text is skipped
    assert "img/shot.png" not in got and "doc.pdf" not in got
    assert hit(r, "bin/finder.dat") == {"path": "bin/finder.dat", "name": [[0, 6]], "count": 0}  # its name still matches
    assert hit(r, "img/finder.png")["count"] == 0


def test_symlinks_to_folders_are_not_followed(tree, tmp_path):
    outside = tmp_path / "outside"
    write(outside, "finder_out.txt", "finder\n")
    os.symlink(outside, tree / "linked")
    os.symlink(tree / "src", tree / "src_again")
    os.symlink(outside / "finder_out.txt", tree / "out_link.txt")  # a file outside: name only
    os.symlink(tree / "README.md", tree / "readme_link.md")  # a file inside: its text too
    r = treesearch.search(tree, "finder")
    got = paths(r)
    assert not [p for p in got if p.startswith(("linked/", "src_again/"))]
    assert "out_link.txt" not in got
    assert hit(r, "readme_link.md")["count"] == 1
    assert "finder_out.txt" not in str(r)


def test_the_cap(tree):
    for i in range(12):
        write(tree, f"many/f{i:02}.txt", "finder\n")
    r = treesearch.search(tree, "finder", limit=5)
    assert len(r["files"]) == 5 and r["more"] is True
    assert paths(r)[0] == "docs/Guide.md"  # the first ones in the order of the tree
    r = treesearch.search(tree, "finder", limit=16)  # 4 + 12: exactly the cap
    assert len(r["files"]) == 16 and r["more"] is False
    assert treesearch.MAX_FILES == 2000


def test_the_first_match_in_utf16_units(tree):
    # é is one unit, 😀 two (a surrogate pair in JS); the line counts from 1
    write(tree, "u.txt", "first line\nné 😀 x finder and finder\nfinder\n")
    f = hit(treesearch.search(tree, "FINDER"), "u.txt")
    assert (f["line"], f["col"], f["len"], f["count"]) == (2, 8, 6, 2)
    write(tree, "u2.txt", "😀😀 Grüße\n")
    f = hit(treesearch.search(tree, "grüße"), "u2.txt")
    assert (f["line"], f["col"], f["len"]) == (1, 5, 5)
    f = hit(treesearch.search(tree, "😀 g", names=False), "u2.txt")
    assert (f["col"], f["len"]) == (2, 4)
    write(tree, "nom-😀-finder.txt", "")
    assert hit(treesearch.search(tree, "finder"), "nom-😀-finder.txt")["name"] == [[7, 13]]
    write(tree, "crlf.txt", b"one\r\ntwo finder\r\n", mode="wb")
    f = hit(treesearch.search(tree, "finder"), "crlf.txt")
    assert (f["line"], f["col"], f["len"]) == (2, 4, 6)


def test_a_newer_search_stops_an_older_one(tree):
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 2

    r = treesearch.search(tree, "finder", cancelled=cancelled)
    assert r["cancelled"] is True and len(r["files"]) <= 2


# ---------------------------------------------------------------- through the backend


@pytest.fixture
def backend(project, tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude"
    fake.write_text("#!/bin/bash\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("BRAIN_FRY_CLAUDE", str(fake))
    core = Core()
    port = start_backend(core)
    yield core, port
    core.shutdown()


def request(ctl, req, **msg):
    ctl.send(json.dumps({"req": req, **msg}))
    end = time.time() + 5
    while time.time() < end:
        m = json.loads(ctl.recv(timeout=end - time.time()))
        if m["type"] == "reply" and m["req"] == req:
            return m
    raise AssertionError("no reply")


def test_fs_search_through_the_core(backend, project, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    core, port = backend
    t = actions.new_task(project)
    write(project, "src/a.py", "xyzzy_secret = 1\n")
    write(project, "notes.md", "nothing\n")
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        r = request(ctl, 1, cmd="fs.search", id=t.id, text="XYZZY_secret")["data"]
        assert r == {"files": [{"path": "src/a.py", "name": [], "count": 1, "line": 1, "col": 0, "len": 12}], "more": False}
        # a project's tabs (no task selected) search the same project
        r = request(ctl, 2, cmd="fs.search", id=f"project:{project}", text="XYZZY_SECRET", case=True, content=True)["data"]
        assert r["files"] == []  # match case: the text is lowercase
        r = request(ctl, 3, cmd="fs.search", id=f"project:{project}", text="a.py", content=False)["data"]
        assert [f["path"] for f in r["files"]] == ["src/a.py"]
        bad = request(ctl, 4, cmd="fs.search", id=t.id, text="xyzzy_secret(", regex=True)
        assert not bad["ok"] and "ValueError" in bad["error"] and "regular expression" in bad["error"]
        (tmp_path / "elsewhere").mkdir()
        write(tmp_path / "elsewhere", "xyzzy_secret.txt", "xyzzy_secret\n")
        bad = request(ctl, 5, cmd="fs.search", id=f"project:{tmp_path / 'elsewhere'}", text="xyzzy_secret")
        assert not bad["ok"] and "PermissionError" in bad["error"]
        bad = request(ctl, 6, cmd="fs.search", id="no-such-task", text="xyzzy_secret")
        assert not bad["ok"] and "PermissionError" in bad["error"]
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("brain_fry"))
    assert "search in proj: 1 files" in ours
    assert "xyzzy" not in ours.lower()  # the query is never logged by Brain Fry, at any level


def test_fs_search_does_not_block_the_other_commands(backend, project, monkeypatch):
    core, port = backend
    t = actions.new_task(project)
    real = treesearch.search
    monkeypatch.setattr(treesearch, "search", lambda *a, **k: (time.sleep(1.5), real(*a, **k))[1])
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        ctl.send(json.dumps({"req": 1, "cmd": "fs.search", "id": t.id, "text": "x"}))
        ctl.send(json.dumps({"req": 2, "cmd": "fs.list", "id": t.id}))
        order = []
        end = time.time() + 5
        while len(order) < 2 and time.time() < end:
            m = json.loads(ctl.recv(timeout=end - time.time()))
            if m["type"] == "reply":
                order.append(m["req"])
        assert order == [2, 1]
