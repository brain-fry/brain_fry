"""`bf` without arguments: the window."""

from __future__ import annotations

import fcntl
import json
import logging
import platform
import signal
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
QUIT_NAMES = 5  # the files named in the question at quit, then "and N more"
_probes: list[subprocess.Popen] = []  # `claude --version` (_log_start): ended at quit, else it outlives the app


def _mac_identity() -> None:
    """Dock icon and app menu name ("Python" otherwise), through AppKit. Main thread, before
    webview.start(). The app must be a regular app before its icon is set: an icon set earlier
    never reaches the Dock or Cmd+Tab, which show the blank document of the bare python binary
    (pywebview makes the app regular only when it loads its Cocoa module, after this call)."""
    try:
        from AppKit import NSApplication, NSImage  # noqa: PLC0415 (pyobjc, a pywebview dependency)
        from Foundation import NSBundle  # noqa: PLC0415

        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Brain Fry"
        icon = resources.files("brain_fry") / "web" / "dock_icon.png"  # transparent, see frontend/dock_icon.py
        image = NSImage.alloc().initWithContentsOfFile_(str(icon))
        if image is not None:
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular, as pywebview does
            app.setApplicationIconImage_(image)
    except Exception:
        log.debug("could not set the Dock icon", exc_info=True)


def _file_menu(get_window) -> list:
    """The File menu of the menu bar. Its items call the page; pywebview runs each one in a
    thread of its own. No key equivalents: a key of the menu bar would never reach the page."""
    from webview import FileDialog  # noqa: PLC0415
    from webview.menu import Menu, MenuAction  # noqa: PLC0415

    def add_project():
        """macOS's folder dialog (with its New Folder button), then the page runs bf init on
        the folder and switches to it (`addProject` in app.js)."""
        log.info("menu: File > Add New Project...")
        try:
            window = get_window()
            chosen = window.create_file_dialog(FileDialog.FOLDER)
            if chosen:
                window.evaluate_js(f"menuAction('project', {json.dumps(chosen[0])})")
        except Exception:
            log.warning("menu action failed", exc_info=True)

    def open_file():
        """The page asks the backend for macOS's open panel in the project on screen
        (`openFromPanel` in files.js, `fs.openpanel`), and opens the files in the pane that I use."""
        log.info("menu: File > Open...")
        try:
            get_window().evaluate_js("menuAction('open')")
        except Exception:
            log.warning("menu action failed", exc_info=True)

    def refresh_explorer():
        log.info("menu: File > Refresh Explorer")
        try:
            get_window().evaluate_js("menuAction('refresh')")
        except Exception:
            log.warning("menu action failed", exc_info=True)

    return [Menu("File", [MenuAction("Open...", open_file), MenuAction("Add New Project...", add_project),
                          MenuAction("Refresh Explorer", refresh_explorer)])]  # fmt: skip


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


def _dark_titlebar(window) -> None:
    """The title bar in the header's color (--panel of app.css), so that the two join; macOS
    paints it light gray while the system is in light mode. The dark appearance goes on the
    title bar's view only: the page, the dialogs and the browser of 3a keep the system's. pywebview
    paints that view with the system's window color: internal to pywebview 6.2 (its view
    hierarchy); if it changes, the bar stays gray. Runs from `before_show`, in a thread."""
    from AppKit import NSAppearance, NSAppearanceNameDarkAqua, NSColor  # noqa: PLC0415
    from PyObjCTools import AppHelper  # noqa: PLC0415

    def apply():  # main thread: AppKit
        try:
            w = window.native
            bar = w.contentView().superview().subviews().lastObject()  # NSTitlebarContainerView
            if bar.className() != "NSTitlebarContainerView":
                log.debug("title bar: unexpected view %s", bar.className())
                return
            bar.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua))
            w.setTitlebarAppearsTransparent_(True)
            bar.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0x23 / 255, 0x24 / 255, 0x28 / 255, 1))
        except Exception:
            log.debug("could not darken the title bar", exc_info=True)

    AppHelper.callAfter(apply)


