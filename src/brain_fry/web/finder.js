'use strict';
// Finder tabs (asked by Renaud on 2026-11-11, TODO.md: "I'm using multiple instances of Finder
// windows to find different files ... the exact same look and feel as the macOS Finder in Column
// View"). A tab of the panes of column 3, like a file or a terminal: Cmd+N E, then 1-4 (grid.js).
// It starts at the project folder and can go anywhere on the disk (Cmd+Up, the path bar). Paths
// are absolute; the backend reads them with Finder's own calls (finder.py). Look chosen from a
// mockup: Finder's dark mode, the path bar and the status bar (his Finder shows both), a folder
// icon in the tab, columns of 170 px, hidden files dimmed (his Finder shows them).
// - Click: selects (a folder shows its content in the next column, a file its preview). A
//   double-click, or Cmd+Down: a folder becomes the first column; a file of the project opens in a
//   tab of column 3 (not in the Finder's own pane), any other file or an app in its default app.
// - Keys, as Finder: arrows, type a name to select it, Enter renames, Cmd+Backspace moves to the
//   Trash, Cmd+Up adds the enclosing folder on the left. Ctrl+C copies the relative path, as in 2a.
// - Right-click: the menu of 2a (explorer.js), on absolute paths. Cut / Copy / Paste work between
//   2a and the Finder tabs (S.fileClip).
// - Double-click on the path bar: that folder becomes the first column, the selection stays.
// - The grip at the bottom of a divider resizes its column; with Option, every column.
// - The folders on screen are read again every 2 s (the agent adds files).
// A tab: { kind: 'finder', finder: true, root (the first column's folder), chain (the name selected
// in each column, from the root; the last one may be a file) }. Saved in the task file
// (`finder_tabs`: root and the selected path), restored with the task, copied to a new task and a fork.
// Uses S, $, esc, request, send, toast, uiLog (app.js), work, openFile
// (files.js), addTab, showTab, openPane (grid.js), openCtx, errText,
// movedPaths, gonePaths, treesChanged (explorer.js), newShell (shells.js).

const FINDER_POLL_MS = 2000;
const FINDER_COL_W = 170; // Renaud, from the mockup (2026-11-11)
const FINDER_COL_MIN = 100;
const TYPE_RESET_MS = 1000; // typing a name: the letters within 1 s add up, as in Finder
const FD_NEIGHBOUR = { 1: 2, 2: 1, 3: 4, 4: 3 };
const FD_CHEVRON = '<svg class="fd-chev" viewBox="0 0 7 10" width="7" height="10"><path d="M1.2 1l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const F = { seq: 0, lists: new Map(), loading: new Map(), infos: new Map(), saveTimers: new Map(), renaming: null, grip: null };

const fJoin = (dir, name) => (dir === '/' ? `/${name}` : `${dir}/${name}`);
const fParent = (p) => (p.lastIndexOf('/') <= 0 ? '/' : p.slice(0, p.lastIndexOf('/')));
const fBase = (p) => p.slice(p.lastIndexOf('/') + 1);
const fdIcon = (key, px = 32) => `/icon/${encodeURIComponent(key)}.png?s=${px}`;

// The path relative to the project of the task, or null outside it.
function fdRel(tid, path) {
  const project = wsProject(tid);
  return project && path.startsWith(`${project}/`) ? path.slice(project.length + 1) : null;
}

// ------------------------------------------------------------------ the tab

function makeFinderTab(tid, root, chain = []) {
  const el = document.createElement('div');
  el.className = 'editor finder';
  el.tabIndex = -1;
  el.innerHTML = '<div class="fd-cols"></div><div class="fd-pathbar"></div><div class="fd-status"></div>';
  const tab = { tid, path: `finder:${++F.seq}`, el, kind: 'finder', finder: true, root, chain: [...chain], widths: [],
    colW: FINDER_COL_W, typed: '', typedAt: 0, dirty: false, diskChanged: false, gone: false };
  tab.focus = () => el.focus({ preventScroll: true });
  el.addEventListener('keydown', (e) => finderKey(tab, e));
  el.addEventListener('mousedown', (e) => finderMouse(tab, e));
  el.addEventListener('contextmenu', (e) => finderMenu(tab, e));
  return tab;
}

