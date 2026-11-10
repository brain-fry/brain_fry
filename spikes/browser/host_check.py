"""Checks app/browser.py's BrowserHost in a real window, without the rest of Brain Fry:
keys (the monitor), popups and window.close(), a download, uBlock's popup, session cookies,
the sound of a closed tab, and the copy of my Firefox cookies (from a fake profile in WORKDIR).

    uv run python spikes/browser/host_check.py PORT WORKDIR

PORT: spikes/browser/site.py runs there. WORKDIR: a scratch folder; it becomes $HOME (for
~/Downloads) and the Brain Fry home (BRAIN_FRY_HOME: its own browser profile), so nothing
of mine is touched. uBlock is loaded if WORKDIR/.brain_fry/extensions/ublock-lite exists.
The window is ordered in off screen: key events need a real window.
"""

import json
import os
import sqlite3
import sys
import time

PORT, WORK = int(sys.argv[1]), os.path.abspath(sys.argv[2])
os.makedirs(f"{WORK}/Downloads", exist_ok=True)
os.environ["HOME"], os.environ["BRAIN_FRY_HOME"] = WORK, f"{WORK}/.brain_fry"

import AppKit  # noqa: E402
import WebKit  # noqa: E402
from Foundation import NSDate, NSMakeRect  # noqa: E402

from brain_fry.app import browser  # noqa: E402

SITE = f"http://127.0.0.1:{PORT}"

# a Firefox profile with one cookie for the site, and one expired
FF = f"{WORK}/Library/Application Support/Firefox/Profiles/t.default-release"
os.makedirs(FF, exist_ok=True)
with open(f"{WORK}/Library/Application Support/Firefox/profiles.ini", "w") as f:
    f.write("[Install1]\nDefault=Profiles/t.default-release\n")
db = sqlite3.connect(f"{FF}/cookies.sqlite")
db.execute("CREATE TABLE moz_cookies (originAttributes TEXT, name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, "
           "isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)")  # fmt: skip
db.executemany("INSERT INTO moz_cookies VALUES ('', ?, '1', '127.0.0.1', '/', ?, 0, 0, 256)",
               [("ff", int((time.time() + 86400) * 1000)), ("ff_old", int((time.time() - 60) * 1000))])  # fmt: skip
db.commit()
db.close()
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


class Core:
    selected = "task-1"

    def __init__(self):
        self.msgs = []

    def notify(self, msg):
        self.msgs.append(msg)


class Window:  # pywebview's window: .native is the NSWindow
    def __init__(self, native):
        self.native = native


app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(-6000, -6000, 1000, 700), 15, 2, False)
ui = WebKit.WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 1000, 700), WebKit.WKWebViewConfiguration.alloc().init())
ui.loadHTMLString_baseURL_("<title>ui</title><input id=i>", None)
win.setContentView_(ui)
win.orderFrontRegardless()


def pump(seconds, until=None):
    """Runs the event loop: NSEvents (the key monitor sees them) and WebKit's callbacks."""
    end = time.time() + seconds
    while time.time() < end and not (until and until()):
        ev = app.nextEventMatchingMask_untilDate_inMode_dequeue_(AppKit.NSEventMaskAny, NSDate.dateWithTimeIntervalSinceNow_(0.02),
                                                                AppKit.NSDefaultRunLoopMode, True)  # fmt: skip
        if ev is not None:
            app.sendEvent_(ev)


def key(chars, flags=browser.CMD, code=0):
    ev = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        AppKit.NSEventTypeKeyDown, (10, 10), flags, 0, win.windowNumber(), None, chars, chars, False, code)
    app.postEvent_atStart_(ev, False)
    pump(0.3)


def msgs(ev):
    return [m for m in core.msgs if m.get("ev") == ev]


core = Core()
host = browser.BrowserHost(core, Window(win))
orig_config = host._config


def config():  # popups and sound without a click: a test can't click in the page
    cfg = orig_config()
    cfg.preferences().setJavaScriptCanOpenWindowsAutomatically_(True)
    cfg.setMediaTypesRequiringUserActionForPlayback_(0)
    return cfg