def _fill_screen(window) -> None:
    """The window fills its screen without going full screen: the screen's visible frame (menu
    bar and Dock excluded), as Window > Fill does. pywebview's `maximized=True` takes the whole
    screen, under the menu bar. Runs from `before_show`, in a thread."""
    from PyObjCTools import AppHelper  # noqa: PLC0415

    def apply():  # main thread: AppKit
        try:
            w = window.native
            w.setFrame_display_(w.screen().visibleFrame(), True)
        except Exception:
            log.debug("could not fill the screen", exc_info=True)

    AppHelper.callAfter(apply)


def _menu_titles() -> str:
    """The menu bar, for the selftest: 'Brain Fry, File (Open..., Add New Project..., Refresh Explorer), Edit (...), ...'."""
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


def _selftest(window, seconds: float, shots: str | None, js: str | None) -> None:
    """For Claude to check the window without a human: optionally run some JS, wait, print
    the page text and the selected terminal, close."""
    time.sleep(0.5)
    if shots:
        _screenshot(window, f"{shots}/1_splash.png")
    time.sleep(2.5)
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


# ---------------------------------------------------------------- quitting (DESIGN_DETAILS.md, "App")


def quit_question(working: int, unsaved: list[str]) -> str:
    """The text of the one question at quit: the working tasks and the files with unsaved edits
    (`files.dirty` from the front end). "" when quitting loses nothing: no question."""
    parts = []
    if working:
        parts.append(f"{working} task{' is' if working == 1 else 's are'} working. Quitting ends "
                     f"{'its' if working == 1 else 'their'} turn; --resume brings the conversation back.")  # fmt: skip
    if unsaved:
        names = ", ".join(unsaved[:QUIT_NAMES]) + (f" and {len(unsaved) - QUIT_NAMES} more" if len(unsaved) > QUIT_NAMES else "")
        parts.append(f"{len(unsaved)} file{' has' if len(unsaved) == 1 else 's have'} unsaved edits, which quitting loses: {names}.")
    return "\n\n".join(parts)


def _confirm_quit(text: str) -> bool:
    """The question at quit, a native alert run here, on the main thread, where `closing` fires.
    pywebview's create_confirmation_dialog queues the alert to the main thread and waits for it:
    called from the main thread, it waited forever and the app hung (seen 2026-11-14)."""
    from AppKit import NSAlert, NSAlertFirstButtonReturn, NSApp  # noqa: PLC0415

    NSApp().activateIgnoringOtherApps_(True)
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Quit Brain Fry?")
    alert.setInformativeText_(text)
    alert.addButtonWithTitle_("Quit")
    alert.addButtonWithTitle_("Cancel")  # also Escape
    return alert.runModal() == NSAlertFirstButtonReturn


