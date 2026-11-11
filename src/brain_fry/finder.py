"""The Finder tabs of column 3: a copy of macOS Finder's column view (DESIGN_DETAILS.md, "Finder tab").

Unlike files.py, paths are absolute and may be anywhere on the disk: a Finder tab starts at the
project folder and can go up to `/`. What Finder shows comes from the calls that Finder uses:
the order of names (`localizedStandardCompare:`), the icons (NSWorkspace), the kinds
("Python script"), sizes and dates in the format of the system, the free space ("important
usage", which counts purgeable space), the hidden flag of macOS (UF_HIDDEN: `/usr`, `~/Library`)
and packages (an app is a file, not a folder). Thumbnails come from Quick Look (`qlmanage -t`).
AppKit is imported in the functions: it is slow to load, and only the window needs it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import stat
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from . import files

log = logging.getLogger(__name__)

ICON_SIZES = (32, 64, 256)  # pixels: 16 px rows and the 14 px path bar at 2x, the preview at 2x
THUMB_PX = 512
THUMB_MAX_BYTES = 200 * 1024 * 1024
THUMB_TIMEOUT = 3  # s: qlmanage never returns for some files (a .bin of random bytes); the preview keeps the icon
_EXT = re.compile(r"[a-z0-9_+-]{1,16}")
# Folders with an icon of their own in Finder: their icon is drawn from their path
_HOME = Path.home()
SPECIAL = {str(p) for p in [_HOME, *(_HOME / n for n in ("Desktop", "Documents", "Downloads", "Library", "Movies", "Music",
                                                            "Pictures", "Public", "Applications", "Sites"))]}  # fmt: skip
SPECIAL |= {"/", "/Applications", "/Library", "/System", "/Users", "/Users/Shared", "/Volumes"}  # "/": the disk

_icon_paths: dict[str, str] = {}  # icon key -> a path that has that icon (see icon_key)
_icon_cache: dict[tuple[str, int], bytes] = {}
_thumbs: OrderedDict[tuple[str, int, int], str | None] = OrderedDict()  # (path, mtime_ns, size) -> data URL
_lock = threading.Lock()


def absolute(path: str) -> Path:
    """A path from the front end: absolute, normalized (no `..`), symlinks not followed."""
    if not path or not path.startswith("/") or "\0" in path:
        raise ValueError(f"not an absolute path: {path!r}")
    return Path(os.path.normpath(path))


# ---------------------------------------------------------------- listing

def _sorted_names(names: list[str]) -> list[str]:
    """Finder's order: `localizedStandardCompare:` (case-insensitive, digits as numbers), in one
    native call. Folders are mixed with files, as in Finder without "Keep folders on top"."""
    from Foundation import NSArray  # noqa: PLC0415

    return [str(n) for n in NSArray.arrayWithArray_(names).sortedArrayUsingSelector_("localizedStandardCompare:")]


def _is_package(p: Path) -> bool:
    """An app, a bundle, a Photos library: Finder shows it as a file. Only folders whose name
    has an extension can be one; asking for every folder would be slow in big folders."""
    if "." not in p.name.lstrip("."):
        return False
    from Foundation import NSURL  # noqa: PLC0415

    ok, value, _ = NSURL.fileURLWithPath_(str(p)).getResourceValue_forKey_error_(None, "NSURLIsPackageKey", None)
    return bool(ok and value)


def icon_key(p: Path, is_dir: bool, package: bool) -> str:
    """The name of an icon: one per extension for files, one for plain folders, one per path for
    apps, packages and the folders that Finder draws with their own icon. The front end asks
    `/icon/<key>`; the key remembers a path to draw it from."""
    if package or (is_dir and str(p) in SPECIAL):
        key = "p-" + hashlib.sha1(str(p).encode()).hexdigest()[:16]
    elif is_dir:
        key = "folder"
    else:
        ext = p.suffix[1:].lower() if p.suffix else ""
        if ext and _EXT.fullmatch(ext):
            key = f"e-{ext}"
        else:
            try:
                key = "f-exec" if os.access(p, os.X_OK) else "f-plain"
            except OSError:
                key = "f-plain"
    with _lock:
        _icon_paths.setdefault(key, str(p))
    return key


def list_folder(path: str) -> dict:
    """A column: every entry of the folder, hidden ones flagged (Renaud's Finder shows them,
    dimmed). A folder that I may not read (privacy settings of macOS) comes back empty, as
    Finder shows it."""
    d = absolute(path)
    if not d.is_dir():
        raise NotADirectoryError(f"not a folder: {d}")
    entries = []
    try:
        scan = list(os.scandir(d))
    except PermissionError:
        scan = []
    by_name = {}
    for e in scan:
        try:
            st = e.stat(follow_symlinks=False)
            is_dir = e.is_dir()  # follows a symlink: a link to a folder opens as a folder, as in Finder
        except OSError:
            continue
        p = d / e.name
        package = is_dir and _is_package(p)
        hidden = e.name.startswith(".") or bool(getattr(st, "st_flags", 0) & stat.UF_HIDDEN)
        by_name[e.name] = {"name": e.name, "dir": is_dir and not package, "pkg": package, "hidden": hidden,
                           "icon": icon_key(p, is_dir, package)}  # fmt: skip
    for name in _sorted_names(list(by_name)):
        entries.append(by_name[name])
    return {"path": str(d), "entries": entries, "free": free_space(d), "crumbs": crumbs(d)}


def crumbs(d: Path) -> list[dict]:
    """The folder and its parents, from the disk down, for the path bar: {path, name, icon}."""
    out = []
    for p in [*reversed(d.parents), d]:
        out.append({"path": str(p), "name": p.name or volume_name(str(p)), "icon": icon_key(p, True, False)})
    return out


def free_space(d: Path) -> str:
    """As the status bar of Finder: "393.58 GB available" counts purgeable space."""
    from Foundation import NSURL, NSByteCountFormatter  # noqa: PLC0415

    try:
        ok, n, _ = NSURL.fileURLWithPath_(str(d)).getResourceValue_forKey_error_(
            None, "NSURLVolumeAvailableCapacityForImportantUsageKey", None)  # fmt: skip
        if not ok or n is None:
            st = os.statvfs(d)
            n = st.f_bavail * st.f_frsize
        return str(NSByteCountFormatter.stringFromByteCount_countStyle_(int(n), 0))  # 0: file style, as Finder
    except OSError:
        return ""


def volume_name(path: str = "/") -> str:
    """The name that Finder gives the disk: "Macintosh HD"."""
    from Foundation import NSURL  # noqa: PLC0415

    ok, name, _ = NSURL.fileURLWithPath_(path).getResourceValue_forKey_error_(None, "NSURLVolumeLocalizedNameKey", None)
    return str(name) if ok and name else "/"


# ---------------------------------------------------------------- the preview column

def _fmt_size(n: int) -> str:
    from Foundation import NSByteCountFormatter  # noqa: PLC0415

    return str(NSByteCountFormatter.stringFromByteCount_countStyle_(n, 0))


def _fmt_date(ts: float | None) -> str | None:
    """In the format of the system (Finder's): "11 November 2026 at 6:03 PM"."""
    if not ts:
        return None
    from Foundation import NSDate, NSDateFormatter  # noqa: PLC0415

    f = NSDateFormatter.alloc().init()
    f.setDateStyle_(3)  # long
    f.setTimeStyle_(1)  # short
    return str(f.stringFromDate_(NSDate.dateWithTimeIntervalSince1970_(ts)))


def _last_opened(p: Path) -> float | None:
    """Finder's "Last opened": Spotlight's kMDItemLastUsedDate (not the access time)."""
    try:
        r = subprocess.run(["mdls", "-raw", "-name", "kMDItemLastUsedDate", str(p)], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    from datetime import datetime  # noqa: PLC0415

    text = " ".join(r.stdout.replace("\u202f", " ").split())  # "2026-11-10 5:37:24 PM +0000", or 24 h
    for fmt in ("%Y-%m-%d %I:%M:%S %p %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:  # "(null)": never opened, or not indexed
            pass
    return None


def info(path: str) -> dict:
    """What the preview column shows for a file: kind, size, dates."""
    from Foundation import NSURL  # noqa: PLC0415

    p = absolute(path)
    st = p.stat()
    ok, v, _ = NSURL.fileURLWithPath_(str(p)).getResourceValue_forKey_error_(None, "NSURLLocalizedTypeDescriptionKey", None)
    kind = str(v) if ok and v else None
    is_dir = p.is_dir()
    package = is_dir and _is_package(p)
    return {
        "path": str(p), "name": p.name or volume_name(str(p)), "kind": kind or ("Folder" if is_dir else "Document"),
        "size": None if is_dir and not package else _fmt_size(st.st_size if not package else _tree_size(p)),
        "created": _fmt_date(getattr(st, "st_birthtime", None)), "modified": _fmt_date(st.st_mtime),
        "opened": _fmt_date(_last_opened(p)), "icon": icon_key(p, is_dir, package),
    }  # fmt: skip


def _tree_size(p: Path, limit: int = 20000) -> int:
    """The size of a package (an app), as Finder adds it up; stops after `limit` files."""
    total = 0
    for i, (root, _, names) in enumerate(os.walk(p)):
        for n in names:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
        if i > limit:
            break
    return total


def thumbnail(path: str) -> str | None:
    """Quick Look's thumbnail, as the preview column of Finder shows it (an image, the first page
    of a PDF, the text of a script), as a data URL; None when Quick Look has none. Remembered per
    version of the file."""
    p = absolute(path)
    st = p.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > THUMB_MAX_BYTES:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _lock:
        if key in _thumbs:
            _thumbs.move_to_end(key)
            return _thumbs[key]
    url = None
    with tempfile.TemporaryDirectory(prefix="bf-thumb-") as out:
        try:
            subprocess.run(["qlmanage", "-t", "-s", str(THUMB_PX), "-o", out, str(p)], capture_output=True, timeout=THUMB_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            log.info("thumbnail of %s: Quick Look gave none within %d s", p.name, THUMB_TIMEOUT)
        made = [f for f in os.listdir(out) if f.endswith(".png")]
        if made:
            data = Path(out, made[0]).read_bytes()
            url = "data:image/png;base64," + base64.b64encode(data).decode()
    with _lock:
        _thumbs[key] = url
        while len(_thumbs) > 64:
            _thumbs.popitem(last=False)
    return url


def icon_png(key: str, px: int) -> bytes | None:
    """The icon `key` (see icon_key) as a PNG of px x px pixels, or None for an unknown key."""
    if px not in ICON_SIZES:
        return None
    with _lock:
        if (key, px) in _icon_cache:
            return _icon_cache[(key, px)]
        sample = _icon_paths.get(key)
    if sample is None and key != "folder" and not (key.startswith("e-") and _EXT.fullmatch(key[2:])):
        return None
    from AppKit import NSBitmapImageRep, NSCompositingOperationCopy, NSGraphicsContext, NSPNGFileType, NSWorkspace  # noqa: PLC0415
    from Foundation import NSMakeRect, NSZeroRect  # noqa: PLC0415

    ws = NSWorkspace.sharedWorkspace()
    if key == "folder":
        img = ws.iconForFileType_("public.folder")
    elif key.startswith("e-"):
        img = ws.iconForFileType_(key[2:])
    else:
        img = ws.iconForFile_(sample)
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0)  # fmt: skip
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        img.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, px, px), NSZeroRect, NSCompositingOperationCopy, 1.0)
    finally:
        NSGraphicsContext.restoreGraphicsState()
    data = bytes(rep.representationUsingType_properties_(NSPNGFileType, {}))
    with _lock:
        _icon_cache[(key, px)] = data
    return data


# ---------------------------------------------------------------- the right-click menu (as 2a's)
# files.py does the work: an entry is (its folder, its name), so its folder plays the project.

def _split(path: str) -> tuple[Path, str]:
    p = absolute(path)
    if p == Path("/"):
        raise PermissionError("the disk itself is no entry")
    return p.parent, p.name


def rename(path: str, name: str) -> str:
    folder, old = _split(path)
    return str(folder / files.rename(folder, old, name))


def trash(path: str) -> None:
    folder, name = _split(path)
    files._move_to_trash(files.entry(folder, name))


def paste(paths: list[str], dest: str, cut: bool) -> list[list[str]]:
    """Copies, or moves, the entries `paths` into the folder `dest`: [old path, new path] pairs.
    The rules of files.paste: a copy takes a free name ("a copy.py"), a move never replaces."""
    d = absolute(dest)
    pairs = []
    for path in paths:
        folder, name = _split(path)
        for old, new in files.paste(folder, [name], d, "", cut):
            pairs.append([str(folder / old), str(d / new)])
    return pairs


def copy_path(path: str, project: str | None, relative: bool) -> str:
    """The path for "Copy path", or relative to the project for "Copy relative path" (only
    inside it)."""
    p = absolute(path)
    if not relative:
        text = str(p)
    else:
        root = Path(project or "/")
        if p != root and root not in p.parents:
            raise PermissionError(f"{p.name} is not in the project: it has no relative path")
        text = p.relative_to(root).as_posix() if p != root else "."
    files.copy_to_clipboard(text)
    return text
