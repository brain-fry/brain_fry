'use strict';
// The search of the explorer (2a), asked by Renaud on 2026-11-14 and settled over two mockups
// (DESIGN_DETAILS.md "2a, 3a, 3b: files and terminals", "Explorer search"). Cmd+F (files.js) or
// Cmd+J F (grid.js) opens a field at the top of 2a. Enter searches the names and the text of the
// files of the whole project (the backend walks it: `fs.search`, treesearch.py) and the tree shows
// only the files that match, under their folders, all open, with the number of lines that match.
// The field stays. Esc closes it: the tree comes back as it was, the keyboard goes back.
// The query and the results belong to the view (work(tid).treeSearch, as the open folders);
// the options (the five toggles, include, exclude, the extended area) to the project, in
// window.json (store, app.js).
// Uses S, $, esc, request, toast, store, stored, uiLog, wsId, wsProject (app.js), work,
// renderExplorer, openFile, viewTabs, fileOnScreen (files.js), isCut, errText, drawTreeHints
// (explorer.js), endChord (grid.js), revealColumn (layout.js).

const TREE_SEARCH_DEFAULTS = { case: false, word: false, re: false, names: true, text: true, include: '', exclude: '', ext: false };
const TREE_SEARCH_MAX = 2000; // files in a result (treesearch.MAX_FILES)
const TREE_SEARCH_QUIET_MS = 300; // the refreshes of a tree come in bursts: one search for them
const treeSearchUi = { box: $('#tree-search'), q: $('#tree-search-q'), more: $('#tree-search-more'),
  ext: $('#tree-search-ext'), inc: $('#tree-search-inc'), exc: $('#tree-search-exc') };
S.treeSearchView = null; // the view whose search the area shows (its field text)

// the search of a view: open, the field's text, the query that ran and its results
function treeSearchOf(tid) {
  const w = work(tid);
  if (!w.treeSearch) {
    w.treeSearch = { open: false, typed: '', ran: '', res: null, byPath: new Map(), shut: new Set(), seq: 0, back: null, timer: null };
  }
  return w.treeSearch;
}

// the search of the view on screen, only if it exists (no work state is made for it)
function treeSearchNow() {
  const tid = wsId();
  const w = tid && S.work && S.work[tid];
  return w && w.treeSearch ? w.treeSearch : null;
}

const treeSearchShows = (ts) => Boolean(ts && ts.open && ts.res);

// ------------------------------------------------------------------ the options of a project

const treeSearchKey = (project) => `treeSearch:${project}`;

function treeSearchOptions(project) {
  const o = stored(treeSearchKey(project), null);
  return { ...TREE_SEARCH_DEFAULTS, ...(o && typeof o === 'object' ? o : {}) };
}

// an option of the extended area that differs from its default (the button turns blue)
const treeSearchSet = (o) => ['case', 'word', 're', 'names', 'text', 'include', 'exclude'].some((k) => o[k] !== TREE_SEARCH_DEFAULTS[k]);

function treeSearchSave(project, o) {
  const keep = {};
  for (const k of Object.keys(TREE_SEARCH_DEFAULTS)) keep[k] = o[k];
  const same = Object.keys(TREE_SEARCH_DEFAULTS).every((k) => keep[k] === TREE_SEARCH_DEFAULTS[k]);
  store(treeSearchKey(project), same ? null : keep); // null: forgotten, as the defaults
}

// the include and exclude fields into the options, when they changed
function treeSearchTakeFields(project) {
  const o = treeSearchOptions(project);
  const inc = treeSearchUi.inc.value.trim();
  const exc = treeSearchUi.exc.value.trim();
  if (o.include !== inc || o.exclude !== exc) treeSearchSave(project, { ...o, include: inc, exclude: exc });
}

// ------------------------------------------------------------------ open, close

// Cmd+F (files.js) and Cmd+J F (grid.js). Nothing without a project, outside Dev mode or with a
// picker open. Already open: the field takes the keyboard, its text selected.
function openTreeSearch() {
  const tid = wsId();
  if (S.mode !== 'dev' || S.picker || !tid || !wsProject(tid)) return false;
  revealColumn('explorer'); // hidden by Cmd+Option+2 (layout.js)
  const ts = treeSearchOf(tid);
  const a = document.activeElement;
  // where Esc gives the keyboard back: where it was when the search opened, or came from outside 2a
  if (!ts.open || !$('#explorer').contains(a)) ts.back = a && a !== document.body ? a : null;
  if (!ts.open) {
    ts.open = true;
    ts.typed = ts.ran; // the query that ran last in this view, if any
    treeSearchUi.q.value = ts.typed;
    S.treeSearchView = tid;
  }
  treeSearchSync(tid);
  renderExplorer();
  treeSearchUi.q.focus();
  treeSearchUi.q.select();
  return true;
}

