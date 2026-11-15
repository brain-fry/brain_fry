'use strict';
// The right-click menu of the explorer (2a) and its keys, as in VS Code on macOS: Enter (or F2)
// renames the selected entry, Cmd+Backspace moves it to the Trash, Ctrl+C copies its relative path. Cut / Copy / Paste move or copy
// files and folders, also between projects. New File... / New Folder... into the folder that was
// right-clicked, the file's folder, or the project folder (the empty space). A row dragged onto a
// folder moves there.
// See DESIGN_DETAILS.md "2a, 3a, 3b: files and terminals".
// Uses S, $, esc, request, send, toast, store, uiLog (app.js), work, loadDir, refreshTree, renderExplorer,
// renderFiles, saveOpenFiles, closeFile, openFile (files.js) and newShell (shells.js).

// What Cut or Copy put aside for Paste: { tid, project, paths, cut }. Kept in Brain Fry only,
// not on the clipboard of macOS: Finder cannot paste it.
S.fileClip = null;
S.treeRename = null; // { tid, path } or { tid, dir } while a name field is open in the tree (Rename, New File..., New Folder...)

const nameOf = (path) => path.split('/').pop();
const parentOf = (path) => path.split('/').slice(0, -1).join('/');
const errText = (e) => e.message.replace(/^\w+Error: /, ''); // "FileExistsError: a.py already exists here"

function isCut(project, path) { // renderExplorer dims it
  const c = S.fileClip;
  return Boolean(c && c.cut && c.project === project && c.paths.includes(path));
}

function treeRow(path) {
  return [...document.querySelectorAll('#tree .node[data-node]')].find((n) => n.dataset.node === path) || null;
}

// The folder that a drop on the row `node` goes into (a drag in the tree, or from Finder, drop.js):
// a folder's row, that folder; a file's row, its folder; no row (the empty space), the project folder ('').
function treeInto(node) {
  return !node ? '' : node.dataset.dir === '1' ? node.dataset.node : parentOf(node.dataset.node);
}

function sameProject(project, fn) { // fn(id, its work) for each task of the project that has a work state, and its own (wsId)
  for (const [id, w] of Object.entries(S.work || {})) {
    if (wsProject(id) === project) fn(id, w);
  }
}

// ------------------------------------------------------------------ the menu

let ctxNow = []; // the items on screen

// `path` null: the empty space under the tree, which stands for the project folder
function ctxItems(tid, path, dir) {
  const c = S.fileClip;
  const into = path === null ? '' : dir ? path : parentOf(path);
  const paste = { label: c ? `Paste ${nameOf(c.paths[0])}${c.cut ? ' (move)' : ''}` : 'Paste', disabled: !c, run: () => pasteInto(tid, into) };
  const refresh = { label: 'Refresh', run: () => refreshTree(tid) };
  const create = [ // into the folder, the file's folder, or the project folder (asked by Renaud on 2026-11-12)
    { label: 'New File...', run: () => startCreate(tid, into, false) },
    { label: 'New Folder...', run: () => startCreate(tid, into, true) },
    '-',
  ];
  // Open in Finder (asked by Renaud on 2026-11-14): a folder opens in Finder, a file shows selected
  // in a Finder window on its folder (VS Code's Reveal in Finder), the empty space opens the project folder
  if (path === null) return [...create, { label: 'Open in Finder', run: () => send('fs.open', { id: tid, path: '' }) }, '-', paste, '-', refresh];
  // a markdown file: as VS Code's Open Preview (asked by Renaud on 2026-11-14, mdpreview.js)
  const preview = !dir && mdPreviewable(path) ? [{ label: 'Open Preview', run: () => openPreview(tid, path) }] : [];
  return [
    ...create,
    ...preview,
    ...(dir ? [] : [{ label: 'Open in default app', run: () => send('fs.open', { id: tid, path }) }]),
    { label: 'Open With', sub: () => openWithItems('fs.apps', { id: tid, path }, (app) => send('fs.open', { id: tid, path, app })) },
    { label: 'Open in Finder', run: () => send('fs.open', { id: tid, path, reveal: !dir }) },
    { label: 'Open a terminal here', run: () => terminalHere(tid, path) },
    '-',
    { label: 'Cut', run: () => setClip(tid, path, true) },
    { label: 'Copy', run: () => setClip(tid, path, false) },
    paste,
    '-',
    { label: 'Copy path', run: () => copyPath(tid, path, false) },
    { label: 'Copy relative path', key: '⌃C', run: () => copyPath(tid, path, true) },
    '-',
    { label: 'Rename', key: 'Enter', run: () => startRename(tid, path) },
    { label: 'Delete', key: '⌘⌫', run: () => deletePath(tid, path, dir) },
    '-',
    refresh,
  ];
}

