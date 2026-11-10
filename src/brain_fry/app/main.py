"""`bf` without arguments: the window."""

from __future__ import annotations

import fcntl
import json
import logging
import platform
import subprocess
import sys
import threading
import time
from importlib import resources

from .. import logs, paths, projects
from ..util import child_env, claude_bin
from .core import Core
from .server import start_backend

log = logging.getLogger(__name__)


def _mac_identity() -> None:
    """Dock icon and app menu name ("Python" otherwise). pywebview's `icon` option is
    GTK/Qt only; on macOS it comes from AppKit. Main thread, before webview.start()."""
    try:
        from AppKit import NSApplication, NSImage  # noqa: PLC0415 (pyobjc, a pywebview dependency)
        from Foundation import NSBundle  # noqa: PLC0415

        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Brain Fry"
        icon = resources.files("brain_fry") / "web" / "dock_icon.png"  # transparent, see frontend/dock_icon.py
        image = NSImage.alloc().initWithContentsOfFile_(str(icon))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        log.debug("could not set the Dock icon", exc_info=True)


def _file_menu(get_window) -> list:
    """The File menu of the menu bar. Its items call the page; pywebview runs each one in a
    thread of its own. No key equivalents: a key of the menu bar would never reach the page."""
    from webview.menu import Menu, MenuAction  # noqa: PLC0415

    def refresh_explorer():
        log.info("menu: File > Refresh Explorer")
        try:
            get_window().evaluate_js("menuAction('refresh')")
        except Exception:
            log.warning("menu action failed", exc_info=True)

    return [Menu("File", [MenuAction("Refresh Explorer", refresh_explorer)])]


def _file_menu_first() -> None:
    """pywebview appends its custom menus after Edit and View; on macOS, File comes right after
    the app menu. pywebview builds the menu bar again when the window becomes key, so the reorder
    goes into its builder. Internal to pywebview 6.2: if it changes, File stays at the end."""
    try:
        from webview.platforms.cocoa import BrowserView  # noqa: PLC0415

        build = BrowserView._recreate_menus

        def recreate(self, user_menu):
            bar = build(self, user_menu)
            i = bar.indexOfItemWithTitle_("File")
            if i > 1:
                item = bar.itemAtIndex_(i)
                bar.removeItemAtIndex_(i)
                bar.insertItem_atIndex_(item, 1)
            return bar

        BrowserView._recreate_menus = recreate
    except Exception:
        log.debug("could not move the File menu", exc_info=True)


def _menu_titles() -> str:
    """The menu bar, for the selftest: 'Brain Fry, File (Refresh Explorer), Edit (...), ...'."""
    try:
        from AppKit import NSApp  # noqa: PLC0415
        from PyObjCTools import AppHelper  # noqa: PLC0415

        done, out = threading.Event(), []

        def read():  # main thread: AppKit
            for item in NSApp().mainMenu().itemArray():
                sub = item.submenu()
                kids = [k.title() for k in sub.itemArray() if not k.isSeparatorItem()] if sub else []
                out.append(f"{item.title() or sub.title()} ({', '.join(kids)})")
            done.set()

        AppHelper.callAfter(read)
        done.wait(5)
        return ", ".join(out)
    except Exception as e:
        return f"? ({e})"


def _single_instance():
    paths.bf_home().mkdir(parents=True, exist_ok=True)
    f = open(paths.bf_home() / "bf.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Brain Fry is already running.")
    return f  # keep the file object alive: closing it releases the lock


SELFTEST_JS = """
(() => {
  const sel = S.selected && S.terms.get(S.selected);
  const lines = [];
  if (sel) { const b = sel.term.buffer.active;
    for (let i = 0; i < b.length; i++) lines.push(b.getLine(i).translateToString(true)); }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return JSON.stringify({ui: document.body.innerText, terminal: lines.join('\\n'), errors: window.__errors,
    debug: {href: location.href, ready: document.readyState, xterm: typeof Terminal, state: !!S.state,
            splash: !!document.getElementById('splash'), size: [innerWidth, innerHeight]},
    browser: [...B.tabs.values()].map((t) => ({key: t.key, task: t.tid.slice(0, 8), url: t.url, title: t.title,
      loading: t.loading, error: t.error, active: activeTab() === t, placed: B.placed.get(t.key) || null}))});
})()
"""