function jumpToTreeSearch() { // Cmd+J F (grid.js), from anywhere, also from inside a file
  const tid = S.mode === 'dev' && wsId();
  const ok = Boolean(tid && wsProject(tid) && !S.picker);
  endChord(`jump: explorer search${ok ? '' : ' (no project)'}`);
  if (ok) openTreeSearch();
}

// Esc: the area goes, the tree comes back as it was (its open folders were never touched), and
// the keyboard goes back to where it was; gone or hidden since: the tree; nowhere: nowhere.
function closeTreeSearch() {
  const tid = wsId();
  const ts = treeSearchNow();
  if (!ts || !ts.open) return;
  Object.assign(ts, { open: false, res: null, byPath: new Map(), seq: ts.seq + 1 }); // a reply on the way is dropped
  clearTimeout(ts.timer);
  treeSearchSync(tid);
  renderExplorer();
  const b = ts.back;
  ts.back = null;
  if (b && b.isConnected && b.getClientRects().length) b.focus({ preventScroll: true });
  else if (b) $('#tree').focus({ preventScroll: true });
  else if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
}

// ------------------------------------------------------------------ search

// quiet: a refresh of the tree (an event of the task, landing on the view, Refresh): the query
// that ran, no toast, the folders closed in the results stay closed. Else: the field's text, and
// an empty field goes back to the plain tree.
async function treeSearchRun(tid, quiet = false) {
  const ts = treeSearchOf(tid);
  const project = wsProject(tid);
  if (!project) return;
  if (quiet && !treeSearchShows(ts)) return;
  const query = quiet ? ts.ran : ts.typed;
  if (!query) {
    Object.assign(ts, { ran: '', res: null, byPath: new Map(), seq: ts.seq + 1 });
    if (tid === wsId()) { treeSearchSync(tid); renderExplorer(); }
    return;
  }
  if (!quiet) treeSearchTakeFields(project);
  const o = treeSearchOptions(project);
  const seq = ++ts.seq;
  let r;
  try {
    r = await request('fs.search', { id: tid, text: query, case: o.case, word: o.word, regex: o.re, names: o.names,
      content: o.text, include: o.include, exclude: o.exclude }); // `text`: the backend never logs it
  } catch (e) {
    if (!quiet && seq === ts.seq) toast(errText(e)); // an invalid regular expression: the results stay
    return;
  }
  if (seq !== ts.seq || !ts.open) return; // a newer search started, or the search closed meanwhile
  Object.assign(ts, { ran: query, res: r, byPath: new Map(r.files.map((f) => [f.path, f])) });
  if (!quiet) {
    ts.shut = new Set();
    if (r.more) toast(`More than ${TREE_SEARCH_MAX.toLocaleString('en-US')} files match: only the first ${TREE_SEARCH_MAX.toLocaleString('en-US')} show.`);
  }
  if (tid !== wsId()) return;
  treeSearchSync(tid);
  renderExplorer();
  if (!quiet) $('#tree').scrollTop = 0;
}

// refreshTree (files.js): a search that shows runs again, quietly, so that counts do not go stale
function treeSearchRefresh(tid) {
  const w = S.work && S.work[tid];
  const ts = w && w.treeSearch;
  if (!treeSearchShows(ts)) return;
  clearTimeout(ts.timer);
  ts.timer = setTimeout(() => treeSearchRun(tid, true), TREE_SEARCH_QUIET_MS);
}

// ------------------------------------------------------------------ the area and the results

// The area shows the search of the view on screen. Called by renderExplorer (files.js) at each
// redraw: cheap, and the field's text changes only when the view does.
function treeSearchSync(tid) {
  const ui = treeSearchUi;
  const project = tid && wsProject(tid);
  const w = project && S.work && S.work[tid];
  const ts = w && w.treeSearch;
  const open = Boolean(ts && ts.open);
  if (S.mode === 'dev' && S.treeSearchView !== tid) { // another view: its own text and results
    S.treeSearchView = tid;
    ui.q.value = ts ? ts.typed : '';
    if (treeSearchShows(ts)) treeSearchRefresh(tid); // landing on a view refreshes its tree: the results too
  }
  if (!open && !ui.box.hidden && ui.box.contains(document.activeElement)) $('#tree').focus({ preventScroll: true });
  ui.box.hidden = !open;
  $('#tree-collapse').hidden = treeSearchShows(ts); // no folder to collapse in the results
  if (!open) return;
  const o = treeSearchOptions(project);
  for (const b of ui.box.querySelectorAll('[data-tree-search-opt]')) b.classList.toggle('on', Boolean(o[b.dataset.treeSearchOpt]));
  if (document.activeElement !== ui.inc) ui.inc.value = o.include;
  if (document.activeElement !== ui.exc) ui.exc.value = o.exclude;
  ui.ext.hidden = !o.ext;
  ui.more.classList.toggle('on', o.ext);
  ui.more.classList.toggle('set', !o.ext && treeSearchSet(o));
}