// Cmd+N E (grid.js): a Finder tab at the project folder, in pane n.
function newFinder(tid, n) {
  const root = wsProject(tid);
  if (!root) { toast('No project: a Finder tab starts at the project folder.'); return; }
  const w = work(tid); // its owner: a task, or a project's tabs (ownerFor)
  const tab = makeFinderTab(tid, root);
  addTab(w, tab, n);
  showTab(tab, true);
  refreshFinder(tab).then(() => saveFinderTabs(tid));
}

// The tabs of the task file, each in the pane that the window remembers (restoreFiles, files.js).
async function restoreFinderTabs(tid, grid) { // tid: a task, or a project's tabs
  const w = work(tid);
  for (const [i, t] of ((ownerData(tid) || {}).finderTabs || []).entries()) {
    const chain = t.path && t.path.startsWith(`${t.root === '/' ? '' : t.root}/`) ? t.path.slice(t.root.length + (t.root === '/' ? 0 : 1)).split('/') : [];
    const tab = makeFinderTab(tid, t.root, chain);
    addTab(w, tab, grid.at[gridKey(tid, `finder#${i}`)] || 1);
    await refreshFinder(tab);
  }
}

function finderName(tab) {
  const c = fdFocusCol(tab);
  const crumbs = c.list && c.list.crumbs;
  return crumbs ? crumbs[crumbs.length - 1].name : fBase(c.folder) || '/';
}

function finderTitle(tab) { return fdFocusCol(tab).folder; }

function fdSelected(tab) { // the chain holds names from the root: the path needs no listing
  return tab.chain.reduce((p, name) => fJoin(p, name), tab.chain.length ? tab.root : null);
}

function saveFinderTabs(tid) { // a project's tabs: in project.json
  clearTimeout(F.saveTimers.get(tid));
  F.saveTimers.set(tid, setTimeout(() => {
    const tabs = work(tid).tabs.filter((t) => t.finder).map((t) => ({ root: t.root, path: fdSelected(t) }));
    send('finder.tabs.set', { id: tid, tabs });
  }, 500));
}

// ------------------------------------------------------------------ folders

function fdLoad(path, quiet = false) {
  if (F.loading.has(path)) return F.loading.get(path);
  const p = request('finder.list', { path }).then((r) => { F.lists.set(path, r); return r; }, (e) => {
    F.lists.delete(path);
    if (!quiet) toast(errText(e));
    return null;
  }).finally(() => F.loading.delete(path));
  F.loading.set(path, p);
  return p;
}

// The columns on screen: [{folder, list, sel}], from the root. A selected folder adds its column;
// a selected file ends them (its preview comes after).
function fdColumns(tab) {
  const cols = [];
  let folder = tab.root;
  for (let i = 0; ; i++) {
    const list = F.lists.get(folder) || null;
    const sel = i < tab.chain.length ? tab.chain[i] : null;
    cols.push({ folder, list, sel });
    if (sel === null || !list) break;
    const e = list.entries.find((x) => x.name === sel);
    if (!e || !e.dir) break;
    folder = fJoin(folder, sel);
  }
  return cols;
}

// The column of the last selection: the keys act there.
function fdFocusCol(tab) {
  const cols = fdColumns(tab);
  return cols[Math.min(Math.max(0, tab.chain.length - 1), cols.length - 1)];
}

function fdEntry(tab, i) {
  const c = fdColumns(tab)[i];
  return c && c.list && c.sel !== null ? c.list.entries.find((x) => x.name === c.sel) || null : null;
}

