"""Reads my Firefox cookies, for the browser of 3a ("Overwrite my cookies").

Only the persistent cookies of the default container: Firefox keeps session cookies in
memory (and in its session store, compressed), not in cookies.sqlite. Partitioned cookies
(third-party, `^partitionKey=`) and container cookies (`^userContextId=`) are skipped.
"""

from __future__ import annotations

import configparser
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import paths

# Firefox's sameSite column: 0 None, 1 Lax, 2 Strict, 256 not set (Firefox 13x and later).
SAME_SITE = {1: "lax", 2: "strict"}


def import_file() -> Path:
    """The last copy into Brain Fry's browser (time, counts). Its absence starts a copy at the
    first start of the browser."""
    return paths.bf_home() / "browser" / "firefox_import.json"


def last_import() -> dict | None:
    try:
        return json.loads(import_file().read_text())
    except (OSError, ValueError):
        return None


def base_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Firefox"


def profile_dir() -> Path | None:
    """The profile that Firefox opens (its Install section in profiles.ini), else the profile
    whose cookies changed last."""
    base = base_dir()
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(base / "profiles.ini")
    except configparser.Error:
        pass
    for section in ini.sections():
        if section.startswith("Install") and ini.get(section, "Default", fallback=""):
            p = _resolve(base, ini.get(section, "Default"))
            if (p / "cookies.sqlite").exists():
                return p
    found = sorted(base.glob("Profiles/*/cookies.sqlite"), key=lambda f: f.stat().st_mtime, reverse=True)
    return found[0].parent if found else None


def _resolve(base: Path, path: str) -> Path:
    return Path(path) if path.startswith("/") else base / path


def read_cookies(profile: Path | None = None) -> tuple[list[dict], str]:
    """The cookies as rows for app/browser.py's cookie_props, and the profile's folder name.
    Reads a copy: Firefox locks its database while it runs. The copy and its WAL (the latest
    writes) go to a private temp folder, deleted at once."""
    profile = profile or profile_dir()
    if profile is None:
        raise FileNotFoundError(f"no Firefox profile with cookies in {base_dir()}")
    with tempfile.TemporaryDirectory(prefix="bf-firefox-") as tmp:
        for name in ("cookies.sqlite", "cookies.sqlite-wal"):
            if (profile / name).exists():
                shutil.copy2(profile / name, Path(tmp) / name)
        db = sqlite3.connect(Path(tmp) / "cookies.sqlite")
        try:
            rows = db.execute("SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies "
                              "WHERE originAttributes = ''").fetchall()  # fmt: skip
        finally:
            db.close()
    now, out = time.time(), []
    for name, value, host, path, expiry, secure, http_only, same_site in rows:
        expires = expiry / 1000 if expiry > 1e11 else float(expiry)  # milliseconds since Firefox 13x, seconds before
        if not host or expires <= now:
            continue
        out.append({"name": name, "value": value or "", "domain": host, "path": path or "/", "secure": bool(secure),
                    "httpOnly": bool(http_only), "sameSite": SAME_SITE.get(same_site, ""), "expires": expires})  # fmt: skip
    return out, profile.name


def covers(site: str, hosts: set[str]) -> bool:
    """Whether a site of WebKit's store (a registrable domain: google.com) is one of the hosts
    that Firefox has cookies for (accounts.google.com, google.com, without the leading dot)."""
    return any(h == site or h.endswith("." + site) for h in hosts)