def cookies():
    out = {}
    host.store.httpCookieStore().getAllCookies_(lambda cs: out.update(c=[(str(c.domain()), str(c.name())) for c in cs]))
    pump(3, lambda: "c" in out)
    return sorted(out.get("c", []))


def playback(wv):
    out = {}
    wv.requestMediaPlaybackStateWithCompletionHandler_(lambda st: out.update(st=st))
    pump(1, lambda: out)
    return out.get("st")  # None: no answer (the page is gone)


host._config = config
host._ensure()
check("host ready", host.ready, f"flipped={bool(host.ui.isFlipped())}")
pump(4, lambda: host.context is not None)
check("uBlock loaded", host.context is not None, "(skip if not installed)")
pump(5, lambda: host.importing is not None and host.importing.done())
check("Firefox cookies copied at the first start", host.importing is not None and host.importing.done() and not host.importing.exception()
      and host.importing.result()["cookies"] == 1, str(host.importing.result() if host.importing and host.importing.done() else None))  # fmt: skip
check("...and the copy recorded", json.load(open(f"{WORK}/.brain_fry/browser/firefox_import.json"))["cookies"] == 1)
check("...the cookie in the store, not the expired one", ("127.0.0.1", "ff") in cookies() and ("127.0.0.1", "ff_old") not in cookies())

page = host.open("t1", "task-1", f"{SITE}/page")
host._place(page, {"visible": True, "x": 100, "y": 50, "w": 600, "h": 500, "focus": True})
pump(8, lambda: page.wv.title() == "BF browser test" and not page.wv.isLoading())
check("page loads", page.wv.title() == "BF browser test", str(page.wv.URL().absoluteString() if page.wv.URL() else None))
f = page.wv.frame()
check("frame in the parent's coordinates", (f.origin.x, f.size.width, f.size.height) == (100, 600, 500), str(f))
check("update messages", any(m.get("title") == "BF browser test" for m in msgs("update")))
check("page has the focus", host._focused_page() is page)
seen = {}
page.wv.evaluateJavaScript_completionHandler_("document.cookie", lambda v, e: seen.update(v=v))
pump(2, lambda: seen)
check("the page sees the Firefox cookie", "ff=1" in (seen.get("v") or ""), repr(seen.get("v")))

# ---- keys
core.msgs.clear()
key("", code=125)  # Cmd+Down: Brain Fry's (next inbox item)
check("Cmd+Down goes to Brain Fry", msgs("key") == [{"type": "browser", "ev": "key", "key": "ArrowDown", "shift": False, "alt": False, "ctrl": False}],
      str(msgs("key")))  # fmt: skip
check("...and the focus goes back to Brain Fry's page", host._focused_page() is None)
win.makeFirstResponder_(page.wv)
core.msgs.clear()
key("c", code=8)  # Cmd+C: the page's
check("Cmd+C stays with the page", not msgs("key"))
key("w", code=13)  # Cmd+W: closes the browser tab (Brain Fry's page does it), not the window
check("Cmd+W goes to Brain Fry", [m["key"] for m in msgs("key")] == ["w"])
win.makeFirstResponder_(ui)
core.msgs.clear()
key("w", code=13)
check("Cmd+W from Brain Fry's page, a tab on screen: Brain Fry", [m["key"] for m in msgs("key")] == ["w"])
key("d", code=2)
check("Cmd+D from Brain Fry's page: not touched", [m["key"] for m in msgs("key")] == ["w"])
win.makeFirstResponder_(page.wv)
before = page.wv.estimatedProgress()
key("r", code=15)  # Cmd+R: reloads, handled here
pump(3, lambda: not page.wv.isLoading())
check("Cmd+R reloads, nothing forwarded", [m["key"] for m in msgs("key")] == ["w"])