// A selection whose entry is gone (renamed, deleted, by the agent or in 2a) ends there.
function fdValidate(tab) {
  const cols = fdColumns(tab);
  for (let i = 0; i < tab.chain.length; i++) {
    const c = cols[i];
    if (!c || !c.list) return; // not loaded yet
    if (!c.list.entries.some((x) => x.name === tab.chain[i])) { tab.chain = tab.chain.slice(0, i); return; }
  }
}

// Loads what the columns need (missing folders; `all`: every folder on screen again), then draws.
async function refreshFinder(tab, all = false) {
  for (let guard = 0; guard < 64; guard++) {
    const missing = fdColumns(tab).map((c) => c.folder).filter((f) => all || !F.lists.has(f));
    all = false;
    if (!missing.length) break;
    const got = await Promise.all(missing.map((f) => fdLoad(f, true)));
    if (got.some((r) => r === null)) { // a folder that is gone: the selection ends before it
      const bad = missing[got.indexOf(null)];
      if (bad === tab.root) { tab.root = fParent(tab.root); tab.chain = []; } else {
        const i = fdColumns(tab).findIndex((c) => c.folder === bad);
        tab.chain = tab.chain.slice(0, Math.max(0, i - 1));
      }
    }
    fdValidate(tab);
  }
  fdValidate(tab);
  renderFinder(tab);
}

function renderFinders() { // a cut in 2a: the entry is dimmed in the Finder tabs too
  for (const w of Object.values(S.work || {})) for (const t of w.tabs) if (t.finder) renderFinder(t);
}

// After a change made here (rename, paste, Trash): every Finder tab on screen reads its folders again.
function refreshFinders() {
  for (const w of Object.values(S.work || {})) for (const t of w.tabs) if (t.finder) refreshFinder(t, true);
}

// The selection fdChanged (click, keys): draw, show the end of the columns, remember.
function fdChanged(tab, reveal = true) {
  tab.typed = '';
  refreshFinder(tab).then(() => {
    if (reveal) fdReveal(tab);
    saveFinderTabs(tab.tid);
    if (inView(tab.tid)) renderFileTabs(); // the tab's name: the folder of the last selection
  });
  renderFinder(tab);
}

// ------------------------------------------------------------------ drawing

function renderFinder(tab) {
  if (F.renaming && F.renaming.tab === tab) return; // a redraw would take the name field away
  const box = tab.el.querySelector('.fd-cols');
  const scroll = {};
  for (const r of box.querySelectorAll('.fd-rows')) scroll[r.dataset.folder] = r.scrollTop;
  const left = box.scrollLeft;
  const atEnd = left > 0 && left >= box.scrollWidth - box.clientWidth - 2; // the end in view: it stays (a preview that grows)
  const cols = fdColumns(tab);
  const ready = tab.chain.length <= cols.length; // every selected folder is loaded (not yet after Cmd+Up)
  const fc = Math.min(Math.max(0, tab.chain.length - 1), cols.length - 1);
  const clip = fdClipPaths();
  const html = cols.map((c, i) => {
    const w = tab.widths[i] || tab.colW;
    const rows = c.list ? c.list.entries.map((e) => {
      const p = fJoin(c.folder, e.name);
      const cls = ['fd-row', e.hidden ? 'hid' : '', e.name === c.sel ? 'sel' : '', e.name === c.sel && i === fc && ready ? 'cur' : '',
        clip.cut && clip.paths.includes(p) ? 'cut' : ''].join(' ');
      return `<div class="${cls}" data-name="${esc(e.name)}"><img src="${fdIcon(e.icon)}" alt="" draggable="false">`
        + `<span class="n">${esc(e.name)}</span>${e.dir ? FD_CHEVRON : ''}</div>`;
    }).join('') : '';
    return `<div class="fd-col" data-i="${i}" style="width:${w}px"><div class="fd-rows" data-folder="${esc(c.folder)}">${rows}</div>`
      + `<div class="fd-grip" data-i="${i}"><i></i><i></i></div></div>`;
  }).join('');
  const last = ready && tab.chain.length ? fdEntry(tab, tab.chain.length - 1) : null;
  const file = last && !last.dir ? fdSelected(tab) : null;
  box.innerHTML = html + (file ? fdPreview(tab, file, last) : '');
  for (const r of box.querySelectorAll('.fd-rows')) if (scroll[r.dataset.folder] !== undefined) r.scrollTop = scroll[r.dataset.folder];
  box.scrollLeft = atEnd ? box.scrollWidth : left;
  fdBars(tab, cols[fc], last ? fdSelected(tab) : null, last);
}

