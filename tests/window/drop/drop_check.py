"""Checks the drops from Finder (web/drop.js, app/drops.py) in a real window of Brain Fry, on a
sandbox: Claude Code (4a), a terminal tab, a pane, the explorer, the header, the board, the
places that refuse, the mark and its label, and the drag of a card, which must still work.

    uv run python tests/window/drop/drop_check.py WORKDIR

A drop is simulated at the level of AppKit, as WebKit's own tests do (DragAndDropSimulator): an
object that stands for the drag (NSDraggingInfo), with a pasteboard of its own that holds the
file URLs, is given to the view of the page (draggingEntered:, draggingUpdated:,
performDragOperation:). The mouse and the system's drag pasteboard are not touched, and the
page gets real dragenter, dragover and drop events, so pywebview's record of the paths is
checked too. Not checked: the drag session of Finder itself (what it puts on the pasteboard).

WORKDIR: a scratch folder, emptied first. It holds the project, the Brain Fry home, $HOME (so
that the bash of a terminal tab reads no profile of mine) and a fake `claude` that turns on
bracketed paste, as Claude Code does, and writes what it is sent to a file. The window comes to
the front without taking the keyboard, and closes at the end. Exit code 1 if a check fails.
DROP_CHECK_SHOTS=DIR saves a picture of the window during each drag (the mark and its label).
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import unicodedata

WORK = os.path.realpath(sys.argv[1])
SHOTS = os.environ.get("DROP_CHECK_SHOTS")
shutil.rmtree(WORK, ignore_errors=True)
PROJ, OUT, CLAUDE_IN = f"{WORK}/proj", f"{WORK}/outside", f"{WORK}/claude_in.bin"
for d in (f"{PROJ}/sub", f"{OUT}/some folder", f"{OUT}/other proj", f"{OUT}/third proj", f"{WORK}/home", f"{WORK}/claude_config"):
    os.makedirs(d)
NFD = unicodedata.normalize("NFD", "café.txt")  # a decomposed name, as some tools write them
ODD = "it's (1) é & $x.png"
for path, text in ((f"{PROJ}/a.py", "print('a')\n"), (f"{PROJ}/sub/b.md", "# b\n"), (f"{OUT}/notes.txt", "notes\n"),
                   (f"{OUT}/some folder/inner.txt", "inner\n"), (f"{OUT}/pic.png", "not a real image\n"),
                   (f"{OUT}/{ODD}", "x\n"), (f"{OUT}/{NFD}", "x\n")):  # fmt: skip
    with open(path, "w") as f:
        f.write(text)
FAKE_CLAUDE = f"""#!{sys.executable}
import os, sys, tty
sys.stdout.write("\\x1b[?2004hfake claude\\r\\n")
sys.stdout.flush()
tty.setraw(0)
with open({CLAUDE_IN!r}, "ab", buffering=0) as f:
    while True:
        data = os.read(0, 4096)
        if not data:
            break
        f.write(data)