class _Quit:
    """The shutdown, exactly once, whatever ends the app: saves the browser's session cookies,
    ends every session and shell (core.shutdown), ends the `claude --version` probe. Each caller
    runs on the main thread: the `finally` of run(), Cocoa's will-terminate notification, the
    handler of SIGTERM and SIGHUP."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self._once = threading.Lock()

    @property
    def started(self) -> bool:
        return self._once.locked()

    def __call__(self, why: str) -> None:
        if not self._once.acquire(blocking=False):
            return
        log.info("quitting: %s", why)
        try:
            if self.core.browser is not None:
                self.core.browser.shutdown()  # main thread: saves the session cookies
        except Exception:
            log.exception("browser shutdown failed")
        try:
            self.core.shutdown()
        except Exception:
            log.exception("shutdown failed")
        for p in _probes:
            if p.poll() is None:
                p.kill()
        log.info("quit")


def _on_terminate(handler):
    """Cmd+Q, Quit in the app menu or the Dock, a logout and Ctrl-C (pywebview's handler) call
    NSApp.terminate_. Once pywebview's delegate has said yes (`closing`), Cocoa posts this
    notification, then calls exit() inside webview.start(), which never returns: the `finally`
    of run() does not run. The observer token must stay alive."""
    from AppKit import NSApplicationWillTerminateNotification  # noqa: PLC0415
    from Foundation import NSNotificationCenter  # noqa: PLC0415

    return NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
        NSApplicationWillTerminateNotification, None, None, lambda _note: handler())


def _on_signals(handler) -> None:
    """SIGTERM (`kill`) and SIGHUP (the terminal of `bf` closed) killed the app at once. Python
    runs its signal handlers only between bytecodes of the main thread, which sits in Cocoa's run
    loop: PyObjC's MachSignals delivers them through that run loop, as pywebview does for Ctrl-C.
    So they arrive only while the run loop runs (not during a modal alert, not after the window)."""
    from PyObjCTools import MachSignals  # noqa: PLC0415

    for sig in (signal.SIGTERM, signal.SIGHUP):
        MachSignals.signal(sig, handler)


def run(debug: bool = False, browser: bool = False, selftest: float | None = None,
        selftest_shots: str | None = None, selftest_js: str | None = None) -> int:  # fmt: skip
    logfile = logs.setup("app", debug)
    lock = _single_instance()  # noqa: F841
    _log_start(logfile)
    core = Core()
    quit_app = _Quit(core)
    url = f"http://127.0.0.1:{start_backend(core)}/"
    log.info("backend on %s", url)
    try:
        if browser:
            print(f"open {url}   (Ctrl-C to quit)")
            threading.Event().wait()
        import webview  # noqa: PLC0415 (Cocoa only after the backend thread started)
        from AppKit import NSApp  # noqa: PLC0415

        _mac_identity()
        _file_menu_first()
        window = webview.create_window(
            "Brain Fry", url, width=1600, height=1000, min_size=(900, 500),
            background_color="#040510",  # the splash color: no white flash before the page paints
            menu=_file_menu(lambda: window),
        )  # fmt: skip

        def on_closing():
            """The window's close button and every terminate_ (main thread). False: stay."""
            if quit_app.started:  # SIGTERM, SIGHUP: nobody to ask
                return True
            text = quit_question(core.working_count(), core.dirty_files)
            if not text:
                return True
            log.info("quit: asking: %s", text.replace("\n\n", " "))
            ok = True if selftest is not None else _confirm_quit(text)  # a selftest has nobody to answer
            log.info("quit: %s", "confirmed" if ok else "cancelled")
            return ok

        def on_signal(signum: int) -> None:
            name = signal.Signals(signum).name
            log.info("%s received", name)
            quit_app(name)
            NSApp().terminate_(None)  # on_closing says yes: the quit has started

        window.events.closing += on_closing
        window.events.before_show += lambda: _dark_titlebar(window)
        if selftest is None:  # a selftest stays 1600x1000: it would cover Renaud's screen
            window.events.before_show += lambda: _fill_screen(window)
        from . import drops  # noqa: PLC0415
        from .browser import BrowserHost  # noqa: PLC0415 (WebKit: only in the window)

        drops.enable()  # the paths of what is dropped from Finder (web/drop.js asks for them)

        core.browser = BrowserHost(core, window)  # the browser of 3a; sets itself up at its first command
        core.window = window  # the save panel of an untitled tab
        terminating = _on_terminate(lambda: quit_app("Cocoa quits the app (Cmd+Q, a Quit menu, Ctrl-C)"))  # noqa: F841
        _on_signals(on_signal)
        if selftest is not None:
            webview.start(_selftest, (window, selftest, selftest_shots, selftest_js), debug=debug)
        else:
            webview.start(debug=debug)
    except KeyboardInterrupt:
        log.info("interrupted (Ctrl-C)")
    except Exception:
        log.exception("the window failed")
        raise
    finally:
        quit_app("window closed")
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
            p = subprocess.Popen([claude_bin(), "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=child_env())  # fmt: skip
            _probes.append(p)  # a quit before its end kills it (_Quit)
            try:
                out, err = p.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                raise
            if p.returncode < 0:  # killed by the quit
                return
            log.info("claude: %s (%s)", (out or err).strip()[:100], claude_bin())
        except Exception as e:
            log.warning("claude --version failed: %s", e)

    threading.Thread(target=claude_version, name="claude-version", daemon=True).start()