function fdPreview(tab, path, e) {
  let p = F.infos.get(path);
  if (!p) {
    p = { info: null, thumb: undefined };
    F.infos.set(path, p);
    if (F.infos.size > 50) F.infos.delete(F.infos.keys().next().value); // thumbnails weigh up to ~100 KB each
    request('finder.info', { path }).then((info) => { p.info = info; if (F.infos.get(path) === p) fdRerender(path); }, () => {});
    request('finder.thumb', { path }).then((url) => { p.thumb = url; if (F.infos.get(path) === p) fdRerender(path); }, () => { p.thumb = null; });
  }
  const i = p.info;
  const img = p.thumb ? `<img class="fd-thumb" src="${p.thumb}" alt="">` : `<img class="fd-bigicon" src="${fdIcon((i && i.icon) || e.icon, 256)}" alt="">`;
  const kv = (k, v) => (v ? `<div class="fd-kv"><span>${k}</span><span>${esc(v)}</span></div>` : '');
  return `<div class="fd-preview"><div class="fd-big">${img}</div><div class="fd-title">${esc(e.name)}</div>`
    + (i ? `<div class="fd-sub">${esc([i.kind, i.size].filter(Boolean).join(' - '))}</div><h5>Information</h5>`
      + kv('Created', i.created) + kv('Modified', i.modified) + kv('Last opened', i.opened) : '') + '</div>';
}

function fdRerender(path) { // a preview's info or thumbnail arrived
  for (const w of Object.values(S.work || {})) for (const t of w.tabs) if (t.finder && fdSelected(t) === path) renderFinder(t);
}

function fdBars(tab, col, selPath, sel) {
  const crumbs = [...((col.list && col.list.crumbs) || [])];
  if (selPath && sel) crumbs.push({ path: selPath, name: sel.name, icon: sel.icon });
  // Too long for the pane: the first folders shrink first (to their icon, then away), the last
  // one keeps its name, as in Finder. The separator belongs to the segment after it.
  const n = crumbs.length;
  tab.el.querySelector('.fd-pathbar').innerHTML = crumbs.map((c, i) => `<span class="fd-seg${i === n - 1 ? ' last' : ''}" data-path="${esc(c.path)}"`
    + ` title="${esc(c.path)}" style="flex-shrink:${(n - i) ** 2}">${i ? '<span class="fd-sep">›</span>' : ''}`
    + `<img src="${fdIcon(c.icon)}" alt="" draggable="false"><span class="fd-name">${esc(c.name)}</span></span>`).join('');
  const count = col.list ? col.list.entries.length : 0;
  const free = col.list && col.list.free ? `, ${col.list.free} available` : '';
  tab.el.querySelector('.fd-status').textContent = col.list
    ? `${sel ? `1 of ${count} selected` : `${count} item${count === 1 ? '' : 's'}`}${free}` : '';
}

// The end of the columns in view (Finder keeps the last column visible), and the selected row.
function fdReveal(tab) {
  const box = tab.el.querySelector('.fd-cols');
  box.scrollLeft = box.scrollWidth;
  const cur = box.querySelector('.fd-row.cur');
  if (cur) cur.scrollIntoView({ block: 'nearest' });
}