function ctxHtml(items, attr) {
  return items.map((it, i) => (it === '-' ? '<hr>'
    : `<button data-${attr}="${i}" ${it.disabled ? 'disabled' : ''}>${esc(it.label)}${it.key ? `<kbd>${esc(it.key)}</kbd>` : ''}${it.sub ? '<b>›</b>' : ''}</button>`)).join('');
}

function openCtx(x, y, items) {
  const m = $('#ctx');
  ctxReset();
  ctxNow = items;
  m.innerHTML = ctxHtml(items, 'ctx');
  m.hidden = false; // covers the browser view: browser.js shows its snapshot meanwhile
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.max(4, Math.min(x, innerWidth - r.width - 4))}px`;
  m.style.top = `${Math.max(4, Math.min(y, innerHeight - r.height - 4))}px`;
}

function closeCtx() { closeSub(); ctxReset(); $('#ctx').hidden = true; }

// A submenu (Open With): an item whose `sub` gives a Promise of its items. It opens as in Finder,
// on hover after SUB_OPEN_MS, or at once on a click, beside its item (on the left when the right
// has no room), moved up to fit the window. The mouse may cross the other items on its way to it:
// it closes only if the mouse stays SUB_GRACE_MS on another item. It sits inside #ctx, so a click
// outside, Esc and the browser's cover (browser.js) treat both menus as one.
const SUB_OPEN_MS = 150;
const SUB_GRACE_MS = 300;
let ctxSub = { i: -1, items: [], openTimer: 0, closeTimer: 0, seq: 0 };

function ctxReset() {
  clearTimeout(ctxSub.openTimer);
  clearTimeout(ctxSub.closeTimer);
  ctxSub = { i: -1, items: [], openTimer: 0, closeTimer: 0, seq: ctxSub.seq + 1 };
}

function closeSub() {
  clearTimeout(ctxSub.closeTimer);
  $('#ctx .ctx-sub')?.remove();
  $('#ctx').querySelector('button.open')?.classList.remove('open');
  ctxSub.i = -1;
  ctxSub.items = [];
}

async function openSub(i) {
  clearTimeout(ctxSub.openTimer);
  clearTimeout(ctxSub.closeTimer);
  if (ctxSub.i === i) return;
  closeSub();
  const it = ctxNow[i];
  const seq = ++ctxSub.seq;
  let items;
  try { items = await (it.subItems ??= it.sub()); } catch (e) { it.subItems = null; toast(errText(e)); return; }
  if (seq !== ctxSub.seq || $('#ctx').hidden) return; // the menu closed, or another submenu opened, meanwhile
  const btn = $('#ctx').querySelector(`button[data-ctx="${i}"]`);
  if (!btn) return;
  const el = document.createElement('div');
  el.className = 'ctx-menu ctx-sub';
  el.innerHTML = ctxHtml(items, 'sub');
  $('#ctx').append(el);
  ctxSub.i = i;
  ctxSub.items = items;
  btn.classList.add('open');
  const m = $('#ctx').getBoundingClientRect();
  const b = btn.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const left = m.right + r.width + 4 <= innerWidth ? m.right - 1 : Math.max(4, m.left - r.width + 1);
  el.style.left = `${left}px`;
  el.style.top = `${Math.max(4, Math.min(b.top - 5, innerHeight - r.height - 4))}px`; // its first item level with `btn`
}

$('#ctx').addEventListener('mouseover', (e) => {
  if (e.target.closest('.ctx-sub')) { clearTimeout(ctxSub.closeTimer); return; }
  const b = e.target.closest('button[data-ctx]');
  if (!b) return;
  const i = Number(b.dataset.ctx);
  clearTimeout(ctxSub.openTimer);
  if (ctxNow[i]?.sub && !b.disabled) {
    clearTimeout(ctxSub.closeTimer);
    if (ctxSub.i !== i) ctxSub.openTimer = setTimeout(() => openSub(i), SUB_OPEN_MS);
  } else if (ctxSub.i >= 0) {
    clearTimeout(ctxSub.closeTimer);
    ctxSub.closeTimer = setTimeout(closeSub, SUB_GRACE_MS);
  }
});

// Open With (asked by Renaud on 2026-11-14, look 2 of the mockup): the apps that macOS offers
// (finder.apps_for), the default app first with "default" in gray on the right, the others in
// Finder's order, then Other... (an app picked in macOS's open panel, not remembered).
// `open(app)` opens the entry with the app at that path.
async function openWithItems(cmd, args, open) {
  const apps = await request(cmd, args);
  const row = (a) => ({ label: a.name, key: a.default ? 'default' : '', run: () => open(a.path) });
  const first = apps.filter((a) => a.default).map(row);
  const rest = apps.filter((a) => !a.default).map(row);
  const other = { label: 'Other...', run: async () => {
    let app = null;
    try { app = await request('apps.pick'); } catch (e) { toast(errText(e)); return; }
    if (app) open(app);
  } };
  return [...first, ...(first.length && rest.length ? ['-'] : []), ...rest, ...(apps.length ? ['-'] : []), other];
}

function menuAction(name, arg) { // the File menu of the menu bar (main.py)
  if (name === 'refresh' && wsId()) refreshTree(wsId());
  if (name === 'project') addProject(arg); // app.js
  if (name === 'open') openFromPanel(); // files.js
}

// ------------------------------------------------------------------ actions

function startRename(tid, path) {
  const row = treeRow(path);
  if (!row || tid !== wsId() || S.treeRename) return;
  const dir = row.dataset.dir === '1';
  const name = nameOf(path);
  const input = document.createElement('input');
  input.className = 'tree-rename';
  input.value = name;
  input.spellcheck = false;
  row.querySelector('.name').replaceWith(input);
  S.treeRename = { tid, path };
  input.focus();
  const dot = name.lastIndexOf('.');
  input.setSelectionRange(0, !dir && dot > 0 ? dot : name.length); // the name without its extension, as VS Code
  let over = false;
  const finish = async (save, refocus) => {
    if (over) return;
    over = true;
    S.treeRename = null;
    const to = input.value.trim();
    if (save && to && to !== name) await renamePath(tid, path, to);
    renderExplorer();
    if (refocus) $('#tree').focus();
  };
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true, true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false, true); }
  });
  input.addEventListener('blur', () => finish(true, false)); // a click elsewhere keeps the name, as VS Code
}

// New File... / New Folder...: a name field in a new row where the entry will show, as in VS Code:
// first in the folder for a folder, after its folders for a file. `dir` '' is the project folder.
// Enter creates it (a file opens with the keyboard, a folder is selected), a click elsewhere
// creates it and leaves the rest as it is, Esc or an empty name: nothing.
async function startCreate(tid, dir, isDir) {
  if (tid !== wsId() || S.treeRename) return;
  const w = work(tid);
  if (dir) w.open.add(dir);
  if (!w.kids.has(dir)) await loadDir(tid, dir);
  if (tid !== wsId() || S.treeRename) return;
  renderExplorer();
  const tree = $('#tree');
  const rows = [...tree.querySelectorAll('.node[data-node]')];
  const dirRow = dir ? treeRow(dir) : null;
  if (dir && !dirRow) return;
  let anchor; // the new row goes before it (null: at the end)
  if (isDir) anchor = dirRow ? dirRow.nextSibling : tree.firstChild;
  else {
    const file = (w.kids.get(dir) || []).find((e) => !e.dir);
    const after = rows.slice(dirRow ? rows.indexOf(dirRow) + 1 : 0);
    anchor = file ? treeRow(file.path) : after.find((r) => dir && !r.dataset.node.startsWith(`${dir}/`)) || null;
  }
  const row = document.createElement('div');
  row.className = `node ${isDir ? 'dir' : 'file'}`;
  row.style.paddingLeft = `${8 + (dir ? dir.split('/').length : 0) * 14}px`;
  row.innerHTML = `<span class="caret">${isDir ? '▸' : ''}</span>`;
  const input = document.createElement('input');
  input.className = 'tree-rename';
  input.spellcheck = false;
  row.appendChild(input);
  tree.insertBefore(row, anchor);
  S.treeRename = { tid, dir };
  input.focus();
  row.scrollIntoView({ block: 'nearest' });
  let over = false;
  const finish = async (save, enter) => {
    if (over) return;
    over = true;
    S.treeRename = null;
    const name = input.value.trim();
    let path = null;
    if (save && name) {
      try { path = await request('fs.new', { id: tid, dir, name, folder: isDir }); } catch (e) { toast(errText(e)); }
    }
    if (path === null) {
      renderExplorer();
      if (enter) $('#tree').focus();
      return;
    }
    for (let p = parentOf(path); p && p !== dir; p = parentOf(p)) w.open.add(p); // made by a name with /
    if (enter) w.treeSel = path;
    treesChanged(wsProject(tid));
    if (enter && !isDir) openFile(tid, path, true);
    else if (enter) $('#tree').focus();
  };
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true, true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false, true); }
  });
  input.addEventListener('blur', () => finish(true, false));
}

async function renamePath(tid, path, name) {
  let to;
  try { to = await request('fs.rename', { id: tid, path, name }); } catch (e) { toast(errText(e)); return; }
  const project = wsProject(tid);
  movedPaths(project, [[path, to]]);
  treesChanged(project);
}

async function deletePath(tid, path, dir) {
  if (!confirm(`Move ${nameOf(path)}${dir ? '/ and everything in it' : ''} to the Trash?`)) return;
  try { await request('fs.trash', { id: tid, path }); } catch (e) { toast(errText(e)); return; }
  const project = wsProject(tid);
  gonePaths(project, [path]);
  treesChanged(project);
}

function setClip(tid, path, cut) {
  S.fileClip = { tid, project: wsProject(tid), paths: [path], cut };
  renderExplorer();
  renderFinders(); // a cut entry is dimmed there too (finder.js)
}

async function pasteInto(tid, dir) {
  const c = S.fileClip;
  if (!c) return;
  if (c.abs && !c.project) { // cut or copied in a Finder tab, outside any project (finder.js)
    const root = wsProject(tid);
    try { await request('finder.paste', { paths: c.abs, dir: dir ? `${root}/${dir}` : root, cut: c.cut }); } catch (e) { toast(errText(e)); return; }
    if (c.cut) S.fileClip = null;
    if (dir) work(tid).open.add(dir);
    treesChanged(root);
    refreshFinders();
    return;
  }
  let pairs;
  try { pairs = await request('fs.paste', { id: tid, from: c.tid, paths: c.paths, dir, cut: c.cut }); } catch (e) { toast(errText(e)); return; }
  const project = wsProject(tid);
  if (c.cut) {
    S.fileClip = null; // a cut is pasted once, as in Finder and VS Code
    if (c.project === project) movedPaths(project, pairs);
    else { gonePaths(c.project, pairs.map(([from]) => from)); treesChanged(c.project); }
  }
  const w = work(tid);
  if (dir) w.open.add(dir);
  if (pairs.length) w.treeSel = pairs[pairs.length - 1][1];
  treesChanged(project);
}

// In the folder, or in the file's folder; pane 3 (bottom left), which opens if it was empty.
function terminalHere(tid, path) {
  uiLog('create: terminal in pane 3, from the explorer');
  newShell(ownerFor(tid), 3, path); // as Cmd+N T: no task selected, a project tab
}

async function copyPath(tid, path, relative) {
  try { toast(`Copied: ${await request('fs.copypath', { id: tid, path, relative })}`); } catch (e) { toast(errText(e)); }
}

// Files and folders moved or renamed inside `project`: the tabs that show them follow, in every
// task of the project (tasks of one project share its files), and so do the open folders of 2a.
function movedPaths(project, pairs) {
  const map = (p) => {
    for (const [a, b] of pairs) {
      if (p === a) return b;
      if (p.startsWith(`${a}/`)) return b + p.slice(a.length);
    }
    return null;
  };
  sameProject(project, (id, w) => {
    let moved = false;
    for (const n of PANES) { // the tab on screen in each pane (grid.js), also a project tab that this view shows
      const a = w.act[n];
      if (a && a.startsWith(MD_PREFIX)) { // a markdown preview (mdpreview.js)
        const to = map(a.slice(MD_PREFIX.length));
        if (to !== null) w.act[n] = MD_PREFIX + to;
        continue;
      }
      const to = a && !/^(shell|web|finder|thread|untitled):/.test(a) ? map(a) : null;
      if (to !== null) w.act[n] = to;
    }
    for (const tab of w.tabs) {
      if (tab.preview) {
        const to = map(tab.src);
        if (to !== null) { tab.src = to; tab.path = MD_PREFIX + to; moved = true; }
        continue;
      }
      const to = isFileTab(tab) ? map(tab.path) : null;
      if (to === null) continue;
      if (tab.langC) tab.view.dispatch({ effects: tab.langC.reconfigure(CM.languageFor(to)) });
      if (tab.wrap) { // remembered under the new path (files.js, setWrap)
        store(wrapKey(id, tab.path), null); // null: forgotten
        store(wrapKey(id, to), true);
      }
      tab.path = to;
      moved = true;
    }
    w.open = new Set([...w.open].map((p) => map(p) ?? p));
    if (w.treeSel) w.treeSel = map(w.treeSel) ?? w.treeSel;
    if (moved) { saveOpenFiles(id); saveGrid(id); }
  });
  renderFiles();
}

// Files and folders gone from `project` (Trash, or moved to another project): their tabs close,
// except those with unsaved edits, which stay as "Deleted on disk" (Save writes the file again).
function gonePaths(project, paths) {
  const under = (p) => paths.some((a) => p === a || p.startsWith(`${a}/`));
  sameProject(project, (id, w) => {
    for (const tab of [...w.tabs]) {
      if (tab.preview && under(tab.src)) { closeFile(id, tab.path); continue; } // its markdown preview (mdpreview.js)
      if (!isFileTab(tab) || !under(tab.path)) continue;
      if (tab.dirty) tab.gone = true;
      else closeFile(id, tab.path);
    }
    w.open = new Set([...w.open].filter((p) => !under(p)));
    if (w.treeSel && under(w.treeSel)) w.treeSel = null;
  });
  renderFiles();
}

function treesChanged(project) { // the selected task's tree reloads now, the others when they are selected
  sameProject(project, (id, w) => { if (id === wsId()) refreshTree(id); else w.kids.clear(); });
  renderExplorer();
}

// ------------------------------------------------------------------ drag a row into a folder
// As in VS Code (asked by Renaud on 2026-11-14): a file or folder dragged onto a folder moves
// there. The folder is the one of a drop from Finder (treeInto), with the same mark and a label
// next to the mouse. Mouse events, as a tab's drag (grid.js): the drag starts 6 px from the press.
// The move is Cut and Paste's (files.paste with `cut`): nothing is replaced, a folder cannot go
// into itself. Its own folder, itself or a folder inside it: no mark, and a drop there does
// nothing; so does a drop outside the tree, or Esc. A closed folder under the mouse opens after
// TD_OPEN_MS, and near the top or bottom edge the tree scrolls.

const TD = { press: null, drag: null, justDragged: false };
const TD_OPEN_MS = 500;
const TD_EDGE = 24; // px from the tree's top or bottom edge
const TD_STEP = 8; // px scrolled every TD_TICK_MS
const TD_TICK_MS = 40;

function treeDragStart(p) {
  const ghost = document.createElement('div');
  ghost.className = 'tab-ghost';
  const mark = document.createElement('div');
  mark.className = 'drop-mark'; // the mark of a dragged tab (grid.js) and of a drop from Finder (drop.js)
  mark.hidden = true;
  document.body.append(ghost, mark);
  document.body.classList.add('tab-dragging');
  getSelection().removeAllRanges(); // text that the press began to select
  // the tick: the tree scrolls near its edges, and the mark follows a tree that was redrawn under a still mouse
  TD.drag = { ...p, ghost, mark, into: null, hover: null, hoverTimer: 0, tick: setInterval(treeDragTick, TD_TICK_MS) };
}

function treeDragEnd(into) { // into: the folder to move to, or null (cancelled)
  const d = TD.drag;
  clearTimeout(d.hoverTimer);
  clearInterval(d.tick);
  d.ghost.remove();
  d.mark.remove();
  TD.drag = null;
  document.body.classList.remove('tab-dragging');
  TD.justDragged = true; // the click that follows the mouseup is not a click on a row
  setTimeout(() => { TD.justDragged = false; }, 0);
  if (into !== null && d.tid === wsId()) moveEntry(d.tid, d.path, into);
}

// The target under the mouse: the mark, the label, and the timer that opens a closed folder.
function treeDragShow() {
  const d = TD.drag;
  const el = document.elementFromPoint(d.x, d.y);
  const node = el && el.closest('#tree .node[data-node]');
  const into = el && el.closest('#tree-wrap') && d.tid === wsId() && !S.treeRename ? treeInto(node) : null;
  const ok = into !== null && into !== parentOf(d.path) && into !== d.path && !into.startsWith(`${d.path}/`);
  d.into = ok ? into : null;
  d.mark.hidden = !ok;
  if (ok) {
    const r = ((into && treeRow(into)) || $('#tree-wrap')).getBoundingClientRect(); // a row off screen: the whole tree, as drop.js
    Object.assign(d.mark.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  }
  d.ghost.textContent = ok ? `Move ${nameOf(d.path)} into ${nameOf(into || wsProject(d.tid))}/` : nameOf(d.path);
  const left = Math.max(4, Math.min(d.x + 12, innerWidth - d.ghost.offsetWidth - 4));
  d.ghost.style.transform = `translate(${left}px, ${d.y + 8}px)`;
  // the results of the explorer search open their folders themselves (treesearch.js)
  const closed = ok && node && node.dataset.dir === '1' && !node.classList.contains('tree-search-res')
    && !work(d.tid).open.has(into) ? into : null;
  if (closed === d.hover) return;
  clearTimeout(d.hoverTimer);
  d.hover = closed;
  if (closed !== null) {
    d.hoverTimer = setTimeout(() => {
      if (TD.drag !== d || d.hover !== closed || d.tid !== wsId()) return;
      work(d.tid).open.add(closed);
      renderExplorer(); // it loads the folder's entries; the next tick marks the new rows
    }, TD_OPEN_MS);
  }
}

function treeDragTick() {
  const d = TD.drag;
  const tree = $('#tree');
  const r = tree.getBoundingClientRect();
  if (d.x >= r.left && d.x <= r.right) {
    if (d.y >= r.top - TD_EDGE && d.y < r.top + TD_EDGE) tree.scrollTop -= TD_STEP;
    else if (d.y > r.bottom - TD_EDGE && d.y <= r.bottom + TD_EDGE) tree.scrollTop += TD_STEP;
  }
  treeDragShow();
}

async function moveEntry(tid, path, into) {
  let pairs;
  try { pairs = await request('fs.paste', { id: tid, from: tid, paths: [path], dir: into, cut: true }); } catch (e) { toast(errText(e)); return; }
  const project = wsProject(tid);
  movedPaths(project, pairs); // its tabs follow
  const w = work(tid);
  if (into) w.open.add(into);
  if (pairs.length) w.treeSel = pairs[0][1]; // selected, as after Paste
  treesChanged(project);
}

document.addEventListener('mousedown', (e) => {
  if (TD.drag) treeDragEnd(null); // its mouseup was lost
  TD.press = null;
  if (e.button !== 0 || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return; // Ctrl+click: the right-click menu
  const node = e.target.closest('#tree .node[data-node]');
  const tid = wsId();
  if (!node || !tid || S.mode !== 'dev' || S.treeRename) return; // the name field: its text is selected with the mouse
  TD.press = { tid, path: node.dataset.node, x: e.clientX, y: e.clientY };
}, true);

document.addEventListener('mousemove', (e) => {
  const p = TD.press;
  if (!p) return;
  if (!(e.buttons & 1)) { // the button came up where no mouseup reached the page
    TD.press = null;
    if (TD.drag) treeDragEnd(null);
    return;
  }
  if (!TD.drag) {
    if (Math.hypot(e.clientX - p.x, e.clientY - p.y) < 6) return;
    treeDragStart(p);
  }
  e.preventDefault();
  TD.drag.x = e.clientX;
  TD.drag.y = e.clientY;
  treeDragShow();
});

document.addEventListener('mouseup', () => {
  TD.press = null;
  if (TD.drag) treeDragEnd(TD.drag.into);
});

document.addEventListener('click', (e) => {
  if (TD.justDragged) { e.stopPropagation(); e.preventDefault(); }
}, true);

document.addEventListener('keydown', (e) => {
  if (!TD.drag || e.key !== 'Escape') return;
  e.preventDefault(); e.stopPropagation();
  TD.press = null;
  treeDragEnd(null);
}, true);

window.addEventListener('blur', () => {
  TD.press = null;
  if (TD.drag) treeDragEnd(null);
});

// ------------------------------------------------------------------ events

$('#tree').addEventListener('contextmenu', (e) => {
  e.preventDefault(); // WebKit's own menu
  if (!wsId() || S.treeRename) return;
  const tid = wsId();
  const node = e.target.closest('.node[data-node]');
  if (node) { work(tid).treeSel = node.dataset.node; renderExplorer(); }
  $('#tree').focus();
  // in the results of the explorer search, without the items that make an entry (treesearch.js)
  openCtx(e.clientX, e.clientY, treeSearchMenu(ctxItems(tid, node ? node.dataset.node : null, Boolean(node) && node.dataset.dir === '1')));
});

$('#ctx').addEventListener('mousedown', (e) => e.preventDefault()); // the tree keeps the keyboard
$('#ctx').addEventListener('click', (e) => {
  const s = e.target.closest('button[data-sub]');
  if (s) {
    if (s.disabled) return;
    const it = ctxSub.items[Number(s.dataset.sub)];
    closeCtx();
    it.run();
    return;
  }
  const b = e.target.closest('button[data-ctx]');
  if (!b || b.disabled) return;
  const it = ctxNow[Number(b.dataset.ctx)];
  if (it.sub) { openSub(Number(b.dataset.ctx)); return; } // a click opens its submenu at once
  closeCtx();
  it.run();
});
document.addEventListener('mousedown', (e) => { if (!$('#ctx').hidden && !e.target.closest('#ctx')) closeCtx(); }, true);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || $('#ctx').hidden) return;
  e.preventDefault(); e.stopPropagation();
  closeCtx();
}, true);
window.addEventListener('blur', closeCtx);
window.addEventListener('resize', closeCtx);

$('#tree-collapse').addEventListener('click', () => { // as VS Code's "Collapse Folders in Explorer"
  if (!wsId()) return;
  work(wsId()).open.clear();
  renderExplorer();
  $('#tree').focus();
});

$('#tree').addEventListener('keydown', (e) => {
  const tid = wsId();
  if (e.target !== $('#tree') || !tid || S.mode !== 'dev') return;
  const path = work(tid).treeSel;
  const row = path && treeRow(path);
  if (!row) return;
  const plain = !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
  if ((e.key === 'Enter' || e.key === 'F2') && plain) startRename(tid, path);
  else if (e.key === 'Backspace' && e.metaKey) deletePath(tid, path, row.dataset.dir === '1');
  else if (e.key.toLowerCase() === 'c' && e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) copyPath(tid, path, true);
  else return;
  e.preventDefault(); e.stopPropagation();
});

// ------------------------------------------------------------------ Cmd+J E: a key on each row
// Cmd+J, then E (grid.js, asked by Renaud on 2026-11-11): each row of 2a on screen shows a label
// right after its name: 1 to 9, then a to z, from the top (35 rows at most, one key each).
// Typing a label acts as a click on its row (a file opens and the keyboard stays in the tree, a
// folder opens or closes), and the keyboard goes to the tree. The labels stay until a key, a
// click, or the window loses the keyboard; Esc or another key: nothing. They are counted again
// when the tree is redrawn or scrolled: they are always what is on screen.

const HINT_KEYS = '123456789abcdefghijklmnopqrstuvwxyz'; // Renaud: "1-9 then a-z, not 10, 11"
let hintRows = []; // the labelled rows, in order: {path, dir}

function startTreeHints() {
  const c = G.chord;
  clearTimeout(c.timer);
  c.timer = null;
  const tid = wsId();
  if (!tid || !wsProject(tid)) { endChord('jump: explorer (no project)'); return; }
  revealColumn('explorer'); // hidden by Cmd+Option+2 (layout.js): the hints need its rows on screen
  c.kind = 'tree';
  drawTreeHints();
  if (!hintRows.length) endChord('jump: explorer (no row on screen)');
}

function drawTreeHints() {
  const tree = $('#tree');
  for (const h of tree.querySelectorAll('.tree-hint')) h.remove();
  const c = G.chord;
  if (!c || c.kind !== 'tree') return;
  const box = tree.getBoundingClientRect();
  const rows = [...tree.querySelectorAll('.node[data-node]')].filter((r) => {
    const b = r.getBoundingClientRect();
    const mid = (b.top + b.bottom) / 2;
    return mid > box.top && mid < box.bottom;
  }).slice(0, HINT_KEYS.length);
  hintRows = rows.map((r) => ({ path: r.dataset.node, dir: r.dataset.dir === '1' }));
  rows.forEach((r, i) => {
    const s = document.createElement('span');
    s.className = 'tree-hint';
    s.textContent = HINT_KEYS[i];
    r.appendChild(s); // after the name (or the rename field)
  });
}

function clearTreeHints() {
  for (const h of $('#tree').querySelectorAll('.tree-hint')) h.remove();
  hintRows = [];
}

function treeHintKey(k) {
  const i = k.length === 1 ? HINT_KEYS.indexOf(k) : -1;
  if (i < 0 || i >= hintRows.length) {
    endChord(i < 0 ? `jump: explorer, cancelled by the key ${k}` : `jump: explorer, no row ${k}`);
    return;
  }
  pickTreeHint(i);
}

function pickTreeHint(i) {
  const row = hintRows[i];
  endChord(`jump: explorer, row ${HINT_KEYS[i]} of ${hintRows.length}`);
  if (!row || !wsId()) return;
  clickTreeNode(row.path, row.dir, false); // files.js
  $('#tree').focus({ preventScroll: true });
}

$('#tree').addEventListener('scroll', () => { if (G.chord && G.chord.kind === 'tree') drawTreeHints(); });
// a click or the window losing the keyboard ends the labels: grid.js (LABELS)
