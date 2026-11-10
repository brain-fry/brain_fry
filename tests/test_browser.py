"""The browser of 3a, without a window: uBlock's installer, the task field, the backend's
commands, and the helpers of app/browser.py. The native views are checked in the window
(`bf --selftest`, see spikes/browser/)."""

import json
import stat
import zipfile

import pytest
from websockets.sync.client import connect

from brain_fry import actions, paths, ublock
from brain_fry.taskfile import Task, dumps, loads

from test_app_backend import backend, request  # noqa: F401 (fixture)


def fake_ubol(tmp_path, version="2026.1.1", name="uBlock Origin Lite", extra=None):
    """The real manifest localizes its name: __MSG_extName__, in _locales/en/messages.json."""
    z = tmp_path / f"ubol-{version}.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("manifest.json", json.dumps({"name": "__MSG_extName__", "default_locale": "en",
                                                "manifest_version": 3, "version": version}))  # fmt: skip
        f.writestr("_locales/en/messages.json", json.dumps({"extName": {"message": name}}))
        f.writestr("js/background.js", "// bg")
        for path, data in (extra or {}).items():
            f.writestr(path, data)
    return z


def test_install_zip_installs_and_replaces(home, tmp_path):
    assert ublock.installed_version() is None
    z1 = fake_ubol(tmp_path, "1.0")
    assert ublock.install_zip(z1, ublock.sha256(z1)) == "1.0"
    assert (ublock.ext_dir() / "js" / "background.js").exists()
    assert ublock.install_zip(fake_ubol(tmp_path, "2.0")) == "2.0"
    assert ublock.installed_version() == "2.0"
    assert sorted(p.name for p in ublock.ext_dir().parent.iterdir()) == ["ublock-lite"]  # no leftovers


@pytest.mark.parametrize("why", ["hash", "not ubol", "unsafe path"])
def test_install_zip_refuses_and_keeps_the_old_one(home, tmp_path, why):
    ublock.install_zip(fake_ubol(tmp_path, "1.0"))
    if why == "hash":
        z, digest = fake_ubol(tmp_path, "2.0"), "0" * 64
    elif why == "not ubol":
        z, digest = fake_ubol(tmp_path, "2.0", name="Evil"), None
    else:
        z, digest = fake_ubol(tmp_path, "2.0", extra={"../escape.txt": "x"}), None
    with pytest.raises(ValueError):
        ublock.install_zip(z, digest)
    assert ublock.installed_version() == "1.0"
    assert not (tmp_path / "escape.txt").exists()


def test_latest_release_needs_a_published_sha256(monkeypatch):
    release = {"tag_name": "2026.1103.1743", "assets": [
        {"name": "uBOLite_2026.1103.1743.chromium.zip", "browser_download_url": "https://x/c.zip", "digest": "sha256:aa"},
        {"name": "uBOLite_2026.1103.1743.safari.zip", "browser_download_url": "https://x/s.zip", "digest": "sha256:bb"},
    ]}  # fmt: skip

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self, *a):
            return json.dumps(self.data).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ublock.urllib.request, "urlopen", lambda req, timeout: Resp(release))
    assert ublock.latest_release() == {"version": "2026.1103.1743", "name": "uBOLite_2026.1103.1743.safari.zip",
                                       "url": "https://x/s.zip", "sha256": "bb"}  # fmt: skip
    del release["assets"][1]["digest"]
    with pytest.raises(ValueError, match="no SHA-256"):
        ublock.latest_release()


def test_browser_tabs_round_trip_and_set(project):
    t = Task(session_ids=["s1"], project=str(project), cwd=str(project), browser_tabs=["https://example.org/a?b=1#c"])
    assert loads(dumps(t)).browser_tabs == ["https://example.org/a?b=1#c"]
    task = actions.new_task(project)
    actions.set_browser_tabs(task.id, ["https://a.example", "http://localhost:5173/"])
    assert actions.load(task.id).browser_tabs == ["https://a.example", "http://localhost:5173/"]