// ------------------------------------------------------------------ actions

function fdSelect(tab, i, name) {
  tab.chain = [...tab.chain.slice(0, i), name];
  F.infos.delete(fdSelected(tab)); // its preview is read again: the agent may have changed the file
  fdChanged(tab);
}

// Double-click or Cmd+Down. A folder becomes the first column; a file of the project opens in a tab
// of column 3, any other file, or an app, in its default app (as Finder does).
function fdOpen(tab, i) {
  const c = fdColumns(tab)[i];
  const e = fdEntry(tab, i);
  if (!c || !e) return;
  const path = fJoin(c.folder, e.name);
  if (e.dir) { tab.root = path; tab.chain = []; fdChanged(tab); return; }
  const rel = fdRel(tab.tid, path);
  if (rel === null || e.pkg) { send('finder.open', { path }); return; }
  const view = wsId(); // the file opens in the view, as from 2a (no task selected: a project tab)
  const n = openPane(work(view)); // never over a terminal nor a Finder tab (grid.js)
  openFile(view, rel, true, n === tab.pane ? FD_NEIGHBOUR[tab.pane] : n, true, true);
}

// Cmd+Up: the enclosing folder becomes the first column; the selection stays.
function fdUp(tab) {
  if (tab.root === '/') return;
  tab.chain = [fBase(tab.root), ...tab.chain];
  tab.root = fParent(tab.root);
  fdChanged(tab);
}

// A double-click on the path bar: that folder becomes the first column; the selection stays when
// it is inside.
function fdGoTo(tab, folder) {
  const sel = fdSelected(tab) || tab.root;
  const pre = folder === '/' ? '/' : `${folder}/`;
  tab.chain = sel.startsWith(pre) && sel !== folder ? sel.slice(pre.length).split('/') : [];
  tab.root = folder;
  fdChanged(tab);
}

function fdMove(tab, delta) {
  const fc = Math.max(0, tab.chain.length - 1);
  const c = fdColumns(tab)[fc];
  const list = c && c.list ? c.list.entries : [];
  if (!list.length) return;
  const at = tab.chain.length ? list.findIndex((x) => x.name === tab.chain[fc]) : -1;
  const to = at < 0 ? (delta > 0 ? 0 : list.length - 1) : Math.min(list.length - 1, Math.max(0, at + delta));
  fdSelect(tab, fc, list[to].name);
}

async function fdRight(tab) {
  const i = tab.chain.length - 1;
  const e = i >= 0 && fdEntry(tab, i);
  if (!e || !e.dir) return;
  const folder = fJoin(fdColumns(tab)[i].folder, e.name);
  const list = F.lists.get(folder) || await fdLoad(folder);
  if (list && list.entries.length) fdSelect(tab, i + 1, list.entries[0].name);
}

function fdLeft(tab) {
  if (tab.chain.length > 1) { tab.chain = tab.chain.slice(0, -1); fdChanged(tab); }
}

// Typing a name selects the first entry that starts with it, else the next one in Finder's order.
function fdType(tab, ch) {
  const now = Date.now();
  tab.typed = (now - tab.typedAt < TYPE_RESET_MS ? tab.typed : '') + ch.toLowerCase();
  tab.typedAt = now;
  const fc = Math.max(0, tab.chain.length - 1);
  const c = fdColumns(tab)[fc];
  const list = c && c.list ? c.list.entries : [];
  if (!list.length) return;
  const typed = tab.typed;
  let e = list.find((x) => x.name.toLowerCase().startsWith(typed));
  if (!e) e = list.find((x) => x.name.localeCompare(typed, undefined, { sensitivity: 'base', numeric: true }) > 0) || list[list.length - 1];
  tab.chain = [...tab.chain.slice(0, fc), e.name];
  refreshFinder(tab).then(() => { fdReveal(tab); saveFinderTabs(tab.tid); renderFileTabs(); });
}