// the name of a file, its matches in a faint grey pill (spans in UTF-16 units, as JS strings)
function treeSearchName(name, spans) {
  let out = '';
  let at = 0;
  for (const [a, b] of spans || []) {
    if (a < at) continue;
    out += `${esc(name.slice(at, a))}<mark class="tree-search-hit">${esc(name.slice(a, b))}</mark>`;
    at = b;
  }
  return out + esc(name.slice(at));
}

// renderExplorer (files.js): the results in place of the tree, or false. The backend gives the
// files in the order of the tree (folders first, then by lowercase name), so each folder row is
// drawn before the first of its files. Rows keep data-node and data-dir: clicks, the right-click
// menu, Cmd+J E and the mark of the file on screen work as in the tree.
function treeSearchDraw(tid, w, el) {
  treeSearchSync(tid);
  const ts = w.treeSearch;
  if (!treeSearchShows(ts)) return false;
  const project = wsProject(tid);
  const shown = fileOnScreen(w);
  const rows = [];
  const drawn = new Set();
  const mark = (path) => `${path === w.treeSel ? ' sel' : ''}${isCut(project, path) ? ' cut' : ''}`;
  for (const f of ts.res.files) {
    const parts = f.path.split('/');
    let hidden = false;
    for (let i = 1; i < parts.length && !hidden; i++) {
      const dir = parts.slice(0, i).join('/');
      const shut = ts.shut.has(dir);
      if (!drawn.has(dir)) {
        drawn.add(dir);
        rows.push(`<div class="node dir tree-search-res${mark(dir)}" data-node="${esc(dir)}" data-dir="1" style="padding-left:${8 + (i - 1) * 14}px">`
          + `<span class="caret">${shut ? '▸' : '▾'}</span><span class="name">${esc(parts[i - 1])}/</span></div>`);
      }
      hidden = shut;
    }
    if (hidden) continue;
    const name = parts[parts.length - 1];
    rows.push(`<div class="node file tree-search-res${f.path === shown ? ' active' : ''}${mark(f.path)}" data-node="${esc(f.path)}" data-dir="0"`
      + ` style="padding-left:${8 + (parts.length - 1) * 14}px"><span class="caret"></span><span class="name">${treeSearchName(name, f.name)}</span>`
      + `${f.count ? `<span class="tree-search-count">${f.count}</span>` : ''}</div>`);
  }
  const html = rows.join('');
  // the same rows already on screen: not redrawn (renderExplorer runs at each state push)
  if (!(html && el.treeSearchHtml === html && el.firstElementChild && el.firstElementChild.classList.contains('tree-search-res'))) {
    el.innerHTML = html;
    el.treeSearchHtml = html;
  }
  if (G.chord && G.chord.kind === 'tree') drawTreeHints(); // Cmd+J E: the labels follow the rows (explorer.js)
  return true;
}

// A click on a row of the results, or its key after Cmd+J E (clickTreeNode, files.js), or Enter.
// A folder closes or opens in the results only (the tree's open folders stay as they were). A file
// opens as from the tree (the keyboard stays in 2a; double: the second click, to the file), and
// the first match of its text is selected. False: no results on screen.
function treeSearchClick(path, dir, double) {
  const tid = wsId();
  const ts = treeSearchNow();
  if (!tid || !treeSearchShows(ts)) return false;
  const w = work(tid);
  w.treeSel = path;
  if (dir) {
    if (!double) { if (ts.shut.has(path)) ts.shut.delete(path); else ts.shut.add(path); }
    renderExplorer();
    return true;
  }
  treeSearchOpen(tid, path, double);
  return true;
}

async function treeSearchOpen(tid, path, focus) {
  const ts = treeSearchOf(tid);
  const f = ts.byPath.get(path);
  renderExplorer();
  await openFile(tid, path, focus);
  if (!f || !f.count || !f.line) return;
  const tab = viewTabs(work(tid)).find((t) => t.path === path && !t.thread);
  if (!tab || !tab.view || tab.kind !== 'text') return;
  const doc = tab.view.state.doc;
  const line = doc.line(Math.min(Math.max(f.line, 1), doc.lines)); // unsaved edits may have moved it
  const from = Math.min(line.from + f.col, line.to);
  const to = Math.min(from + f.len, doc.length);
  tab.view.dispatch({ selection: { anchor: from, head: to }, effects: CM.EditorView.scrollIntoView(from, { y: 'center' }) });
}

