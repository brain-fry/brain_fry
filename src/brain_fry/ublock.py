"""uBlock Origin Lite for the browser of 3a: its Safari build, from the official GitHub release,
unpacked in ~/.brain_fry/extensions/ublock-lite/. `bf ublock` installs or updates it, and
`bf install` runs it too. See DESIGN_DETAILS.md, "Browser (3a)".

A download is checked against the SHA-256 that GitHub publishes for the release asset (the
`digest` field of the releases API); a file without one, or with another hash, is refused.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import paths

RELEASES_API = "https://api.github.com/repos/uBlockOrigin/uBOL-home/releases/latest"
ASSET_SUFFIX = ".safari.zip"
USER_AGENT = "brain_fry"  # GitHub's API requires one; generic on purpose


def ext_dir() -> Path:
    return paths.bf_home() / "extensions" / "ublock-lite"


def installed_version() -> str | None:
    try:
        return json.loads((ext_dir() / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def install_zip(zip_path: Path, expected_sha256: str | None = None) -> str:
    """Unpacks a uBOL Safari zip into ext_dir(), replacing the old one. Returns its version.
    The old folder stays until the new one is complete: a failure leaves it untouched."""
    zip_path = Path(zip_path)
    if expected_sha256 and sha256(zip_path) != expected_sha256.lower():
        raise ValueError(f"{zip_path.name}: SHA-256 differs from the published one, refused")
    dest = ext_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".ublock-", dir=dest.parent))
    try:
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():  # no absolute paths, no '..': nothing lands outside tmp
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ValueError(f"{zip_path.name}: unsafe path {name!r} in the archive")
            z.extractall(tmp)
        manifest = json.loads((tmp / "manifest.json").read_text())
        name = _ext_name(tmp, manifest)
        if "uBlock" not in name or manifest.get("manifest_version") != 3:
            raise ValueError(f"{zip_path.name}: not a uBlock Origin Lite build (name {name!r})")
        old = dest.with_name(dest.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            dest.rename(old)
        tmp.rename(dest)
        shutil.rmtree(old, ignore_errors=True)
        return str(manifest["version"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ext_name(root: Path, manifest: dict) -> str:
    """The extension's name; a localized one (__MSG_extName__) is read from its default locale."""
    name = str(manifest.get("name") or "")
    if name.startswith("__MSG_") and name.endswith("__"):
        locale = manifest.get("default_locale") or "en"
        try:
            messages = json.loads((root / "_locales" / locale / "messages.json").read_text())
            return str(messages[name[6:-2]]["message"])
        except (OSError, ValueError, KeyError):
            return name
    return name


def _get(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def latest_release() -> dict:
    """{version, name, url, sha256} of the Safari build of the latest release."""
    with urllib.request.urlopen(_get(RELEASES_API, "application/vnd.github+json"), timeout=30) as r:
        rel = json.load(r)
    for a in rel.get("assets", []):
        if a["name"].endswith(ASSET_SUFFIX):
            digest = a.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise ValueError(f"{a['name']}: GitHub publishes no SHA-256 for it, refused")
            return {"version": rel.get("tag_name"), "name": a["name"], "url": a["browser_download_url"],
                    "sha256": digest.split(":", 1)[1]}  # fmt: skip
    raise ValueError(f"no {ASSET_SUFFIX} asset in the release {rel.get('tag_name')}")


def update() -> str:
    """Installs the latest release unless it is installed already. Returns one line for the user."""
    rel = latest_release()
    if installed_version() == rel["version"]:
        return f"uBlock Origin Lite {rel['version']} is up to date"
    with tempfile.TemporaryDirectory() as d:
        z = Path(d) / rel["name"]
        with urllib.request.urlopen(_get(rel["url"], "application/octet-stream"), timeout=120) as r, open(z, "wb") as f:
            shutil.copyfileobj(r, f)
        version = install_zip(z, rel["sha256"])
    return f"installed uBlock Origin Lite {version} in {ext_dir()} (restart Brain Fry to load it)"