function startFinderRename(tab) {
  const i = tab.chain.length - 1;
  const e = fdEntry(tab, i);
  const row = e && tab.el.querySelector('.fd-row.cur');
  if (!row || F.renaming) return;
  const folder = fdColumns(tab)[i].folder;
  const input = document.createElement('input');
  input.className = 'fd-rename';
  input.value = e.name;
  input.spellcheck = false;
  row.querySelector('.n').replaceWith(input);
  F.renaming = { tab };
  input.focus();
  const dot = e.name.lastIndexOf('.');
  input.setSelectionRange(0, !e.dir && dot > 0 ? dot : e.name.length); // the name without its extension, as Finder
  let over = false;
  const finish = async (save, refocus) => {
    if (over) return;
    over = true;
    F.renaming = null;
    const to = input.value.trim();
    if (save && to && to !== e.name) await fdRenameEntry(tab, folder, e.name, to);
    renderFinder(tab);
    if (refocus) tab.focus();
  };
  input.addEventListener('keydown', (ev) => {
    ev.stopPropagation();
    if (ev.key === 'Enter') { ev.preventDefault(); finish(true, true); }
    if (ev.key === 'Escape') { ev.preventDefault(); finish(false, true); }
  });
  input.addEventListener('blur', () => finish(true, false)); // a click elsewhere keeps the name, as Finder
}

async function fdRenameEntry(tab, folder, name, to) {
  const path = fJoin(folder, name);
  let now;
  try { now = await request('finder.rename', { path, name: to }); } catch (e) { toast(errText(e)); return; }
  const i = fdColumns(tab).findIndex((c) => c.folder === folder);
  if (i >= 0 && tab.chain[i] === name) tab.chain[i] = fBase(now);
  fdProjectChanged(tab.tid, [[path, now]], []);
  refreshFinders();
  saveFinderTabs(tab.tid);
}

async function fdTrash(tab, path, dir) {
  if (!confirm(`Move ${fBase(path)}${dir ? '/ and everything in it' : ''} to the Trash?`)) return;
  try { await request('finder.trash', { path }); } catch (e) { toast(errText(e)); return; }
  fdProjectChanged(tab.tid, [], [path]);
  refreshFinders();
}

// The open file tabs and 2a follow what a Finder tab did in the project (explorer.js).
function fdProjectChanged(tid, moved, gone) {
  const project = wsProject(tid);
  if (!project) return;
  const rel = (p) => fdRel(tid, p);
  const pairs = moved.filter(([a, b]) => rel(a) !== null && rel(b) !== null).map(([a, b]) => [rel(a), rel(b)]);
  const out = [...gone, ...moved.filter(([a, b]) => rel(a) !== null && rel(b) === null).map(([a]) => a)].map(rel).filter((p) => p !== null);
  if (pairs.length) movedPaths(project, pairs);
  if (out.length) gonePaths(project, out);
  treesChanged(project);
}

// Cut / Copy / Paste share S.fileClip with 2a (explorer.js): 2a puts paths of a project, a Finder
// tab absolute paths (`abs`), with the project's paths too when they are inside it, so 2a dims a cut.
function fdClipPaths() {
  const c = S.fileClip;
  if (!c) return { cut: false, paths: [] };
  return { cut: c.cut, paths: c.abs || c.paths.map((p) => `${c.project}/${p}`) };
}

function setFinderClip(tab, path, cut) {
  const rel = fdRel(tab.tid, path);
  S.fileClip = { tid: tab.tid, project: rel === null ? null : wsProject(tab.tid), paths: rel === null ? [] : [rel], abs: [path], cut };
  refreshFinders();
  renderExplorer();
}

