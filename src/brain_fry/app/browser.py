"""The browser of 3a: one native WKWebView per tab, laid over the tab's placeholder in Brain
Fry's page (web/browser.js sends the placeholder's rectangle). See DESIGN_DETAILS.md, "Browser (3a)".

Everything here runs on the main (Cocoa) thread: `call()` takes a command from the backend's
thread and runs it there. Pages report to the front end through `core.notify`.

- One persistent website data store (cookies, storage) for every tab of every task, keyed by
  the Brain Fry home, so that tests never touch the real profile. Cookies without an expiry
  date (session cookies) are saved every 30 s and at quit, and restored at start, as a browser
  that restores its last session does.
- uBlock Origin Lite (`ublock.py`) through WebKit's extension API, with a fixed identity, so
  that its settings survive a restart.
- My Firefox cookies (`firefox.py`) are copied into the store once, at the first start, and
  again by "Overwrite my cookies" in the burger menu: per site, Firefox's replace the store's.
- A key monitor sees each key before WebKit: while a page has the focus, Brain Fry's own Cmd
  keys go to Brain Fry's page, and Cmd+W closes the browser tab on screen, not the window.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import platform
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path

import AppKit
import objc
import WebKit
from Foundation import NSURL, NSDate, NSHTTPCookie, NSMakeRect, NSObject, NSRunLoop, NSTimer, NSURLRequest, NSUUID
from PyObjCTools import AppHelper

from .. import firefox, paths, ublock
from ..logs import short

log = logging.getLogger(__name__)

WEB_SCHEMES = {"http", "https", "about", "data", "blob", "file", "webkit-extension", "javascript"}
KVO_KEYS = ("URL", "title", "loading", "canGoBack", "canGoForward")
# Cmd keys that go to Brain Fry's page while a browser page has the focus: Brain Fry's own
# (app.js: N and J start the chords of grid.js, T a new task) and the browser's that live in the page
# (browser.js: address, find, close)
FORWARD = {"arrowup", "arrowdown", "n", "j", "p", "d", "e", "b", "1", "2", "3", "4", "5", "6", "7", "8", "9", "l", "t", "f", "w", "g"}
ARROWS = {0xF700: "arrowup", 0xF701: "arrowdown"}
KEY_NAMES = {"arrowup": "ArrowUp", "arrowdown": "ArrowDown"}
CMD, SHIFT = AppKit.NSEventModifierFlagCommand, AppKit.NSEventModifierFlagShift
ALT, CTRL = AppKit.NSEventModifierFlagOption, AppKit.NSEventModifierFlagControl
SESSION_SAVE_EVERY = 30.0  # s
# After a start, uBlock's rules apply about 2 s after its context loads (measured on
# 2026-11-10: a load at +1 s showed the ads, one at +2 s did not). Pages wait until then.
# The first start after an update compiles the rules again (about 5 s): those seconds leak.
UBLOCK_SETTLE, UBLOCK_LOAD_MAX = 2.5, 10.0  # s
EXT_ID, EXT_BASE = "org.brainfry.ublock-lite", "webkit-extension://ublock-lite/"
POPUP_SIZE = (360, 560)
IMPORT_MAX = 20.0  # s: page loads wait while my Firefox cookies are copied, at most this long
STORE_NS = uuid.UUID("2f0b6a3e-8c1d-4b7a-9e55-0c3d1f6a7b21")
IGNORED_ERRORS = {("NSURLErrorDomain", -999), ("WebKitErrorDomain", 102)}  # cancelled; became a download


def store_uuid() -> str:
    """One browser profile per Brain Fry home (BRAIN_FRY_HOME for tests)."""
    return str(uuid.uuid5(STORE_NS, str(paths.bf_home().resolve()))).upper()


def session_file() -> Path:
    return paths.bf_home() / "browser" / "session_cookies.json"


def user_agent_suffix() -> str:
    """Appended to WebKit's user agent, as Safari does: sites treat the pages as Safari."""
    return f"Version/{platform.mac_ver()[0] or '26.0'} Safari/605.1.15"


def cookie_row(c) -> dict:
    return {"name": str(c.name()), "value": str(c.value()), "domain": str(c.domain()), "path": str(c.path() or "/"),
            "secure": bool(c.isSecure()), "httpOnly": bool(c.isHTTPOnly()), "sameSite": str(c.sameSitePolicy() or "")}  # fmt: skip


def cookie_props(row: dict) -> dict:
    """NSHTTPCookie properties of a row. Without `expires` (epoch s), a session cookie."""
    p = {"Name": row["name"], "Value": row["value"], "Domain": row["domain"], "Path": row.get("path") or "/"}
    if row.get("expires"):
        p["Expires"] = NSDate.dateWithTimeIntervalSince1970_(float(row["expires"]))
    if row.get("secure"):
        p["Secure"] = "TRUE"
    if row.get("httpOnly"):
        p["HttpOnly"] = "TRUE"
    if row.get("sameSite"):
        p["SameSite"] = row["sameSite"]
    return p


