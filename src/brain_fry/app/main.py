"""`bf` without arguments: the window."""

from __future__ import annotations

import fcntl
import json
import logging
import sys
import threading
import time
from importlib import resources

from .. import paths
from .core import Core
from .server import start_backend

log = logging.getLogger("brain_fry")


def _mac_identity() -> None:
    """Dock icon and app menu name ("Python" otherwise). pywebview's `icon` option is
    GTK/Qt only; on macOS it comes from AppKit. Main thread, before webview.start()."""
    try:
        from AppKit import NSApplication, NSImage  # noqa: PLC0415 (pyobjc, a pywebview dependency)
        from Foundation import NSBundle  # noqa: PLC0415

        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Brain Fry"
        icon = resources.files("brain_fry") / "web" / "icon.png"
        image = NSImage.alloc().initWithContentsOfFile_(str(icon))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        log.debug("could not set the Dock icon", exc_info=True)


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
            splash: !!document.getElementById('splash'), size: [innerWidth, innerHeight]}});
})()
"""


def _screenshot(window, path: str) -> None:
    """The page as WebKit renders it (WKWebView.takeSnapshot). Unlike `screencapture`, it
    needs no Screen Recording permission."""
    from AppKit import NSBitmapImageRep  # noqa: PLC0415
    from PyObjCTools import AppHelper  # noqa: PLC0415
    from WebKit import WKWebView  # noqa: PLC0415

    done = threading.Event()

    def handler(image, error):
        if image is not None:
            rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            rep.representationUsingType_properties_(4, None).writeToFile_atomically_(path, True)  # 4: PNG
            print(f"=== screenshot: {path}")
        else:
            print(f"=== screenshot failed: {error}")
        done.set()

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
    print("=== JS errors:", info["errors"])
    print("=== debug:", info.get("debug"))
    print("=== page text:\n" + info["ui"])
    print("=== terminal:\n" + info["terminal"])
    window.destroy()


def run(debug: bool = False, browser: bool = False, selftest: float | None = None,
        selftest_new: str | None = None, selftest_shots: str | None = None,
        selftest_js: str | None = None) -> int:  # fmt: skip
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lock = _single_instance()  # noqa: F841
    core = Core()
    url = f"http://127.0.0.1:{start_backend(core)}/"
    try:
        if browser:
            print(f"open {url}   (Ctrl-C to quit)")
            threading.Event().wait()
        import webview  # noqa: PLC0415 (Cocoa only after the backend thread started)

        _mac_identity()
        window = webview.create_window(
            "Brain Fry", url, width=1600, height=1000, min_size=(900, 500),
            background_color="#040510",  # the splash color: no white flash before the page paints
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
        if selftest is not None:
            webview.start(_selftest, (window, selftest, selftest_new, selftest_shots, selftest_js), debug=debug)
        else:
            webview.start(debug=debug)
    except KeyboardInterrupt:
        pass
    finally:
        core.shutdown()
    return 0