async function fdPaste(tab, folder) {
  const c = S.fileClip;
  if (!c) return;
  const from = fdClipPaths().paths;
  let pairs;
  try { pairs = await request('finder.paste', { paths: from, dir: folder, cut: c.cut }); } catch (e) { toast(errText(e)); return; }
  if (c.cut) {
    S.fileClip = null; // a cut is pasted once, as in Finder
    fdProjectChanged(tab.tid, pairs, []);
    if (c.project && c.project !== wsProject(tab.tid)) { gonePaths(c.project, c.paths); treesChanged(c.project); }
  } else fdProjectChanged(tab.tid, [], []);
  if (pairs.length) { // the pasted entry is selected, as in Finder
    const at = fParent(pairs[pairs.length - 1][1]);
    const i = fdColumns(tab).findIndex((x) => x.folder === at);
    if (i >= 0) tab.chain = [...tab.chain.slice(0, i), fBase(pairs[pairs.length - 1][1])];
  }
  refreshFinders();
  saveFinderTabs(tab.tid);
}

async function copyFinderPath(tab, path, relative) {
  try { toast(`Copied: ${await request('finder.copypath', { id: tab.tid, path, relative })}`); } catch (e) { toast(errText(e)); }
}

// As in 2a: a terminal in pane 3, in the folder (or the file's folder).
function finderTerminal(tab, path) {
  uiLog('create: terminal in pane 3, from a Finder tab');
  newShell(ownerFor(wsId()), 3, null, path); // as Cmd+N T
}

// ------------------------------------------------------------------ events

function finderKey(tab, e) {
  if (e.target !== tab.el || S.mode !== 'dev') return; // a rename field keeps its keys
  const plain = !e.metaKey && !e.ctrlKey && !e.altKey;
  const k = e.key;
  const i = tab.chain.length - 1;
  const e0 = i >= 0 ? fdEntry(tab, i) : null;
  if (k === 'ArrowDown' && e.metaKey) { if (i >= 0) fdOpen(tab, i); }
  else if (k === 'ArrowUp' && e.metaKey) fdUp(tab);
  else if (k === 'ArrowDown' && plain) fdMove(tab, 1);
  else if (k === 'ArrowUp' && plain) fdMove(tab, -1);
  else if (k === 'ArrowRight' && plain) fdRight(tab);
  else if (k === 'ArrowLeft' && plain) fdLeft(tab);
  else if (k === 'Enter' && plain && !e.shiftKey) startFinderRename(tab);
  else if (k === 'Backspace' && e.metaKey) { if (e0) fdTrash(tab, fdSelected(tab), e0.dir); }
  else if (k.toLowerCase() === 'c' && e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) { if (e0) copyFinderPath(tab, fdSelected(tab), true); }
  else if (k.length === 1 && plain && k !== ' ') fdType(tab, k);
  else return;
  e.preventDefault();
  e.stopPropagation();
}

function finderMouse(tab, e) {
  if (e.button !== 0) return;
  const grip = e.target.closest('.fd-grip');
  if (grip) { fdGrip(tab, Number(grip.dataset.i), e); return; }
  if (e.target.closest('.fd-rename')) return;
  const seg = e.target.closest('.fd-seg');
  if (seg) { e.preventDefault(); tab.focus(); if (e.detail === 2) fdGoTo(tab, seg.dataset.path); return; }
  e.preventDefault(); // the tab keeps the keyboard, no text selection
  tab.focus();
  const row = e.target.closest('.fd-row');
  const col = e.target.closest('.fd-col');
  if (!row || !col) return;
  const i = Number(col.dataset.i);
  // the second click of a double-click (as the cards: the first click redraws the rows)
  if (e.detail === 2 && tab.chain[i] === row.dataset.name && tab.chain.length === i + 1) { fdOpen(tab, i); return; }
  fdSelect(tab, i, row.dataset.name);
}

