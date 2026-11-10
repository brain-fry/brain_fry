'use strict';
// The right-click menu of the explorer (2a) and its keys, as in VS Code on macOS: Enter (or F2)
// renames the selected entry, Cmd+Backspace moves it to the Trash, Ctrl+C copies its relative path. Cut / Copy / Paste move or copy
// files and folders, also between projects. See DESIGN.md "2a, 3a, 3b".
// Uses S, $, esc, request, send, toast, store (app.js) and work, refreshTree, renderExplorer,
// renderFiles, saveOpenFiles, closeFile (files.js).

// What Cut or Copy put aside for Paste: { tid, project, paths, cut }. Kept in Brain Fry only,
// not on the clipboard of macOS: Finder cannot paste it.
S.fileClip = null;
S.treeRename = null; // { tid, path } while a name field is open in the tree

const nameOf = (path) => path.split('/').pop();
const parentOf = (path) => path.split('/').slice(0, -1).join('/');
const errText = (e) => e.message.replace(/^\w+Error: /, ''); // "FileExistsError: a.py already exists here"

function isCut(task, path) { // renderExplorer dims it
  const c = S.fileClip;
  return Boolean(c && c.cut && c.project === task.projectPath && c.paths.includes(path));
}

function treeRow(path) {
  return [...document.querySelectorAll('#tree .node[data-node]')].find((n) => n.dataset.node === path) || null;
}

function sameProject(project, fn) { // fn(task id, its work) for each task of the project that has a work state
  for (const [id, w] of Object.entries(S.work || {})) {
    const t = S.state.tasks[id];
    if (t && t.projectPath === project) fn(id, w);
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

function menuAction(name) { // the File menu of the menu bar (main.py)
  if (name === 'refresh' && S.selected) refreshTree(S.selected);
}

// ------------------------------------------------------------------ actions

function startRename(tid, path) {
  const row = treeRow(path);
  if (!row || tid !== S.selected || S.treeRename) return;
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
  const project = S.state.tasks[tid].projectPath;
  movedPaths(project, [[path, to]]);
  treesChanged(project);
}

async function deletePath(tid, path, dir) {
  if (!confirm(`Move ${nameOf(path)}${dir ? '/ and everything in it' : ''} to the Trash?`)) return;
  try { await request('fs.trash', { id: tid, path }); } catch (e) { toast(errText(e)); return; }
  const project = S.state.tasks[tid].projectPath;
  gonePaths(project, [path]);
  treesChanged(project);
}

function setClip(tid, path, cut) {
  S.fileClip = { tid, project: S.state.tasks[tid].projectPath, paths: [path], cut };
  renderExplorer();
}

async function pasteInto(tid, dir) {
  const c = S.fileClip;
  if (!c) return;
  let pairs;
  try { pairs = await request('fs.paste', { id: tid, from: c.tid, paths: c.paths, dir, cut: c.cut }); } catch (e) { toast(errText(e)); return; }
  const project = S.state.tasks[tid].projectPath;
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
    for (const tab of w.tabs) {
      const to = tab.thread || tab.web ? null : map(tab.path);
      if (to === null) continue;
      if (w.active === tab.path) { w.active = to; store(`active:${id}`, to); }
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
    if (moved) saveOpenFiles(id);
  });
  renderFiles();
}

// Files and folders gone from `project` (Trash, or moved to another project): their tabs close,
// except those with unsaved edits, which stay as "Deleted on disk" (Save writes the file again).
function gonePaths(project, paths) {
  const under = (p) => paths.some((a) => p === a || p.startsWith(`${a}/`));
  sameProject(project, (id, w) => {
    for (const tab of [...w.tabs]) {
      if (tab.thread || tab.web || !under(tab.path)) continue;
      if (tab.dirty) tab.gone = true;
      else closeFile(id, tab.path);
    }
    w.open = new Set([...w.open].filter((p) => !under(p)));
    if (w.treeSel && under(w.treeSel)) w.treeSel = null;
  });
  renderFiles();
}

function treesChanged(project) { // the selected task's tree reloads now, the others when they are selected
  sameProject(project, (id, w) => { if (id === S.selected) refreshTree(id); else w.kids.clear(); });
  renderExplorer();
}

// ------------------------------------------------------------------ events

$('#tree').addEventListener('contextmenu', (e) => {
  e.preventDefault(); // WebKit's own menu
  if (!S.selected || S.treeRename) return;
  const tid = S.selected;
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
  if (!S.selected) return;
  work(S.selected).open.clear();
  renderExplorer();
  $('#tree').focus();
});

$('#tree').addEventListener('keydown', (e) => {
  if (e.target !== $('#tree') || !S.selected || S.mode !== 'dev') return;
  const path = work(S.selected).treeSel;
  const row = path && treeRow(path);
  if (!row) return;
  const plain = !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
  if ((e.key === 'Enter' || e.key === 'F2') && plain) startRename(S.selected, path);
  else if (e.key === 'Backspace' && e.metaKey) deletePath(S.selected, path, row.dataset.dir === '1');
  else if (e.key.toLowerCase() === 'c' && e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) copyPath(S.selected, path, true);
  else return;
  e.preventDefault(); e.stopPropagation();
});