def _screenshot(window, path: str) -> None:
    """The page as WebKit renders it (WKWebView.takeSnapshot). Unlike `screencapture`, it
    needs no Screen Recording permission. The browser views of 3a (app/browser.py) are other
    WKWebViews, laid over the page: each visible one is snapshotted too and drawn at its frame."""
    from AppKit import NSBitmapImageRep, NSImage, NSMakeRect  # noqa: PLC0415
    from PyObjCTools import AppHelper  # noqa: PLC0415
    from WebKit import WKWebView  # noqa: PLC0415

    done = threading.Event()
    shots: dict = {}

    def write(ui) -> None:
        image = shots.pop("ui")
        if shots:  # browser views: compose them over the page
            size = ui.bounds().size
            canvas = NSImage.alloc().initWithSize_(size)
            canvas.lockFocus()
            image.drawInRect_(NSMakeRect(0, 0, size.width, size.height))
            for sub, img in shots.values():
                f = sub.frame()
                y = size.height - f.origin.y - f.size.height if ui.isFlipped() else f.origin.y
                img.drawInRect_(NSMakeRect(f.origin.x, y, f.size.width, f.size.height))
            canvas.unlockFocus()
            image = canvas
        rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        rep.representationUsingType_properties_(4, None).writeToFile_atomically_(path, True)  # 4: PNG
        print(f"=== screenshot: {path}" + (f" (with {len(shots)} browser view(s))" if shots else ""))
        done.set()

    def handler(image, error):
        if image is None:
            print(f"=== screenshot failed: {error}")
            done.set()
            return
        shots["ui"] = image
        ui = find(window.native.contentView())
        subs = [s for s in ui.subviews() if s.isKindOfClass_(WKWebView) and not s.isHidden()]
        if not subs:
            return write(ui)
        left = [len(subs)]

        def sub_done(sub, img):
            if img is not None:
                shots[id(sub)] = (sub, img)
            left[0] -= 1
            if not left[0]:
                write(ui)

        for s in subs:
            s.takeSnapshotWithConfiguration_completionHandler_(None, lambda img, err, s=s: sub_done(s, img))

    def find(view):
        if view.isKindOfClass_(WKWebView):
            return view
        for sub in view.subviews():
            if found := find(sub):
                return found
        return None

    def go():
        webview = find(window.native.contentView())
        if webview is None:
            print("=== screenshot failed: no WKWebView")
            done.set()
        else:
            webview.takeSnapshotWithConfiguration_completionHandler_(None, handler)

    AppHelper.callAfter(go)
    done.wait(5)


def _selftest(window, seconds: float, new_in: str | None, shots: str | None, js: str | None) -> None:
    """For Claude to check the window without a human: optionally start a task or run
    some JS, wait, print the page text and the selected terminal, close."""
    time.sleep(0.5)
    if shots:
        _screenshot(window, f"{shots}/1_splash.png")
    time.sleep(2.5)
    if new_in:
        window.evaluate_js(f"newTask({json.dumps(new_in)})")
    if js:
        window.evaluate_js(js)
    time.sleep(seconds)
    if shots:
        _screenshot(window, f"{shots}/2_app.png")
    out = window.evaluate_js(SELFTEST_JS)
    info = json.loads(out) if isinstance(out, str) else out
    print("=== menu bar:", _menu_titles())
    print("=== JS errors:", info["errors"])
    print("=== debug:", info.get("debug"))
    print("=== browser:", json.dumps(info.get("browser"), indent=1))
    print("=== page text:\n" + info["ui"])
    print("=== terminal:\n" + info["terminal"])
    window.destroy()


def run(debug: bool = False, browser: bool = False, selftest: float | None = None,
        selftest_new: str | None = None, selftest_shots: str | None = None,
        selftest_js: str | None = None) -> int:  # fmt: skip
    logfile = logs.setup("app", debug)
    lock = _single_instance()  # noqa: F841
    _log_start(logfile)
    core = Core()
    url = f"http://127.0.0.1:{start_backend(core)}/"
    log.info("backend on %s", url)
    try:
        if browser:
            print(f"open {url}   (Ctrl-C to quit)")
            threading.Event().wait()
        import webview  # noqa: PLC0415 (Cocoa only after the backend thread started)

        _mac_identity()
        _file_menu_first()
        window = webview.create_window(
            "Brain Fry", url, width=1600, height=1000, min_size=(900, 500),
            background_color="#040510",  # the splash color: no white flash before the page paints
            menu=_file_menu(lambda: window),
        )  # fmt: skip

        def on_closing():
            n = core.working_count()
            if n and selftest is None:
                ok = window.create_confirmation_dialog(
                    "Quit Brain Fry?", f"{n} task(s) are working. Quitting ends their turn; --resume brings the conversation back."
                )
                return bool(ok)
            return True

        window.events.closing += on_closing
        from .browser import BrowserHost  # noqa: PLC0415 (WebKit: only in the window)

        core.browser = BrowserHost(core, window)  # the browser of 3a; sets itself up at its first command
        if selftest is not None:
            webview.start(_selftest, (window, selftest, selftest_new, selftest_shots, selftest_js), debug=debug)
        else:
            webview.start(debug=debug)
    except KeyboardInterrupt:
        log.info("interrupted (Ctrl-C)")
    except Exception:
        log.exception("the window failed")
        raise
    finally:
        log.info("window closed: quitting")
        if core.browser is not None:
            core.browser.shutdown()  # main thread: saves the session cookies
        core.shutdown()
        log.info("quit")
    return 0


def _version(package: str) -> str:
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version(package)
    except Exception:
        return "?"


def _log_start(logfile) -> None:
    log.info("start: Brain Fry %s, Python %s, pywebview %s, macOS %s, home %s, log %s", _version("brain-fry"),
             platform.python_version(), _version("pywebview"), platform.mac_ver()[0], paths.bf_home(), logfile)  # fmt: skip
    log.info("projects: %s", ", ".join(str(p) for p in projects.project_paths()) or "none")

    def claude_version() -> None:  # in a thread: `claude --version` takes a moment
        try:
            r = subprocess.run([claude_bin(), "--version"], capture_output=True, text=True, timeout=20, env=child_env())
            log.info("claude: %s (%s)", (r.stdout or r.stderr).strip()[:100], claude_bin())
        except Exception as e:
            log.warning("claude --version failed: %s", e)

    threading.Thread(target=claude_version, name="claude-version", daemon=True).start()