function finderMenu(tab, e) {
  e.preventDefault(); // WebKit's own menu
  if (F.renaming) return;
  const col = e.target.closest('.fd-col');
  if (!col) return;
  tab.focus();
  const i = Number(col.dataset.i);
  const c = fdColumns(tab)[i];
  const row = e.target.closest('.fd-row');
  const clip = S.fileClip;
  const refresh = { label: 'Refresh', run: () => refreshFinder(tab, true) };
  const paste = (into) => ({ label: clip ? `Paste ${fBase(fdClipPaths().paths[0] || '')}${clip.cut ? ' (move)' : ''}` : 'Paste',
    disabled: !clip, run: () => fdPaste(tab, into) });
  if (!row) { openCtx(e.clientX, e.clientY, [paste(c.folder), '-', refresh]); return; }
  if (tab.chain[i] !== row.dataset.name) { tab.chain = [...tab.chain.slice(0, i), row.dataset.name]; fdChanged(tab, false); }
  const ent = c.list.entries.find((x) => x.name === row.dataset.name);
  const path = fJoin(c.folder, ent.name);
  openCtx(e.clientX, e.clientY, [
    { label: ent.dir ? 'Open in Finder' : 'Open in default app', run: () => send('finder.open', { path }) },
    { label: 'Open a terminal here', run: () => finderTerminal(tab, path) },
    '-',
    { label: 'Cut', run: () => setFinderClip(tab, path, true) },
    { label: 'Copy', run: () => setFinderClip(tab, path, false) },
    paste(ent.dir ? path : c.folder),
    '-',
    { label: 'Copy path', run: () => copyFinderPath(tab, path, false) },
    { label: 'Copy relative path', key: '⌃C', disabled: fdRel(tab.tid, path) === null, run: () => copyFinderPath(tab, path, true) },
    '-',
    { label: 'Rename', key: 'Enter', run: () => startFinderRename(tab) },
    { label: 'Delete', key: '⌘⌫', run: () => fdTrash(tab, path, ent.dir) },
    '-',
    refresh,
  ]);
}

// The grip at the bottom of a divider: that column's width; with Option, every column's.
function fdGrip(tab, i, e) {
  e.preventDefault();
  const col = tab.el.querySelector(`.fd-col[data-i="${i}"]`);
  F.grip = { tab, i, x: e.clientX, w: col.getBoundingClientRect().width, all: e.altKey };
  document.body.classList.add('resizing-h');
}

document.addEventListener('mousemove', (e) => {
  const g = F.grip;
  if (!g) return;
  const w = Math.max(FINDER_COL_MIN, Math.round(g.w + e.clientX - g.x));
  if (g.all) { g.tab.colW = w; g.tab.widths = []; } else g.tab.widths[g.i] = w;
  for (const col of g.tab.el.querySelectorAll('.fd-col')) {
    const i = Number(col.dataset.i);
    col.style.width = `${g.tab.widths[i] || g.tab.colW}px`;
  }
});

document.addEventListener('mouseup', () => {
  if (!F.grip) return;
  F.grip = null;
  document.body.classList.remove('resizing-h');
});

// Every 2 s, the folders of the Finder tabs on screen are read again: the agent adds files, 2a
// renames them. A tab is drawn again only when one of its folders changed.
setInterval(async () => {
  const w = wsId() && S.mode === 'dev' && document.visibilityState === 'visible' && S.work && S.work[wsId()];
  if (!w || F.renaming) return;
  const shown = viewTabs(w).filter((t) => t.finder && t.el.style.display !== 'none');
  for (const tab of shown) {
    const folders = fdColumns(tab).map((c) => c.folder);
    const before = folders.map((f) => JSON.stringify(F.lists.get(f) || null));
    await Promise.all(folders.map((f) => fdLoad(f, true)));
    if (folders.some((f, i) => JSON.stringify(F.lists.get(f) || null) !== before[i])) {
      const sel = fdSelected(tab);
      await refreshFinder(tab);
      if (fdSelected(tab) !== sel) { saveFinderTabs(tab.tid); renderFileTabs(); }
    }
  }
}, FINDER_POLL_MS);