def write_private(path: Path, text: str) -> None:
    """Atomic, readable by me only: the file holds login cookies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(1, 10_000):
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
    raise FileExistsError(p)


def _chain(src: Future, dst: Future) -> None:
    if src.exception() is not None:
        dst.set_exception(src.exception())
    else:
        dst.set_result(src.result())


# ---------------------------------------------------------------- Objective-C side
# The classes below implement WebKit's delegate protocols without `protocols=[...]`: with it,
# PyObjC takes the method signatures from the runtime protocol, which does not describe the
# blocks (decisionHandler, completionHandler), and calling one fails ("cannot call block
# without a signature"). Without it, PyObjC uses its framework metadata, which does. The
# protocols are added afterwards through the runtime (_conform), in case WebKit checks them.
# PyObjC 13's metadata lacks one selector (uBlock's popup): registered here.

objc.registerMetaDataForSelector(
    b"NSObject", b"webExtensionController:presentActionPopup:forExtensionContext:completionHandler:",
    {"arguments": {5: {"callable": {"retval": {"type": b"v"}, "arguments": {0: {"type": b"^v"}, 1: {"type": b"@"}}}}}},
)  # fmt: skip

_libobjc = ctypes.CDLL("/usr/lib/libobjc.dylib")
_libobjc.objc_getClass.argtypes = _libobjc.objc_getProtocol.argtypes = [ctypes.c_char_p]
_libobjc.objc_getClass.restype = _libobjc.objc_getProtocol.restype = ctypes.c_void_p
_libobjc.class_addProtocol.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_libobjc.class_addProtocol.restype = ctypes.c_bool


def _conform(cls, protocol: str) -> None:
    c, p = _libobjc.objc_getClass(cls.__name__.encode()), _libobjc.objc_getProtocol(protocol.encode())
    if not (c and p and _libobjc.class_addProtocol(c, p)):
        log.debug("could not declare %s on %s", protocol, cls.__name__)


class _Tab(NSObject):  # WKWebExtensionTab
    """A browser tab as WebKit's extension API sees it. `page` is set after init, and is None
    once the tab is closed."""

    def webViewForWebExtensionContext_(self, ctx):
        return self.page.wv if self.page else None

    def windowForWebExtensionContext_(self, ctx):
        return self.page.host.ext_window if self.page else None

    def isSelectedForWebExtensionContext_(self, ctx):
        return bool(self.page) and self.page.host.active == self.page.key

    def reloadFromOrigin_forWebExtensionContext_completionHandler_(self, from_origin, ctx, done):
        if self.page:
            (self.page.wv.reloadFromOrigin if from_origin else self.page.wv.reload)()
        done(None)

    def closeForWebExtensionContext_completionHandler_(self, ctx, done):
        if self.page:
            self.page.host.notify("closed", self.page)
        done(None)

    def activateForWebExtensionContext_completionHandler_(self, ctx, done):
        done(None)


class _Window(NSObject):  # WKWebExtensionWindow
    """All the tabs, as one window: Brain Fry has one."""

    def tabsForWebExtensionContext_(self, ctx):
        return [p.ext for p in self.host.pages.values()]

    def activeTabForWebExtensionContext_(self, ctx):
        p = self.host.pages.get(self.host.active)
        return p.ext if p else None


class _ExtDelegate(NSObject):  # WKWebExtensionControllerDelegate
    def webExtensionController_openWindowsForExtensionContext_(self, c, ctx):
        return [self.host.ext_window]

    def webExtensionController_focusedWindowForExtensionContext_(self, c, ctx):
        return self.host.ext_window

    def webExtensionController_openNewTabUsingConfiguration_forExtensionContext_completionHandler_(self, c, conf, ctx, done):
        url = conf.url()
        page = self.host.open_native(str(url.absoluteString()) if url else "about:blank", bool(conf.shouldBeActive()))
        done(page.ext, None)

    def webExtensionController_openOptionsPageForExtensionContext_completionHandler_(self, c, ctx, done):
        url = ctx.optionsPageURL()
        if url is not None:
            self.host.open_native(str(url.absoluteString()), True)
        done(None)

    def webExtensionController_presentActionPopup_forExtensionContext_completionHandler_(self, c, action, ctx, done):
        self.host.show_popup(action)
        done(None)

    # uBlock is installed by me: what it asks for is granted
    def webExtensionController_promptForPermissions_inTab_forExtensionContext_completionHandler_(self, c, perms, tab, ctx, done):
        done(perms, None)

    def webExtensionController_promptForPermissionToAccessURLs_inTab_forExtensionContext_completionHandler_(self, c, urls, tab, ctx, done):
        done(urls, None)

    def webExtensionController_promptForPermissionMatchPatterns_inTab_forExtensionContext_completionHandler_(self, c, pats, tab, ctx, done):
        done(pats, None)


class _PopoverDelegate(NSObject):  # NSPopoverDelegate
    def popoverDidClose_(self, note):
        action, self.host.popup_action, self.host.popover = self.host.popup_action, None, None
        if action is not None:
            action.closePopup()


class _Observer(NSObject):
    def observeValueForKeyPath_ofObject_change_context_(self, path, obj, change, ctx):
        self.host.changed(obj)


class _Nav(NSObject):  # WKNavigationDelegate
    def webView_decidePolicyForNavigationAction_decisionHandler_(self, wv, action, handler):
        url = action.request().URL()
        scheme = str(url.scheme() or "").lower() if url is not None else ""
        if scheme and scheme not in WEB_SCHEMES:  # mailto:, zoommtg:, vscode:... go to their app
            AppKit.NSWorkspace.sharedWorkspace().openURL_(url)
            handler(WebKit.WKNavigationActionPolicyCancel)
            return
        if action.shouldPerformDownload():
            handler(WebKit.WKNavigationActionPolicyDownload)
            return
        if (action.navigationType() == WebKit.WKNavigationTypeLinkActivated and action.modifierFlags() & CMD
                and action.targetFrame() is not None):  # Cmd+click: a new tab, in the background
            self.host.open_child(wv, url)
            handler(WebKit.WKNavigationActionPolicyCancel)
            return
        handler(WebKit.WKNavigationActionPolicyAllow)

    def webView_decidePolicyForNavigationResponse_decisionHandler_(self, wv, resp, handler):
        r = resp.response()
        disp = r.valueForHTTPHeaderField_("Content-Disposition") if r.respondsToSelector_("valueForHTTPHeaderField:") else None
        attachment = bool(disp) and str(disp).lower().startswith("attachment")
        if resp.isForMainFrame() and (attachment or not resp.canShowMIMEType()):
            handler(WebKit.WKNavigationResponsePolicyDownload)
            return
        handler(WebKit.WKNavigationResponsePolicyAllow)

    def webView_navigationAction_didBecomeDownload_(self, wv, action, dl):
        dl.setDelegate_(self.host.downloads)

    def webView_navigationResponse_didBecomeDownload_(self, wv, resp, dl):
        dl.setDelegate_(self.host.downloads)

    def webView_didStartProvisionalNavigation_(self, wv, nav):
        self.host.failed(wv, None)

    def webView_didFailProvisionalNavigation_withError_(self, wv, nav, err):
        self.host.failed(wv, err)

    def webView_didFailNavigation_withError_(self, wv, nav, err):
        self.host.failed(wv, err)

    def webViewWebContentProcessDidTerminate_(self, wv):
        log.warning("browser: a page's web process ended: reloading it")
        wv.reload()


class _UI(NSObject):  # WKUIDelegate
    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, wv, cfg, action, features):
        return self.host.open_popup(wv, cfg, action)  # window.open, target=_blank: a new tab

    def webViewDidClose_(self, wv):
        page = self.host.page_of(wv)
        if page is not None:
            self.host.notify("closed", page)

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(self, wv, msg, frame, done):
        _alert(msg, frame, ["OK"])
        done()

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(self, wv, msg, frame, done):
        done(_alert(msg, frame, ["OK", "Cancel"]) == AppKit.NSAlertFirstButtonReturn)

    def webView_runJavaScriptTextInputPanelWithPrompt_defaultText_initiatedByFrame_completionHandler_(self, wv, prompt, default, frame, done):
        field = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        field.setStringValue_(default or "")
        ok = _alert(prompt, frame, ["OK", "Cancel"], field) == AppKit.NSAlertFirstButtonReturn
        done(str(field.stringValue()) if ok else None)

    def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(self, wv, params, frame, done):
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(bool(params.allowsDirectories()))
        panel.setAllowsMultipleSelection_(bool(params.allowsMultipleSelection()))
        done(panel.URLs() if panel.runModal() == AppKit.NSModalResponseOK else None)


def _alert(text, frame, buttons, accessory=None):
    a = AppKit.NSAlert.alloc().init()
    origin = frame.securityOrigin() if frame is not None else None
    a.setMessageText_(f"{origin.host()} says" if origin is not None and origin.host() else "This page says")
    a.setInformativeText_(str(text or ""))
    for b in buttons:
        a.addButtonWithTitle_(b)
    if accessory is not None:
        a.setAccessoryView_(accessory)
    return a.runModal()


class _Downloads(NSObject):  # WKDownloadDelegate
    def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(self, dl, resp, name, done):
        path = unique_path(Path.home() / "Downloads" / (Path(str(name or "download")).name or "download"))
        self.host.download_paths[objc.pyobjc_id(dl)] = path
        log.info("browser: download to %s", path)
        done(NSURL.fileURLWithPath_(str(path)))

    def downloadDidFinish_(self, dl):
        path = self.host.download_paths.pop(objc.pyobjc_id(dl), None)
        if path is not None:
            self.host.core.notify({"type": "browser", "ev": "download", "ok": True, "name": path.name, "path": str(path)})

    def download_didFailWithError_resumeData_(self, dl, err, data):
        path = self.host.download_paths.pop(objc.pyobjc_id(dl), None)
        log.warning("browser: download failed: %s", err.localizedDescription())
        self.host.core.notify({"type": "browser", "ev": "download", "ok": False, "name": path.name if path else "",
                               "error": str(err.localizedDescription())})  # fmt: skip


for _cls, _proto in ((_Tab, "WKWebExtensionTab"), (_Window, "WKWebExtensionWindow"), (_Nav, "WKNavigationDelegate"),
                     (_ExtDelegate, "WKWebExtensionControllerDelegate"), (_UI, "WKUIDelegate"),
                     (_Downloads, "WKDownloadDelegate"), (_PopoverDelegate, "NSPopoverDelegate")):  # fmt: skip
    _conform(_cls, _proto)


# ---------------------------------------------------------------- the host


class Page:
    """One tab: its view, its task, and its WebKit extension tab."""

    def __init__(self, host: BrowserHost, key: str, task: str | None, wv) -> None:
        self.host, self.key, self.task, self.wv = host, key, task, wv
        self.pending: str | None = None  # a restored tab loads its URL when it first shows
        self.error: str | None = None
        self.ext = _Tab.alloc().init()
        self.ext.page = self


class BrowserHost:
    def __init__(self, core, window) -> None:
        self.core, self.window = core, window  # window: pywebview's; its .native is the NSWindow
        self.pages: dict[str, Page] = {}
        self.views: dict[int, Page] = {}  # objc.pyobjc_id(view) -> page
        self.active: str | None = None  # the page on screen, or the last one that was
        self.visible: str | None = None
        self.ready = False
        self.seq = 0
        self.context = None  # uBlock's extension context, once loaded
        self.popover = self.popup_action = self.popup_anchor = None
        self.download_paths: dict[int, Path] = {}
        self.monitor = None
        self._saved_cookies: str | None = None
        self.hold_until = 0.0  # epoch s: page loads wait until uBlock's rules apply
        self.import_hold = 0.0  # epoch s: ...and while my Firefox cookies are copied
        self.importing: Future | None = None

    # ---- from the backend's thread

    def call(self, msg: dict) -> Future:
        """Runs a command on the main thread. The future holds its reply."""
        fut: Future = Future()

        def run() -> None:
            try:
                self._ensure()
                r = self._command(msg)
                if isinstance(r, Future):
                    r.add_done_callback(lambda f: _chain(f, fut))
                else:
                    fut.set_result(r)
            except Exception as e:
                if msg.get("req") is None:
                    log.warning("browser command %s failed: %s", msg.get("cmd"), e)
                fut.set_exception(e)

        AppHelper.callAfter(run)
        return fut

    # ---- main thread from here on

    def _ensure(self) -> None:
        if self.ready:
            return
        self.ui = self._find_ui()
        if self.ui is None:
            raise RuntimeError("the window has no page yet")
        self.nswindow = self.ui.window()
        uid = NSUUID.alloc().initWithUUIDString_(store_uuid())
        self.store = WebKit.WKWebsiteDataStore.dataStoreForIdentifier_(uid)
        ccfg = WebKit.WKWebExtensionControllerConfiguration.configurationWithIdentifier_(uid)
        ccfg.setDefaultWebsiteDataStore_(self.store)
        self.controller = WebKit.WKWebExtensionController.alloc().initWithConfiguration_(ccfg)
        self.ext_delegate = self._obj(_ExtDelegate)
        self.controller.setDelegate_(self.ext_delegate)
        self.ext_window = self._obj(_Window)
        self.controller.didOpenWindow_(self.ext_window)
        self.nav, self.uidel, self.downloads = self._obj(_Nav), self._obj(_UI), self._obj(_Downloads)
        self.observer, self.pop_delegate = self._obj(_Observer), self._obj(_PopoverDelegate)
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, self._on_key)
        self._restore_session_cookies()
        if not firefox.import_file().exists():  # my Firefox cookies, once (Renaud, 2026-11-10); later by the menu
            self.import_firefox(auto=True)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(SESSION_SAVE_EVERY, True, lambda t: self.save_session_cookies())
        self._load_ublock()
        self.ready = True
        log.info("browser: ready (profile %s, flipped %s)", store_uuid()[:8], bool(self.ui.isFlipped()))

    def _obj(self, cls):
        o = cls.alloc().init()
        o.host = self
        return o

    def _find_ui(self):
        def find(view):
            if view.isKindOfClass_(WebKit.WKWebView):
                return view
            for sub in view.subviews():
                if found := find(sub):
                    return found
            return None

        cv = self.window.native.contentView()
        return find(cv) if cv is not None else None

    def _config(self):
        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        cfg.setWebsiteDataStore_(self.store)
        cfg.setWebExtensionController_(self.controller)
        cfg.setApplicationNameForUserAgent_(user_agent_suffix())
        prefs = cfg.preferences()
        prefs.setElementFullscreenEnabled_(True)
        try:
            prefs.setValue_forKey_(True, "developerExtrasEnabled")  # right click --> Inspect Element
        except Exception:
            pass
        return cfg

    def _command(self, msg: dict):
        cmd, key = msg["cmd"], msg.get("tab")
        if cmd == "browser.open":
            self.open(str(key), msg.get("id"), str(msg.get("url") or "about:blank"), lazy=bool(msg.get("lazy")))
            return {"ok": True}
        if cmd == "browser.reveal":  # a finished download, in the Finder
            p = Path(str(msg.get("path"))).resolve()
            if p.parent != (Path.home() / "Downloads").resolve():
                raise PermissionError("only a download can be revealed")
            AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(str(p))])
            return None
        if cmd == "browser.cookies.firefox":  # the burger menu: "Overwrite my cookies"
            return self.import_firefox()
        page = self.pages.get(str(key))
        if page is None:
            if cmd in ("browser.close", "browser.place"):
                return None
            raise ValueError(f"no browser tab {key}")
        if cmd == "browser.place":
            self._place(page, msg)
        elif cmd == "browser.go":
            page.pending = None
            self._load(page, str(msg["url"]))
        elif cmd == "browser.nav":
            a = msg.get("action")
            {"back": page.wv.goBack, "forward": page.wv.goForward, "reload": page.wv.reload, "stop": page.wv.stopLoading}[a]()
        elif cmd == "browser.close":
            self._close(page)
        elif cmd == "browser.find":
            return self._find(page, str(msg.get("query") or ""), bool(msg.get("backwards")))
        elif cmd == "browser.snapshot":
            return self._snapshot(page)
        elif cmd == "browser.ublock":
            return self._ublock(page, msg)
        else:
            raise ValueError(f"unknown command {cmd!r}")
        return None

    # ---- pages

    def open(self, key: str, task: str | None, url: str, lazy: bool = False, cfg=None) -> Page:
        if key in self.pages:
            return self.pages[key]
        if cfg is None:  # an extension's own page needs the configuration that its context gives
            cfg = self.context.webViewConfiguration() if url.startswith("webkit-extension:") and self.context else self._config()
        wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 400, 300), cfg)
        wv.setNavigationDelegate_(self.nav)
        wv.setUIDelegate_(self.uidel)
        wv.setAllowsBackForwardNavigationGestures_(True)
        wv.setAllowsMagnification_(True)
        if wv.respondsToSelector_("setInspectable:"):
            wv.setInspectable_(True)  # Safari's Web Inspector
        wv.setHidden_(True)
        self.ui.addSubview_positioned_relativeTo_(wv, AppKit.NSWindowAbove, None)
        page = Page(self, key, task, wv)
        self.pages[key] = page
        self.views[objc.pyobjc_id(wv)] = page
        for k in KVO_KEYS:
            wv.addObserver_forKeyPath_options_context_(self.observer, k, 0, None)
        self.controller.didOpenTab_(page.ext)
        if lazy:
            page.pending = url
        elif url and url != "about:blank":  # a popup's view: WebKit loads it
            self._load(page, url)
        log.info("browser: tab %s opened (task %s)%s, %d tab(s)", key, short(task) if task else "-",
                 " lazy" if lazy else "", len(self.pages))  # fmt: skip
        return page

    def page_of(self, wv) -> Page | None:
        return self.views.get(objc.pyobjc_id(wv))

    def _new_key(self) -> str:
        self.seq += 1
        return f"n{self.seq}"

    def open_popup(self, opener_wv, cfg, action):
        """window.open or target=_blank: WebKit loads the page into the view returned here,
        which must use its configuration (that keeps window.opener, for OAuth logins)."""
        opener = self.page_of(opener_wv)
        task = opener.task if opener else self._active_task()
        page = self.open(self._new_key(), task, "about:blank", cfg=cfg)
        url = action.request().URL()
        self._announce(page, str(url.absoluteString()) if url else "", True, opener)
        return page.wv

    def open_child(self, opener_wv, url) -> None:
        opener = self.page_of(opener_wv)
        page = self.open(self._new_key(), opener.task if opener else self._active_task(), str(url.absoluteString()))
        self._announce(page, str(url.absoluteString()), False, opener)

    def open_native(self, url: str, activate: bool) -> Page:
        """A tab that uBlock opens (its dashboard): for the task on screen."""
        page = self.open(self._new_key(), self._active_task(), url)
        self._announce(page, url, activate)
        return page

    def _announce(self, page: Page, url: str, activate: bool, opener: Page | None = None) -> None:
        """opener: the page that opened it; the new tab goes to its pane (browser.js)."""
        self.core.notify({"type": "browser", "ev": "opened", "tab": page.key, "task": page.task, "url": url, "activate": activate,
                          "opener": opener.key if opener else None})  # fmt: skip

    def _active_task(self) -> str | None:
        p = self.pages.get(self.active)
        return p.task if p else self.core.selected

    def _load(self, page: Page, url: str) -> None:
        u = NSURL.URLWithString_(url)
        if u is None:
            raise ValueError(f"not a URL: {url[:80]}")
        wait = max(self.hold_until, self.import_hold) - time.time()
        if wait > 0:  # uBlock's rules are not active yet (a page loaded now would show its ads), or cookies are being copied
            # check again soon: the holds move when uBlock has loaded, or end when the copy does
            AppHelper.callLater(min(wait, 0.25), lambda: self._load(page, url) if self.pages.get(page.key) is page else None)
            return
        if u.isFileURL():
            page.wv.loadFileURL_allowingReadAccessToURL_(u, u.URLByDeletingLastPathComponent())
        else:
            page.wv.loadRequest_(NSURLRequest.requestWithURL_(u))

    def _place(self, page: Page, msg: dict) -> None:
        if msg.get("visible"):
            x, y, w, h = (float(msg[k]) for k in ("x", "y", "w", "h"))
            fy = y if self.ui.isFlipped() else self.ui.bounds().size.height - y - h
            page.wv.setFrame_(NSMakeRect(x, fy, w, h))
            self._fade(page.wv, float(msg.get("alpha", 1.0)), float(msg.get("fade") or 0))
            page.wv.setHidden_(False)
            if page.pending:
                url, page.pending = page.pending, None
                self._load(page, url)
            if self.visible != page.key:
                prev = self.pages.get(self.active)
                self.visible = self.active = page.key
                self.controller.didActivateTab_previousActiveTab_(page.ext, prev.ext if prev is not None and prev is not page else None)
            if msg.get("focus"):
                self.nswindow.makeFirstResponder_(page.wv)
        else:
            if not page.wv.isHidden():
                if self._has_focus(page):
                    self.nswindow.makeFirstResponder_(self.ui)
                page.wv.setHidden_(True)
            if self.visible == page.key:
                self.visible = None

    @staticmethod
    def _fade(view, alpha: float, ms: float) -> None:
        """A native page ignores the CSS opacity of the page under it: while its task's tabs are
        built it has alpha 0, then it fades in with 2a and column 3 (web/veil.js)."""
        if view.alphaValue() == alpha:
            return
        if ms <= 0:
            view.setAlphaValue_(alpha)
            return
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(ms / 1000)
        view.animator().setAlphaValue_(alpha)
        AppKit.NSAnimationContext.endGrouping()

    def _has_focus(self, page: Page) -> bool:
        r = self.nswindow.firstResponder()
        return r is not None and r.isKindOfClass_(AppKit.NSView) and r.isDescendantOf_(page.wv)

    def _focused_page(self) -> Page | None:
        for p in self.pages.values():
            if not p.wv.isHidden() and self._has_focus(p):
                return p
        return None

    def _close(self, page: Page) -> None:
        wv = page.wv
        for k in KVO_KEYS:
            wv.removeObserver_forKeyPath_(self.observer, k)
        if self._has_focus(page):
            self.nswindow.makeFirstResponder_(self.ui)
        # A view that something still holds plays on: a closed YouTube tab kept its sound
        # (Renaud, 2026-11-10). The page --> ext --> page cycle held it: PyObjC cannot collect
        # a cycle through an Objective-C object. So the cycle is cut, and the page is ended.
        wv.pauseAllMediaPlaybackWithCompletionHandler_(None)
        wv.closeAllMediaPresentationsWithCompletionHandler_(None)  # fullscreen video, picture in picture
        wv.stopLoading()
        wv.setNavigationDelegate_(None)
        wv.setUIDelegate_(None)
        wv.removeFromSuperview()
        self.controller.didCloseTab_windowIsClosing_(page.ext, False)
        page.ext.page = None
        if wv.respondsToSelector_("_close"):
            wv._close()  # WebKit SPI: closes the page (and its web process's share of it) now
        else:
            wv.loadHTMLString_baseURL_("", None)
        self.pages.pop(page.key, None)
        self.views.pop(objc.pyobjc_id(page.wv), None)
        if self.visible == page.key:
            self.visible = None
        if self.active == page.key:
            self.active = None
        log.info("browser: tab %s closed, %d tab(s)", page.key, len(self.pages))

    def changed(self, wv) -> None:  # KVO: URL, title, loading, back, forward
        page = self.page_of(wv)
        if page is not None:
            self.notify("update", page)

    def failed(self, wv, err) -> None:
        page = self.page_of(wv)
        if page is None:
            return
        if err is not None and (str(err.domain()), int(err.code())) in IGNORED_ERRORS:
            return
        page.error = str(err.localizedDescription()) if err is not None else None
        if err is not None:
            self.notify("update", page)

    def notify(self, ev: str, page: Page) -> None:
        wv, msg = page.wv, {"type": "browser", "ev": ev, "tab": page.key}
        if ev == "update":
            url = wv.URL()
            msg.update(url=str(url.absoluteString()) if url is not None else (page.pending or ""), title=str(wv.title() or ""),
                       loading=bool(wv.isLoading()), back=bool(wv.canGoBack()), forward=bool(wv.canGoForward()), error=page.error)  # fmt: skip
        self.core.notify(msg)

    def _find(self, page: Page, query: str, backwards: bool):
        if not query:
            return {"found": False}
        fut: Future = Future()
        cfg = WebKit.WKFindConfiguration.alloc().init()
        cfg.setBackwards_(backwards)
        cfg.setWraps_(True)
        cfg.setCaseSensitive_(False)
        page.wv.findString_withConfiguration_completionHandler_(query, cfg, lambda r: fut.set_result({"found": bool(r.matchFound())}))
        return fut

    def _snapshot(self, page: Page):
        """The page as a JPEG data URL: shown in its place while an HTML dialog covers it."""
        fut: Future = Future()

        def done(img, err):
            if img is None:
                fut.set_result(None)
                return
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
            data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypeJPEG, {AppKit.NSImageCompressionFactor: 0.75})
            fut.set_result("data:image/jpeg;base64," + base64.b64encode(bytes(data)).decode())

        page.wv.takeSnapshotWithConfiguration_completionHandler_(None, done)
        return fut

    # ---- keys

    def _on_key(self, event):
        try:
            w = event.window()
            if w is None or w.windowNumber() != self.nswindow.windowNumber():
                return event  # uBlock's popup, a dialog
            flags = event.modifierFlags()
            if not flags & CMD:
                return event
            chars = str(event.charactersIgnoringModifiers() or "")
            k = ARROWS.get(ord(chars[0]), chars.lower()) if chars else ""
            page = self._focused_page()
            if k == "w" and (page is not None or self.visible is not None):
                self._forward(k, flags, page)  # closes the browser tab on screen (browser.js)
                return None
            if page is None:
                return event
            wv = page.wv
            if k == "r":
                wv.reload()
            elif k == "[":
                wv.goBack()
            elif k == "]":
                wv.goForward()
            elif k in ("=", "+"):
                wv.setPageZoom_(min(3.0, wv.pageZoom() + 0.1))
            elif k == "-":
                wv.setPageZoom_(max(0.3, wv.pageZoom() - 0.1))
            elif k == "0":
                wv.setPageZoom_(1.0)
            elif k in FORWARD:
                self.nswindow.makeFirstResponder_(self.ui)  # Cmd+N: the keys after it go to Brain Fry's page
                self._forward(k, flags, page)
            else:
                return event  # copy, paste, select all, undo...: the page's
            return None
        except Exception:
            log.exception("browser: key monitor")
            return event

    def _forward(self, k: str, flags: int, page: Page | None = None) -> None:
        """page: the one that had the focus; its pane becomes the one in use (browser.js)."""
        self.core.notify({"type": "browser", "ev": "key", "key": KEY_NAMES.get(k, k), "tab": page.key if page else None,
                          "shift": bool(flags & SHIFT), "alt": bool(flags & ALT), "ctrl": bool(flags & CTRL)})  # fmt: skip

    # ---- uBlock Origin Lite

    def _load_ublock(self) -> None:
        d = ublock.ext_dir()
        if not (d / "manifest.json").exists():
            log.info("browser: uBlock Origin Lite is not installed (bf ublock)")
            return
        self.hold_until = time.time() + UBLOCK_LOAD_MAX  # until it is loaded, then UBLOCK_SETTLE more
        WebKit.WKWebExtension.extensionWithResourceBaseURL_completionHandler_(NSURL.fileURLWithPath_(str(d)), self._ublock_loaded)

    def _ublock_loaded(self, ext, err) -> None:
        self.hold_until = time.time() + UBLOCK_SETTLE if ext is not None else 0.0
        if ext is None:
            log.warning("browser: uBlock Origin Lite did not load: %s", err.localizedDescription() if err else "?")
            return
        ctx = WebKit.WKWebExtensionContext.contextForExtension_(ext)
        ctx.setUniqueIdentifier_(EXT_ID)  # a fixed identity: its settings survive a restart
        ctx.setBaseURL_(NSURL.URLWithString_(EXT_BASE))
        ctx.setInspectable_(True)
        granted = WebKit.WKWebExtensionContextPermissionStatusGrantedExplicitly
        ctx.setPermissionStatus_forMatchPattern_(granted, WebKit.WKWebExtensionMatchPattern.allURLsMatchPattern())
        for p in ext.requestedPermissions():
            ctx.setPermissionStatus_forPermission_(granted, p)
        ok, error = self.controller.loadExtensionContext_error_(ctx, None)
        if not ok:
            log.warning("browser: uBlock Origin Lite context did not load: %s", error)
            return
        self.context = ctx
        warnings = [str(e.localizedDescription()) for e in ext.errors()]
        log.info("browser: uBlock Origin Lite %s loaded%s", ext.version(), f" ({'; '.join(warnings)})" if warnings else "")

    def _ublock(self, page: Page, msg: dict):
        if self.context is None:
            return {"installed": False}
        self.popup_anchor = tuple(float(msg.get(k) or 0) for k in ("x", "y", "w", "h"))
        action = self.context.actionForTab_(page.ext)
        if action is None or not action.presentsPopup():
            self.context.performActionForTab_(page.ext)  # an action without a popup: it acts at once
            return {"installed": True}
        # WebKit loads the popup's page, but did not call presentActionPopup (macOS 26.7,
        # tested 2026-11-10): it may wait for a layout that a view outside any window never
        # gets. So the popup is shown here; the delegate stays, should a later WebKit call it.
        self.show_popup(action)
        return {"installed": True}

    def show_popup(self, action) -> None:
        if self.popover is not None and self.popup_action is action and self.popover.isShown():
            return  # shown already (the delegate after _ublock)
        pv = action.popupWebView()  # created and loaded at the first call
        if pv is None:
            return
        if self.popover is not None:
            self.popover.close()
        vc = AppKit.NSViewController.alloc().init()
        vc.setView_(pv)
        pop = AppKit.NSPopover.alloc().init()
        pop.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        pop.setContentViewController_(vc)
        pop.setContentSize_(POPUP_SIZE)
        pop.setDelegate_(self.pop_delegate)
        self.popover, self.popup_action = pop, action
        x, y, w, h = self.popup_anchor or (0, 0, 10, 10)
        flipped = self.ui.isFlipped()
        rect = NSMakeRect(x, y if flipped else self.ui.bounds().size.height - y - h, w, h)
        pop.showRelativeToRect_ofView_preferredEdge_(rect, self.ui, AppKit.NSMaxYEdge if flipped else AppKit.NSMinYEdge)

    # ---- my Firefox cookies

    def import_firefox(self, auto: bool = False) -> Future:
        """Copies my Firefox cookies into the store. Per site, Firefox's replace the store's:
        the cookies of every site that Firefox has cookies for are deleted first, so that two
        logins never mix. The sites that only Brain Fry knows keep theirs. The future holds
        the counts. Page loads wait meanwhile."""
        if self.importing is not None and not self.importing.done():
            return self.importing
        fut: Future = Future()
        self.importing, self.import_hold = fut, time.time() + IMPORT_MAX

        def finish(result=None, error=None) -> None:
            if fut.done():
                return
            self.import_hold = 0.0
            if error is not None:
                log.warning("browser: Firefox cookies not copied: %s", error)
                fut.set_exception(error)
            else:
                fut.set_result(result)
            if auto:
                self.core.notify({"type": "browser", "ev": "cookies", "auto": True, "error": str(error) if error else None, **(result or {})})

        def read() -> None:  # off the main thread: copying the database takes a moment
            try:
                rows, profile = firefox.read_cookies()
            except Exception as e:
                AppHelper.callAfter(finish, None, e)
                return
            AppHelper.callAfter(self._import_rows, rows, profile, finish)

        threading.Thread(target=read, name="firefox-cookies", daemon=True).start()
        AppHelper.callLater(IMPORT_MAX, lambda: finish(None, TimeoutError("WebKit did not answer")))  # never wait forever
        return fut

    def _import_rows(self, rows: list[dict], profile: str, finish) -> None:
        fut_done = self.importing.done
        hosts = {r["domain"].lstrip(".") for r in rows}
        cs, types = self.store.httpCookieStore(), {WebKit.WKWebsiteDataTypeCookies}
        n = {"set": 0, "left": len(rows)}

        def got_records(records) -> None:
            mine = [r for r in records if firefox.covers(str(r.displayName()), hosts)]
            n["replaced"] = len(mine)
            self.store.removeDataOfTypes_forDataRecords_completionHandler_(types, mine, removed)

        def removed() -> None:
            try:
                for row in rows:
                    c = NSHTTPCookie.cookieWithProperties_(cookie_props(row))
                    if c is None:  # a value that NSHTTPCookie refuses
                        n["left"] -= 1
                    else:
                        cs.setCookie_completionHandler_(c, one_set)
            except Exception as e:
                finish(None, e)
                return
            if n["left"] == 0:
                done()

        def one_set() -> None:
            n["set"] += 1
            n["left"] -= 1
            if n["left"] == 0:
                done()

        def done() -> None:
            if fut_done():
                return
            result = {"cookies": n["set"], "sites": len(hosts), "replaced": n.get("replaced", 0), "profile": profile, "time": time.time()}
            write_private(firefox.import_file(), json.dumps(result))
            log.info("browser: copied %d Firefox cookie(s) of %d host(s) from profile %s; the store's cookies of %d site(s) replaced",
                     result["cookies"], result["sites"], profile, result["replaced"])  # fmt: skip
            self.save_session_cookies()  # the replaced sites' session cookies are gone: so is their saved copy
            finish(result)

        self.store.fetchDataRecordsOfTypes_completionHandler_(types, got_records)

    # ---- session cookies

    def _restore_session_cookies(self) -> None:
        try:
            rows = json.loads(session_file().read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("browser: session cookies not restored: %s", e)
            return
        cs, n = self.store.httpCookieStore(), 0
        for row in rows:
            c = NSHTTPCookie.cookieWithProperties_(cookie_props(row))
            if c is not None:
                cs.setCookie_completionHandler_(c, None)
                n += 1
        log.info("browser: restored %d session cookie(s)", n)

    def save_session_cookies(self, then=None) -> None:
        def got(cookies):
            rows = sorted((cookie_row(c) for c in cookies if c.isSessionOnly()), key=lambda r: (r["domain"], r["name"], r["path"]))
            text = json.dumps(rows, indent=0)
            if text != self._saved_cookies:
                write_private(session_file(), text)
                self._saved_cookies = text
            if then is not None:
                then()

        self.store.httpCookieStore().getAllCookies_(got)

    def shutdown(self) -> None:
        """Main thread, once the window is gone: saves the session cookies (2 s at most)."""
        if not self.ready:
            return
        done: list[int] = []
        try:
            self.save_session_cookies(lambda: done.append(1))
            end = time.time() + 2
            while not done and time.time() < end:
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        except Exception:
            log.exception("browser: session cookies not saved at quit")
        log.info("browser: quit (%s)", "session cookies saved" if done else "session cookies NOT saved")
        if self.monitor is not None:
            AppKit.NSEvent.removeMonitor_(self.monitor)