// Up / Down over the file rows of the results; Up on the first one goes back to the field.
function treeSearchMove(d) {
  const tree = $('#tree');
  const w = work(wsId());
  const rows = [...tree.querySelectorAll('.node[data-node]')];
  const cur = rows.findIndex((r) => r.dataset.node === w.treeSel);
  const next = d > 0 ? rows.slice(cur + 1).find((r) => r.dataset.dir === '0')
    : rows.slice(0, Math.max(cur, 0)).reverse().find((r) => r.dataset.dir === '0');
  if (!next) {
    if (d < 0) { treeSearchUi.q.focus(); treeSearchUi.q.select(); }
    return;
  }
  w.treeSel = next.dataset.node;
  renderExplorer();
  const row = treeRow(w.treeSel); // explorer.js
  if (row) row.scrollIntoView({ block: 'nearest' });
}

// The right-click menu in the results (explorer.js): no New File..., New Folder... or Paste, whose
// new entry would show in the tree, not in the results. The rest works on the rows as in the tree.
function treeSearchMenu(items) {
  if (!treeSearchShows(treeSearchNow())) return items;
  const out = [];
  for (const it of items) {
    if (it !== '-' && /^(New File|New Folder|Paste)/.test(it.label)) continue;
    if (it === '-' && (!out.length || out[out.length - 1] === '-')) continue;
    out.push(it);
  }
  while (out[out.length - 1] === '-') out.pop();
  return out;
}

// ------------------------------------------------------------------ events

const treeSearchPlain = (e) => !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && !e.isComposing;

treeSearchUi.q.addEventListener('input', () => {
  const tid = wsId();
  if (tid && wsProject(tid)) treeSearchOf(tid).typed = treeSearchUi.q.value;
});

// Enter searches, in the field and in include / exclude; the field keeps the keyboard. Down: into the results.
treeSearchUi.box.addEventListener('keydown', (e) => {
  const tid = wsId();
  if (!tid || !wsProject(tid) || !treeSearchPlain(e)) return;
  if (e.key === 'Enter' && e.target.matches('input')) {
    e.preventDefault();
    treeSearchRun(tid);
  } else if (e.key === 'ArrowDown' && e.target === treeSearchUi.q) {
    e.preventDefault();
    const first = $('#tree').querySelector('.node.file.tree-search-res');
    if (!treeSearchShows(treeSearchNow()) || !first) return;
    work(tid).treeSel = first.dataset.node;
    $('#tree').focus({ preventScroll: true });
    renderExplorer();
    first.scrollIntoView({ block: 'nearest' });
  }
});

[treeSearchUi.inc, treeSearchUi.exc].forEach((f) => f.addEventListener('change', () => {
  const tid = wsId();
  if (tid && wsProject(tid)) treeSearchTakeFields(wsProject(tid));
}));

// the buttons do not take the keyboard: it stays in the field where it was
treeSearchUi.box.addEventListener('mousedown', (e) => { if (e.target.closest('button')) e.preventDefault(); });

treeSearchUi.box.addEventListener('click', (e) => {
  const tid = wsId();
  const project = tid && wsProject(tid);
  if (!project) return;
  if (e.target.closest('#tree-search-more')) { // the extended area, open or closed
    const o = treeSearchOptions(project);
    treeSearchSave(project, { ...o, ext: !o.ext });
    treeSearchSync(tid);
    return;
  }
  const b = e.target.closest('[data-tree-search-opt]');
  if (!b) return;
  const k = b.dataset.treeSearchOpt;
  const o = treeSearchOptions(project);
  if ((k === 'names' && o.names && !o.text) || (k === 'text' && o.text && !o.names)) {
    toast('Names or text: one of the two stays on.');
    return;
  }
  treeSearchSave(project, { ...o, [k]: !o[k] });
  treeSearchSync(tid);
  if (treeSearchShows(treeSearchOf(tid))) treeSearchRun(tid); // a query has run: again, with the new option
});

// Keys of 2a while the search is open, before explorer.js's (Enter renames there): Esc closes it
// (from the field, the extended area or the tree); in the results Up / Down move over the files
// and Enter opens the selected one. F2, Cmd+Backspace and Ctrl+C stay explorer.js's.
$('#explorer').addEventListener('keydown', (e) => {
  const ts = treeSearchNow();
  if (!ts || !ts.open || S.treeRename || S.mode !== 'dev') return;
  if (e.key === 'Escape' && treeSearchPlain(e)) {
    e.preventDefault(); e.stopPropagation();
    closeTreeSearch();
    return;
  }
  if (e.target !== $('#tree') || !treeSearchShows(ts) || !treeSearchPlain(e)) return;
  if (e.key === 'ArrowDown') treeSearchMove(1);
  else if (e.key === 'ArrowUp') treeSearchMove(-1);
  else if (e.key === 'Enter') {
    const path = work(wsId()).treeSel;
    const row = path && treeRow(path);
    if (row) treeSearchClick(path, row.dataset.dir === '1', false);
  } else return;
  e.preventDefault(); e.stopPropagation();
}, true);