def test_backend_browser_commands(backend, project):  # noqa: F811
    core, port = backend
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert request(ctl, 1, cmd="browser.tabs.set", id=t.id, urls=["https://example.org/"])["ok"]
        assert actions.load(t.id).browser_tabs == ["https://example.org/"]
        bad = request(ctl, 2, cmd="browser.open", id=t.id, tab="b1", url="https://example.org/")
        assert not bad["ok"] and "only in the Brain Fry window" in bad["error"]  # no window, no host

        class FakeHost:  # stands for app.browser.BrowserHost: the command reaches it
            def __init__(self):
                self.got = []

            def call(self, msg):
                from concurrent.futures import Future

                self.got.append(msg["cmd"])
                f = Future()
                f.set_result({"ok": True})
                return f

        core.browser = FakeHost()
        assert request(ctl, 3, cmd="browser.open", id=t.id, tab="b1", url="https://example.org/")["data"] == {"ok": True}
        ctl.send(json.dumps({"cmd": "browser.place", "tab": "b1", "visible": False}))  # no req: not awaited
        assert request(ctl, 4, cmd="browser.snapshot", tab="b1")["ok"]
        assert core.browser.got == ["browser.open", "browser.place", "browser.snapshot"]


def test_host_helpers(home, tmp_path):
    from Foundation import NSHTTPCookie

    from brain_fry.app import browser

    row = {"name": "sid", "value": "a b=c", "domain": ".example.org", "path": "/x", "secure": True, "httpOnly": True, "sameSite": "lax"}
    c = NSHTTPCookie.cookieWithProperties_(browser.cookie_props(row))
    assert c.isSessionOnly()
    assert browser.cookie_row(c) == row

    a = browser.store_uuid()
    assert a == browser.store_uuid()
    home2 = tmp_path / "other"
    import os

    os.environ["BRAIN_FRY_HOME"] = str(home2)  # restored by the home fixture's monkeypatch
    assert browser.store_uuid() != a  # tests never share the real profile

    f = paths.bf_home() / "browser" / "session_cookies.json"
    browser.write_private(f, "[]")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700

    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "a (1).pdf").write_text("x")
    assert browser.unique_path(tmp_path / "a.pdf").name == "a (2).pdf"
    assert browser.unique_path(tmp_path / "b.pdf").name == "b.pdf"


# ---- my Firefox cookies (firefox.py)

MOZ_COOKIES = """CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '', name TEXT,
  value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER, isSecure INTEGER,
  isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0, sameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0,
  isPartitionedAttributeSet INTEGER DEFAULT 0, updateTime INTEGER)"""


