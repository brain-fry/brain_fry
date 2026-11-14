'use strict';
// Files and folders dragged from Finder (or another app) onto the window. See DESIGN_DETAILS.md,
// "Drop from Finder". What a drop does depends on where it lands:
// - Claude Code (4a) or a terminal tab: the paths are pasted at the cursor, escaped as
//   Terminal.app does. Claude Code turns the path of an image into an attachment.
// - a pane of column 3: each folder opens there as a Finder tab. A file: nothing, a toast
//   (Renaud, 2026-11-14: no file tab from a drop).
// - the explorer (2a): a copy into the folder under the mouse (on a file's row: its folder; on
//   the empty space: the project folder). Never a move, never a replace.
// - the header, or the board of Organize mode: each folder becomes a project (File > Add New Project...).
// Anywhere else the drop is refused. A browser page of 3a is a native view over this page: it
// takes the drop itself (an upload on a site), and this script never sees it.
// A page gets the names of dropped files, not their paths: the backend has them (drop.take,
// app/drops.py). While the drag is over the page, WebKit tells neither how many entries it holds
// nor their kinds (dataTransfer.items is empty until the drop), so the mark and its label depend
// on the place only.
// The listeners are on `window`, capture phase, and stop the event: the board's own drag of a
// card (app.js) and CodeMirror (it would insert the text of a dropped file into the open file)
// never see a drag of files.
// Uses S, $, request, toast, uiLog, wsId, wsProject, ownerFor, addProject (app.js), G, paneOf,
// paneActive, shownPanes (grid.js), work (files.js), newFinder, refreshFinders (finder.js),
// treeRow, nameOf, parentOf, errText, treesChanged (explorer.js), treeSearchShows, treeSearchNow
// (treesearch.js), revealColumn (layout.js), at call time.

// inner: a drag that started in this page (a card, an image of a tab); over: the element that the
// drag entered last; mark, label: the rectangle of the target and what a drop does there
const OSD = { inner: false, over: null, mark: null, label: null };

const osDragOfFiles = (e) => !OSD.inner && Boolean(e.dataTransfer) && [...e.dataTransfer.types].includes('Files');

// Where a drop on `el`, the element under the mouse at (x, y), goes: {kind, rect, label, ...},
// or null when this place takes no drop. A dialog over the page is the element under the mouse,
// so nothing behind it is a target.
function osDropTarget(el, x, y) {
  if (!el || !el.closest) return null;
  const rect = (node) => node.getBoundingClientRect();
  const project = (node) => ({ kind: 'project', rect: rect(node), label: 'Add the folder as a project' });
  if (el.closest('#top')) return project($('#top'));
  if (S.mode === 'organize') return el.closest('#board') ? project($('#board')) : null;
  const open = (t) => Boolean(t && t.ws && t.ws.readyState === WebSocket.OPEN);
  if (el.closest('#agent')) { // as `live` in ensureTerm: 4a types in Dev mode, and in Review mode while it shows
    const t = (S.mode === 'dev' || (S.mode === 'review' && S.reviewAgent)) && S.selected && S.terms.get(S.selected);
    const shows = open(t) && t.el.style.display !== 'none' && $('#overlay').hidden && $('#calm').hidden;
    return shows ? { kind: 'term', term: t, rect: rect($('#agent')), label: 'Paste the path into Claude Code' } : null;
  }
  if (S.mode !== 'dev' || !wsId()) return null;
  if (el.closest('#middle')) {
    const w = work(wsId());
    const inside = (n) => { const r = rect(G.els[n].pane); return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom; };
    const n = paneOf(el) || shownPanes(w).find(inside) || shownPanes(w)[0]; // a splitter: the pane next to it
    const a = paneActive(w, n);
    if (a && a.shell && open(a.term) && el.closest('.pane-body')) {
      return { kind: 'term', term: a.term, rect: rect(G.els[n].body), label: 'Paste the path into the terminal' };
    }
    return { kind: 'pane', pane: n, rect: rect(G.els[n].pane), label: 'Open the folder as a Finder tab' };
  }
  if (el.closest('#explorer')) {
    const tid = wsId();
    const root = wsProject(tid);
    if (!root || S.treeRename || el.closest('#tree-search')) return null;
    const node = el.closest('#tree .node[data-node]');
    const into = !node ? '' : node.dataset.dir === '1' ? node.dataset.node : parentOf(node.dataset.node);
    const row = into ? treeRow(into) : null; // the folder's row; the project folder, or a row off screen: the whole tree
    return { kind: 'tree', tid, into, rect: rect(row || $('#tree-wrap')), label: `Copy into ${nameOf(into || root)}/` };
  }
  return null;
}

