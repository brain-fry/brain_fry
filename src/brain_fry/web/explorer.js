'use strict';
// The right-click menu of the explorer (2a) and its keys, as in VS Code on macOS: Enter (or F2)
// renames the selected entry, Cmd+Backspace moves it to the Trash, Ctrl+C copies its relative path. Cut / Copy / Paste move or copy
// files and folders, also between projects. See DESIGN_DETAILS.md "2a, 3a, 3b: files and terminals".
// Uses S, $, esc, request, send, toast, store, uiLog (app.js), work, refreshTree, renderExplorer,
// renderFiles, saveOpenFiles, closeFile (files.js) and newShell (shells.js).

// What Cut or Copy put aside for Paste: { tid, project, paths, cut }. Kept in Brain Fry only,
// not on the clipboard of macOS: Finder cannot paste it.
S.fileClip = null;
S.treeRename = null; // { tid, path } while a name field is open in the tree

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
  if (path === null) return [paste, '-', refresh];
  return [
    { label: dir ? 'Open in Finder' : 'Open in default app', run: () => send('fs.open', { id: tid, path }) },
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

function openCtx(x, y, items) {
  const m = $('#ctx');
  ctxNow = items;
  m.innerHTML = items.map((it, i) => (it === '-' ? '<hr>'
    : `<button data-ctx="${i}" ${it.disabled ? 'disabled' : ''}>${esc(it.label)}${it.key ? `<kbd>${it.key}</kbd>` : ''}</button>`)).join('');
  m.hidden = false; // covers the browser view: browser.js shows its snapshot meanwhile
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.max(4, Math.min(x, innerWidth - r.width - 4))}px`;
  m.style.top = `${Math.max(4, Math.min(y, innerHeight - r.height - 4))}px`;
}

function closeCtx() { $('#ctx').hidden = true; }

function menuAction(name, arg) { // the File menu of the menu bar (main.py)
  if (name === 'refresh' && wsId()) refreshTree(wsId());
  if (name === 'project') addProject(arg); // app.js
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
      const to = w.act[n] && !/^(shell|web|finder|thread|untitled):/.test(w.act[n]) ? map(w.act[n]) : null;
      if (to !== null) w.act[n] = to;
    }
    for (const tab of w.tabs) {
      const to = isFileTab(tab) ? map(tab.path) : null;
      if (to === null) continue;
      if (tab.langC) tab.view.dispatch({ effects: tab.langC.reconfigure(CM.languageFor(to)) });
      if (tab.wrap) { // remembered under the new path (files.js, setWrap)
        try { localStorage.removeItem(wrapKey(id, tab.path)); } catch { /* not remembered */ }
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

// ------------------------------------------------------------------ events

$('#tree').addEventListener('contextmenu', (e) => {
  e.preventDefault(); // WebKit's own menu
  if (!wsId() || S.treeRename) return;
  const tid = wsId();
  const node = e.target.closest('.node[data-node]');
  if (node) { work(tid).treeSel = node.dataset.node; renderExplorer(); }
  $('#tree').focus();
  openCtx(e.clientX, e.clientY, ctxItems(tid, node ? node.dataset.node : null, Boolean(node) && node.dataset.dir === '1'));
});

$('#ctx').addEventListener('mousedown', (e) => e.preventDefault()); // the tree keeps the keyboard
$('#ctx').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-ctx]');
  if (!b || b.disabled) return;
  const it = ctxNow[Number(b.dataset.ctx)];
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
document.addEventListener('mousedown', () => {
  if (G.chord && G.chord.kind === 'tree') endChord('jump: explorer, cancelled by a click');
}, true);
window.addEventListener('blur', () => {
  if (G.chord && G.chord.kind === 'tree') endChord('jump: explorer, the window lost the keyboard');
});