def fake_firefox(home, name="ab12.default-release", rows=()):
    """A Firefox profile as Firefox 13x leaves it while it runs: the latest rows in the WAL.
    Returns the open connection: closing it would move the rows into the database."""
    import sqlite3

    base = home / "Library" / "Application Support" / "Firefox"
    prof = base / "Profiles" / name
    prof.mkdir(parents=True)
    (base / "profiles.ini").write_text(f"[Profile0]\nName=x\nPath=Profiles/old.default\nDefault=1\n\n"
                                       f"[Install2656FF1E876E9973]\nDefault=Profiles/{name}\nLocked=1\n")  # fmt: skip
    db = sqlite3.connect(prof / "cookies.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(MOZ_COOKIES)
    db.commit()
    db.execute("PRAGMA wal_autocheckpoint=0")
    for r in rows:
        db.execute("INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, isSecure, isHttpOnly, sameSite) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", r)  # fmt: skip
    db.commit()
    return db, prof


def test_firefox_reads_the_profile_that_firefox_opens(home):
    import time

    from brain_fry import firefox

    later_ms, later_s = int((time.time() + 86400) * 1000), int(time.time() + 86400)
    db, prof = fake_firefox(home, rows=[
        ("", "sid", "v1", ".example.org", "/", later_ms, 1, 1, 256),  # ms (Firefox 13x), SameSite not set
        ("", "lax", "v2", "a.example.org", "/x", later_s, 0, 0, 1),  # seconds (older Firefox)
        ("", "strict", "v3", "b.example.net", "/", later_ms, 1, 0, 2),
        ("", "old", "v4", ".example.org", "/", int((time.time() - 10) * 1000), 0, 0, 0),  # expired
        ("^partitionKey=%28https%2Cexample.com%29", "p", "v5", ".tracker.example", "/", later_ms, 1, 0, 0),
        ("^userContextId=2", "c", "v6", ".example.org", "/", later_ms, 1, 0, 0),  # a container
    ])  # fmt: skip
    (home / "Library/Application Support/Firefox/Profiles/zz.newer").mkdir()  # newer, but not the one that Firefox opens
    assert (prof / "cookies.sqlite-wal").stat().st_size > 0  # the rows are in the WAL only
    rows, name = firefox.read_cookies()
    db.close()
    assert name == "ab12.default-release"
    by = {r["name"]: r for r in rows}
    assert set(by) == {"sid", "lax", "strict"}
    assert by["sid"] == {"name": "sid", "value": "v1", "domain": ".example.org", "path": "/", "secure": True, "httpOnly": True,
                         "sameSite": "", "expires": later_ms / 1000}  # fmt: skip
    assert by["lax"]["sameSite"] == "lax" and by["lax"]["expires"] == later_s and by["lax"]["path"] == "/x"
    assert by["strict"]["sameSite"] == "strict"


def test_firefox_without_profiles_ini_takes_the_newest_cookies(home):
    import os

    from brain_fry import firefox

    assert firefox.profile_dir() is None
    with pytest.raises(FileNotFoundError):
        firefox.read_cookies()
    db, prof = fake_firefox(home, name="a.default")
    db.close()
    (prof.parent.parent / "profiles.ini").unlink()
    newer = prof.parent / "b.other"
    newer.mkdir()
    (newer / "cookies.sqlite").write_bytes((prof / "cookies.sqlite").read_bytes())
    os.utime(prof / "cookies.sqlite", (1, 1))
    assert firefox.profile_dir() == newer


def test_firefox_sites_and_last_import(home):
    from brain_fry import firefox

    hosts = {"accounts.google.com", "example.org"}
    assert firefox.covers("google.com", hosts) and firefox.covers("example.org", hosts)
    assert not firefox.covers("le.com", {"google.com"}) and not firefox.covers("www.example.org", hosts)
    assert firefox.last_import() is None
    firefox.import_file().parent.mkdir(parents=True)
    firefox.import_file().write_text('{"time": 1, "cookies": 2, "sites": 1}')
    assert firefox.last_import()["cookies"] == 2


def test_cookie_with_expiry(home):
    import time

    from Foundation import NSHTTPCookie

    from brain_fry.app import browser

    t = float(int(time.time()) + 86400)  # CFNetwork caps a lifetime at 400 days
    c = NSHTTPCookie.cookieWithProperties_(browser.cookie_props({"name": "a", "value": "1", "domain": "example.org", "expires": t}))
    assert not c.isSessionOnly() and c.expiresDate().timeIntervalSince1970() == t


def test_backend_cookie_info(backend, home):  # noqa: F811
    from brain_fry import firefox

    core, port = backend
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        assert request(ctl, 1, cmd="browser.cookies.info")["data"] == {"last": None}
        firefox.import_file().parent.mkdir(parents=True, exist_ok=True)
        firefox.import_file().write_text('{"time": 5, "cookies": 3, "sites": 2}')
        assert request(ctl, 2, cmd="browser.cookies.info")["data"]["last"]["cookies"] == 3


def test_browser_tabs_are_logged_as_counts(project, caplog):
    import logging

    task = actions.new_task(project)
    with caplog.at_level(logging.INFO, logger="brain_fry.actions"):
        actions.set_browser_tabs(task.id, ["https://secret.example/path"])
    assert "browser_tabs 0 -> 1" in caplog.text and "secret" not in caplog.text