"""
with open(f"{WORK}/fake_claude", "w") as f:
    f.write(FAKE_CLAUDE)
os.chmod(f"{WORK}/fake_claude", 0o755)
os.environ.update(HOME=f"{WORK}/home", BRAIN_FRY_HOME=f"{WORK}/home/.brain_fry", BRAIN_FRY_CLAUDE=f"{WORK}/fake_claude",
                  CLAUDE_CONFIG_DIR=f"{WORK}/claude_config")  # fmt: skip

import objc  # noqa: E402
from AppKit import NSObject, NSPasteboard  # noqa: E402
from Foundation import NSURL, NSPoint  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402

from brain_fry import projects  # noqa: E402
from brain_fry.app import main  # noqa: E402

projects.init(PROJ)
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}".rstrip(), flush=True)


class FakeDrag(NSObject, protocols=[objc.protocolNamed("NSDraggingInfo")]):
    """A drag that comes from another app (no source), over the point `loc` of the window."""

    def draggingDestinationWindow(self):
        return self.win

    def draggingSourceOperationMask(self):
        return 0xFFFFFFFFFFFFFFFF  # NSDragOperationEvery

    def draggingLocation(self):
        return self.loc

    def draggedImageLocation(self):
        return self.loc

    def draggedImage(self):
        return None

    def draggingPasteboard(self):
        return self.pb

    def draggingSource(self):
        return None

    def draggingSequenceNumber(self):
        return 1

    def slideDraggedImageTo_(self, point):
        pass

    def namesOfPromisedFilesDroppedAtDestination_(self, url):
        return None

    def draggingFormation(self):
        return 0

    def setDraggingFormation_(self, v):
        pass

    def animatesToDestination(self):
        return False

    def setAnimatesToDestination_(self, v):
        pass

    def numberOfValidItemsForDrop(self):
        return self.n

    def setNumberOfValidItemsForDrop_(self, v):
        pass

    def enumerateDraggingItemsWithOptions_forView_classes_searchOptions_usingBlock_(self, o, v, c, s, b):
        pass

    def springLoadingHighlight(self):
        return 0

    def resetSpringLoading(self):
        pass


def on_main(fn):
    done, out = threading.Event(), {}

    def go():
        try:
            out["v"] = fn()
        except Exception as e:
            out["e"] = e
        done.set()

    AppHelper.callAfter(go)
    if not done.wait(15):
        raise TimeoutError("the main thread did not answer")
    if "e" in out:
        raise out["e"]
    return out.get("v")


def page_view(view):
    from WebKit import WKWebView  # noqa: PLC0415

    if view.isKindOfClass_(WKWebView):
        return view
    for sub in view.subviews():
        if found := page_view(sub):
            return found
    return None


MARK_JS = """(() => {
  const m = document.querySelector('body > .drop-mark:not([hidden])');
  const l = [...document.querySelectorAll('body > .tab-ghost')].find((x) => !x.hidden);
  const r = m && m.getBoundingClientRect();
  return { mark: r ? [r.left, r.top, r.width, r.height].map(Math.round) : null, label: l ? l.textContent : null };
})()"""
TERM_TEXT_JS = """((t) => { const b = t.term.buffer.active; let out = '';
  for (let i = 0; i < b.length; i++) { const next = i + 1 < b.length && b.getLine(i + 1).isWrapped;
    out += (b.getLine(i).isWrapped ? '' : '\\n') + b.getLine(i).translateToString(!next); }
  return out; })"""
SHELL_JS = "viewTabs(work(wsId())).find((t) => t.shell).term"


class Window:
    def __init__(self, window):
        self.window = window
        self.shot = 0
        self.view = on_main(lambda: page_view(window.native.contentView()))

    def js(self, code):
        return self.window.evaluate_js(code)

    def wait(self, cond, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(f"Boolean({cond})"):
                return True
            time.sleep(0.1)
        return False

    def rect(self, sel):
        return self.js(f"(() => {{ const r = document.querySelector({json.dumps(sel)}).getBoundingClientRect(); "
                       "return [r.left, r.top, r.width, r.height].map(Math.round); })()")  # fmt: skip

    def at(self, sel, fx=0.5, fy=0.5):
        left, top, width, height = self.rect(sel)
        return left + width * fx, top + height * fy

    def toast(self):
        return self.js("$('#toast').hidden ? '' : $('#toast').textContent")

    def drop(self, paths, xy):
        """One drag of `paths` from another app to the point `xy` of the page. Returns the drag
        operations that WebKit answered (0: refused, 1: copy) and what the page showed (mark and
        label) just before the mouse was let go."""
        self.js("$('#toast').hidden = true; $('#toast').textContent = ''; 1")
        x, y = xy

        def start():
            pb = NSPasteboard.pasteboardWithUniqueName()
            pb.clearContents()
            pb.writeObjects_([NSURL.fileURLWithPath_(p) for p in paths])
            info = FakeDrag.alloc().init()
            info.pb, info.win, info.n = pb, self.view.window(), len(paths)
            info.loc = self.view.convertPoint_toView_(NSPoint(x, y), None)
            return info, self.view.draggingEntered_(info)

        info, op = on_main(start)
        ops = [op]
        time.sleep(0.25)
        for _ in range(3):
            ops.append(on_main(lambda: self.view.draggingUpdated_(info)))
            time.sleep(0.15)
        shown = self.js(MARK_JS)
        if SHOTS:
            self.shot += 1
            main._screenshot(self.window, f"{SHOTS}/{self.shot:02d}.png")
        if ops[-1]:
            on_main(lambda: self.view.prepareForDragOperation_(info) and self.view.performDragOperation_(info))
            on_main(lambda: self.view.concludeDragOperation_(info))
        else:
            on_main(lambda: self.view.draggingExited_(info))
        time.sleep(0.4)
        return ops, shown


def escaped(path):  # what Terminal.app types for a dropped file; written again here, not copied from drop.js
    return re.sub(r"[^A-Za-z0-9_\-.,:+=@%/\u0080-\U0010ffff]", lambda m: "\\" + m.group(), path)


def near(a, b, px=3):
    return bool(a and b) and all(abs(x - y) <= px for x, y in zip(a, b, strict=True))


def nfc(s):
    return unicodedata.normalize("NFC", s)


def checks(w: Window):
    from webview.dom import _dnd_state  # noqa: PLC0415

    js = w.js
    check("the page is ready", w.wait("S.state && typeof osDropTarget === 'function'", 10))
    on_main(lambda: w.window.native.orderFrontRegardless())  # on screen (WebKit pauses a covered page), without the keyboard
    time.sleep(0.5)
    print("     visibility:", js("document.visibilityState"))
    check("pywebview records the paths of drops", _dnd_state.get("num_listeners", 0) >= 1, _dnd_state)

    # ---- the header: a folder becomes a project
    ops, shown = w.drop([f"{OUT}/other proj"], w.at("#top"))
    check("header: the drop is accepted", ops[-1] == 1, ops)
    check("header: the label", shown["label"] == "Add the folder as a project", shown)
    check("header: the mark is on the header", near(shown["mark"], w.rect("#top")), (shown, w.rect("#top")))
    check("header: the folder is the project on screen", w.wait(f"currentProject() === {json.dumps(OUT + '/other proj')}"), js("currentProject()"))
    check("header: the folder is registered", f"{OUT}/other proj" in [p["path"] for p in projects.load()["projects"]])
    check("header: the mark is gone after the drop", js(MARK_JS) == {"mark": None, "label": None}, js(MARK_JS))
    w.drop([f"{OUT}/notes.txt"], w.at("#top"))
    check("header: a file gives a toast", w.wait("$('#toast').textContent.includes('Drop a folder here')"), w.toast())
    check("header: a file adds no project", len(projects.load()["projects"]) == 2, projects.load())

    # ---- no task selected: 4a takes nothing
    ops, shown = w.drop([f"{OUT}/notes.txt"], w.at("#agent"))
    check("4a without a task: refused", ops[-1] == 0 and shown["mark"] is None, (ops, shown))

    # ---- Claude Code (4a)
    js(f"newTask({json.dumps(PROJ)}); 1")
    check("a task with its session", w.wait("S.selected && S.terms.get(S.selected) && S.terms.get(S.selected).ws && S.terms.get(S.selected).ws.readyState === 1", 10))
    time.sleep(1.5)  # the fake claude turns on bracketed paste
    many = [f"{OUT}/pic.png", f"{OUT}/{ODD}", f"{OUT}/{NFD}", f"{OUT}/some folder"]
    ops, shown = w.drop(many, w.at("#agent"))
    check("4a: the label", shown["label"] == "Paste the path into Claude Code", shown)
    check("4a: the mark is on 4a", near(shown["mark"], w.rect("#agent")), (shown, w.rect("#agent")))
    want = "\x1b[200~" + " ".join(escaped(p) for p in many) + " \x1b[201~"
    end = time.time() + 5
    got = ""
    while time.time() < end and "\x1b[201~" not in got:
        time.sleep(0.1)
        got = open(CLAUDE_IN, "rb").read().decode() if os.path.exists(CLAUDE_IN) else ""
    check("4a: the escaped paths arrive as one bracketed paste", nfc(got) == nfc(want), f"\n     got  {got!r}\n     want {want!r}")
    check("4a: the keyboard is in Claude Code's terminal",
          js("S.terms.get(S.selected).el.contains(document.activeElement)"), js("document.activeElement && document.activeElement.className"))  # fmt: skip

    # ---- a terminal tab
    js("create('t', 1); 1")
    check("a terminal tab", w.wait(f"viewTabs(work(wsId())).some((t) => t.shell) && {SHELL_JS}.ws && {SHELL_JS}.ws.readyState === 1", 10))
    w.wait(f"{TERM_TEXT_JS}({SHELL_JS}).includes('$')", 8)  # the prompt of bash
    before = os.path.getsize(CLAUDE_IN)
    ops, shown = w.drop([f"{OUT}/notes.txt", f"{OUT}/some folder"], w.at('.pane[data-pane="1"] .pane-body'))
    check("terminal tab: the label", shown["label"] == "Paste the path into the terminal", shown)
    check("terminal tab: the mark is on the terminal", near(shown["mark"], w.rect('.pane[data-pane="1"] .pane-body')), shown)
    typed = f"{escaped(OUT + '/notes.txt')} {escaped(OUT + '/some folder')} "
    check("terminal tab: the escaped paths are typed at the prompt",
          w.wait(f"{TERM_TEXT_JS}({SHELL_JS}).includes({json.dumps(typed)})"), js(f"{TERM_TEXT_JS}({SHELL_JS})"))  # fmt: skip
    check("terminal tab: nothing went to Claude Code", os.path.getsize(CLAUDE_IN) == before)
    check("terminal tab: the keyboard is in it", js(f"{SHELL_JS}.el.contains(document.activeElement)"))

    # ---- a pane: a folder opens as a Finder tab, a file does nothing
    finders = "viewTabs(work(wsId())).filter((t) => t.finder).map((t) => [t.pane, t.root])"
    ops, shown = w.drop([f"{OUT}/some folder"], w.at('.pane[data-pane="1"] .pane-tabs', 0.8, 0.5))
    check("tab bar of a terminal's pane: the label", shown["label"] == "Open the folder as a Finder tab", shown)
    check("tab bar: the mark is on the pane", near(shown["mark"], w.rect('.pane[data-pane="1"]')), shown)
    check("tab bar: a Finder tab at the folder, in pane 1",
          w.wait(f"{finders}.length === 1 && {finders}[0][0] === 1 && {finders}[0][1] === {json.dumps(OUT + '/some folder')}"), js(finders))  # fmt: skip
    check("tab bar: the Finder tab is on screen", js("String(work(wsId()).act[1]).startsWith('finder:')"), js("work(wsId()).act[1]"))
    js("openFile(wsId(), 'a.py', true, 2); 1")
    check("a file in pane 2", w.wait("document.querySelector('.pane[data-pane=\"2\"] .cm-content')"))
    doc = "viewTabs(work(wsId())).find((t) => t.path === 'a.py').view.state.doc.toString()"
    tabs = js("viewTabs(work(wsId())).length")
    ops, shown = w.drop([f"{OUT}/notes.txt"], w.at('.pane[data-pane="2"] .cm-content'))
    check("editor: a file gives a toast", w.wait("$('#toast').textContent.includes('A pane takes folders')"), w.toast())
    check("editor: the text of the open file is not changed", js(doc) == "print('a')\n", js(doc))
    check("editor: no new tab", js("viewTabs(work(wsId())).length") == tabs)
    w.drop([f"{OUT}/notes.txt", f"{OUT}/third proj"], w.at('.pane[data-pane="2"] .cm-content'))
    check("editor: a folder (with a file) opens as a Finder tab in pane 2",
          w.wait(f"{finders}.some(([n, root]) => n === 2 && root === {json.dumps(OUT + '/third proj')})"), js(finders))  # fmt: skip
    check("editor: the text is still unchanged", js(doc) == "print('a')\n", js(doc))

    # ---- the explorer: a copy into the folder under the mouse
    row = lambda p: f'#tree .node[data-node="{p}"]'  # noqa: E731
    check("the tree shows", w.wait(f"document.querySelector({json.dumps(row('sub'))})"))
    ops, shown = w.drop([f"{OUT}/notes.txt", f"{OUT}/some folder"], w.at(row("sub")))
    check("explorer, on a folder: the label", shown["label"] == "Copy into sub/", shown)
    check("explorer, on a folder: the mark is on its row", near(shown["mark"], w.rect(row("sub"))), (shown, w.rect(row("sub"))))
    check("explorer, on a folder: copied into it", w.wait("work(wsId()).treeSel === 'sub/some folder'")
          and os.path.isfile(f"{PROJ}/sub/notes.txt") and os.path.isfile(f"{PROJ}/sub/some folder/inner.txt"), os.listdir(f"{PROJ}/sub"))  # fmt: skip
    check("explorer: the folder is open and shows the copies", w.wait(f"document.querySelector({json.dumps(row('sub/notes.txt'))})"))
    check("explorer: a copy, not a move", os.path.isfile(f"{OUT}/notes.txt") and os.path.isfile(f"{OUT}/some folder/inner.txt"))
    ops, shown = w.drop([f"{OUT}/notes.txt"], w.at(row("sub/b.md")))
    check("explorer, on a file in a folder: its folder", shown["label"] == "Copy into sub/" and near(shown["mark"], w.rect(row("sub"))), shown)
    check("explorer: a name that is taken gets a free one", w.wait("work(wsId()).treeSel === 'sub/notes copy.txt'") and os.path.isfile(f"{PROJ}/sub/notes copy.txt"),
          os.listdir(f"{PROJ}/sub"))  # fmt: skip
    ops, shown = w.drop([f"{OUT}/notes.txt"], w.at(row("a.py")))
    check("explorer, on a file of the project folder: the label", shown["label"] == "Copy into proj/", shown)
    check("explorer, on a file of the project folder: the mark is on the tree", near(shown["mark"], w.rect("#tree-wrap")), shown)
    check("explorer: copied into the project folder", w.wait("work(wsId()).treeSel === 'notes.txt'") and os.path.isfile(f"{PROJ}/notes.txt"), os.listdir(PROJ))
    left, top, width, height = w.rect("#tree-wrap")
    ops, shown = w.drop([f"{OUT}/pic.png"], (left + width / 2, top + height - 12))
    check("explorer, on the empty space: the project folder", shown["label"] == "Copy into proj/" and w.wait("work(wsId()).treeSel === 'pic.png'")
          and os.path.isfile(f"{PROJ}/pic.png"), (shown, os.listdir(PROJ)))  # fmt: skip

    # ---- places that take nothing
    ops, shown = w.drop([f"{OUT}/notes.txt"], w.at("#side"))
    check("inbox column: refused, no mark", ops[-1] == 0 and shown == {"mark": None, "label": None}, (ops, shown))
    check("inbox column: no toast", w.toast() == "", w.toast())

    # ---- a file that does not come from Finder: no path
    x, y = w.at("#agent")
    js(f"""(() => {{ const dt = new DataTransfer(); dt.items.add(new File(['x'], 'ghost.txt'));
      const ev = (type) => new DragEvent(type, {{ bubbles: true, cancelable: true, dataTransfer: dt, clientX: {x}, clientY: {y} }});
      const el = document.elementFromPoint({x}, {y}); el.dispatchEvent(ev('dragenter')); el.dispatchEvent(ev('dragover')); el.dispatchEvent(ev('drop')); }})(); 1""")
    check("a file without a path: a toast", w.wait("$('#toast').textContent.includes('Could not read the path')"), w.toast())
    check("a file without a path: the mark is gone", js(MARK_JS) == {"mark": None, "label": None}, js(MARK_JS))

    # ---- Organize mode: the drag of a card still works, and the board takes a folder
    js("setMode('organize'); 1")
    check("the board shows the card", w.wait("document.querySelector('#board .card[data-card]')"))
    moved = js("""(() => { const card = document.querySelector('#board .card[data-card]'); const id = card.dataset.card;
      const col = document.querySelector('#board .column[data-prio="next"]'); const dt = new DataTransfer();
      const ev = (type) => new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt });
      card.dispatchEvent(ev('dragstart')); col.dispatchEvent(ev('dragover')); col.dispatchEvent(ev('drop'));
      document.querySelector(`#board .card[data-card="${id}"]`).dispatchEvent(ev('dragend'));
      return S.state.tasks[id].prio; })()""")
    check("board: a card dragged to another column moves", moved == "next", moved)
    board = w.rect("#board")  # before the drop: the new project shows in Dev mode
    ops, shown = w.drop([f"{OUT}/third proj"], w.at("#board"))
    check("board: the label and the mark", shown["label"] == "Add the folder as a project" and near(shown["mark"], board), (shown, board))
    check("board: the folder is the project on screen", w.wait(f"currentProject() === {json.dumps(OUT + '/third proj')}"), js("currentProject()"))
    check("board: the folder is registered", f"{OUT}/third proj" in [p["path"] for p in projects.load()["projects"]])
    check("no JS error", not js("window.__errors"), js("window.__errors"))
    check("pywebview's record is empty at the end", _dnd_state["paths"] == [], _dnd_state["paths"])


def run_checks(window, *_):
    try:
        time.sleep(3)
        checks(Window(window))
    except Exception:
        traceback.print_exc()
        check("the run reached its end", False)
    finally:
        window.destroy()


main._selftest = run_checks  # webview.start() runs it in a thread, as the selftest of bf
main.run(selftest=1.0)
failed = [name for name, ok, _ in results if not ok]
print(f"{len(results) - len(failed)} passed, {len(failed)} failed" + (f": {failed}" if failed else ""))
sys.exit(1 if failed or not results else 0)