// The mark on the target and its label next to the mouse; t null: none.
function osDropShow(t, x = 0, y = 0) {
  if (!OSD.mark) {
    OSD.mark = document.createElement('div');
    OSD.mark.className = 'drop-mark'; // the mark of a dragged tab (grid.js)
    OSD.label = document.createElement('div');
    OSD.label.className = 'tab-ghost';
    OSD.mark.hidden = OSD.label.hidden = true;
    document.body.append(OSD.mark, OSD.label);
  }
  OSD.mark.hidden = OSD.label.hidden = !t;
  if (!t) return;
  const r = t.rect;
  Object.assign(OSD.mark.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  OSD.label.textContent = t.label;
  const left = Math.max(4, Math.min(x + 16, innerWidth - OSD.label.offsetWidth - 4)); // the whole label stays in the window
  const top = Math.max(4, Math.min(y + 20, innerHeight - OSD.label.offsetHeight - 4));
  OSD.label.style.transform = `translate(${left}px, ${top}px)`;
}

// ------------------------------------------------------------------ what a drop does

// A path as Terminal.app types a dropped file: a backslash before every character that a shell
// reads as something else. Claude Code reads such a paste as paths too (its paste handler, read
// in the binary of 2.1.x): it splits at a space that a `/` follows, removes the backslashes, and
// attaches a path that ends in .png, .jpg, .jpeg, .gif or .webp as an image.
const osShellPath = (p) => p.replace(/[^A-Za-z0-9_\-.,:+=@%/\u0080-￿]/g, '\\$&');

// t: the terminal of S.terms (Claude Code's, or a terminal tab's). xterm.js's paste() wraps the
// text as the program asked (bracketed paste), and the text goes through the terminal's own
// input, so a hidden terminal drops it as it drops keys. A space follows each path, as in
// Terminal.app. The keyboard goes to the terminal: the next thing I do is type.
function osDropPaste(t, paths) {
  const ok = paths.filter((p) => !/[\u0000-\u001f\u007f]/.test(p)); // a line break in a name would run what follows it
  if (ok.length < paths.length) toast('A name with a control character was left out.');
  if (!ok.length) return;
  uiLog(`drop: ${ok.length} path${ok.length === 1 ? '' : 's'} pasted into ${t.shell ? 'a terminal tab' : 'Claude Code'}`);
  t.term.paste(`${ok.map(osShellPath).join(' ')} `);
  t.term.focus();
}

// Each folder: a Finder tab in pane n, the last one on screen with the keyboard (as Cmd+N E).
function osDropFinder(n, dirs) {
  if (!dirs.length) {
    toast('A pane takes folders: each one opens as a Finder tab. Drop a file on Claude Code, on a terminal or on the explorer.');
    return;
  }
  if (S.mode !== 'dev' || !wsId()) return; // the view changed while the backend answered
  const tid = ownerFor(wsId()); // with no task selected: project tabs
  uiLog(`drop: ${dirs.length} folder${dirs.length === 1 ? '' : 's'} as Finder tab${dirs.length === 1 ? '' : 's'} in pane ${n}`);
  revealColumn('middle');
  for (const d of dirs) newFinder(tid, n, d);
}

// A copy into the folder `into` of the project ('' is the project folder), with the rules of
// Paste: a name that is taken gets a free one ("a copy.py"), nothing is replaced.
async function osDropCopy(tid, into, paths) {
  const root = wsProject(tid);
  if (!root) return;
  let pairs = [];
  try {
    pairs = await request('finder.paste', { paths, dir: into ? `${root}/${into}` : root, cut: false });
  } catch (e) { toast(errText(e)); } // the entries before the one that failed are copied: the tree must show them
  const w = work(tid);
  if (into) w.open.add(into);
  if (pairs.length) w.treeSel = pairs[pairs.length - 1][1].slice(root.length + 1);
  treesChanged(root);
  refreshFinders();
  if (pairs.length && treeSearchShows(treeSearchNow())) { // the results of the search hide the tree
    toast(`Copied ${pairs.length === 1 ? nameOf(pairs[0][1]) : `${pairs.length} entries`} into ${nameOf(into || root)}/`);
  }
}

async function osDropRun(t, names) {
  let got;
  try { got = await request('drop.take', { names }); } catch (e) { toast(`The drop failed: ${e.message}`); return; }
  if (!got.length) {
    toast('Could not read the path of what was dropped. Brain Fry takes files and folders dragged from Finder.');
    return;
  }
  const paths = got.map((g) => g.path);
  const dirs = got.filter((g) => g.dir).map((g) => g.path);
  if (t.kind === 'term') osDropPaste(t.term, paths);
  else if (t.kind === 'pane') osDropFinder(t.pane, dirs);
  else if (t.kind === 'tree') osDropCopy(t.tid, t.into, paths);
  else if (!dirs.length) toast('Drop a folder here to add it as a project.');
  else for (const d of dirs) addProject(d); // in their order: the window ends on the last one
}

// ------------------------------------------------------------------ events

function osDragOver(e) {
  if (!osDragOfFiles(e)) return;
  e.preventDefault(); // without it WebKit refuses every drop, or lets an editor take it
  e.stopPropagation();
  const t = osDropTarget(e.target, e.clientX, e.clientY);
  e.dataTransfer.dropEffect = t ? 'copy' : 'none'; // none: the cursor says no, and no drop event comes
  osDropShow(t, e.clientX, e.clientY);
}

window.addEventListener('dragenter', (e) => {
  if (osDragOfFiles(e)) OSD.over = e.target;
  osDragOver(e);
}, true);
window.addEventListener('dragover', osDragOver, true);
// The drag enters the next element before it leaves the last one: a leave of the element entered
// last means that the drag left the window.
window.addEventListener('dragleave', (e) => {
  if (!osDragOfFiles(e) || e.target !== OSD.over) return;
  OSD.over = null;
  osDropShow(null);
}, true);
window.addEventListener('drop', (e) => {
  if (!osDragOfFiles(e)) return;
  e.preventDefault();
  e.stopPropagation();
  OSD.over = null;
  osDropShow(null);
  const t = osDropTarget(e.target, e.clientX, e.clientY);
  if (t) osDropRun(t, [...e.dataTransfer.files].map((f) => f.name));
}, true);

// A drag that starts in this page is not a drop from Finder, whatever it carries (an image of a
// tab carries a file). No mouse event comes during a drag, so a mouse event says that none runs:
// it clears the flag when `dragend` was lost (the dragged element was redrawn), and a mark that
// a lost `dragleave` left on screen.
window.addEventListener('dragstart', () => { OSD.inner = true; }, true);
window.addEventListener('dragend', () => { OSD.inner = false; }, true);
for (const type of ['mousedown', 'mousemove']) {
  window.addEventListener(type, () => {
    OSD.inner = false;
    if (OSD.mark && !OSD.mark.hidden) osDropShow(null);
  }, true);
}