# ---- popups and window.close()
core.msgs.clear()
page.wv.evaluateJavaScript_completionHandler_("window.open('/page2'); 1", None)
pump(5, lambda: msgs("opened") and len(host.pages) == 2 and host.pages[msgs("opened")[0]["tab"]].wv.title() == "page 2")
opened = msgs("opened")
check("window.open --> an 'opened' tab of the same task", opened and opened[0]["task"] == "task-1" and opened[0]["url"].endswith("/page2"), str(opened))
if opened:
    pop = host.pages[opened[0]["tab"]]
    check("the popup loads in its new view", pop.wv.title() == "page 2", str(pop.wv.title()))
    pop.wv.evaluateJavaScript_completionHandler_("window.close()", None)
    pump(3, lambda: msgs("closed"))
    check("window.close() --> 'closed'", [m["tab"] for m in msgs("closed")] == [pop.key])
    host._close(pop)
check("closed tabs are gone", list(host.pages) == ["t1"])

# ---- download
core.msgs.clear()
host._load(page, f"{SITE}/download")
pump(6, lambda: msgs("download"))
d = msgs("download")
check("download to ~/Downloads", d and d[0]["ok"] and os.path.exists(f"{WORK}/Downloads/bf-test.txt"), str(d))

# ---- uBlock's popup
if host.context is not None:
    host._ublock(page, {"x": 600, "y": 20, "w": 60, "h": 20})
    pump(4, lambda: host.popover is not None and host.popup_action.popupWebView().URL() is not None)
    pv = host.popup_action.popupWebView() if host.popup_action else None
    check("uBlock popup shows", host.popover is not None and host.popover.isShown(), str(pv.URL().absoluteString() if pv and pv.URL() else None))
    pump(2)
    if pv is not None:  # what the popup shows: its text, from its own page
        out = {}
        pv.evaluateJavaScript_completionHandler_("document.body.innerText.slice(0, 300)", lambda v, e: out.update(v=v))
        pump(2, lambda: out)
        check("uBlock popup has its content", out.get("v") and "uBlock" in out.get("v", "") or len(out.get("v") or "") > 20, repr((out.get("v") or "")[:120]))
    if host.popover is not None:
        host.popover.close()
        pump(1)
    check("popup closes", host.popover is None)

# ---- session cookies (site.py sets session=1 on /page)
saved = []
host.save_session_cookies(lambda: saved.append(1))
pump(3, lambda: saved)
rows = __import__("json").loads(browser.session_file().read_text())
check("session cookies saved (and only them)", [r["name"] for r in rows] == ["session"], str([r["name"] for r in rows]))

# ---- a closed tab stops its sound, even while something still holds its view
snd = host.open("t2", "task-1", f"{SITE}/audio")
host._place(snd, {"visible": True, "x": 100, "y": 50, "w": 300, "h": 200})
pump(6, lambda: playback(snd.wv) == WebKit.WKMediaPlaybackStatePlaying)
held = snd.wv
check("the audio page plays", playback(held) == WebKit.WKMediaPlaybackStatePlaying, str(playback(held)))
host._close(snd)
pump(1)
st = playback(held)
check("closed: no sound any more", st != WebKit.WKMediaPlaybackStatePlaying, f"state {st}")
check("closed: the page --> ext --> page cycle is cut", snd.ext.page is None)
del held

# ---- "Overwrite my cookies": per site, Firefox's replace the store's; other sites keep theirs
from Foundation import NSHTTPCookie  # noqa: E402

for dom, name in (("127.0.0.1", "bf_only"), (".example.org", "other_site")):
    host.store.httpCookieStore().setCookie_completionHandler_(
        NSHTTPCookie.cookieWithProperties_({"Name": name, "Value": "1", "Domain": dom, "Path": "/", "Expires": NSDate.dateWithTimeIntervalSinceNow_(3600)}), None)
pump(1)
before = cookies()
fut = host._command({"cmd": "browser.cookies.firefox"})
pump(5, lambda: fut.done())
after = cookies()
check("overwrite: done", fut.done() and not fut.exception(), str(fut.result() if fut.done() else None))
check("overwrite: the site's own cookies replaced by Firefox's", ("127.0.0.1", "bf_only") in before
      and [n for d, n in after if d == "127.0.0.1"] == ["ff"], f"before {before} after {after}")  # fmt: skip
check("overwrite: a site that Firefox lacks keeps its cookie", (".example.org", "other_site") in after)

host._close(page)
fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed" + (f"; FAILED: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
