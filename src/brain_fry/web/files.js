'use strict';
// 2a (explorer) and the tabs of column 3 (CodeMirror for text, viewers for images and PDFs), in
// the panes of grid.js. Each task has its own tree state and its own tabs, restored when it is
// selected. With no task selected, the idle screen shows the tabs of the task that I left (S.idle),
// and a project picked in the dropdown has its own work state (`wsId`, app.js). A tab belongs to a
// task or to its project (project tabs, `project:<path>`, app.js): every view of the project shows
// the project's tabs too (viewTabs), and a tab opened with no task selected is one of them.
// A tab's `kind`: text, image, pdf, thread (1b),
// web (browser.js), shell (shells.js) or finder (finder.js); an untitled text tab has `untitled` (Cmd+N F).
// Uses S, $, esc, request, send, toast, store, stored from app.js, which loads first, and the
// panes of grid.js (PANES, paneTabs, paneActive, addTab, showTab, dropTab, openPane...).

const POLL_MS = 2000; // checks the open files for changes on disk (the agent edits them too)

function work(tid) {
  S.work = S.work || {};
  if (!S.work[tid]) {
    S.work[tid] = { id: tid, open: new Set(), kids: new Map(), loading: new Set(), tabs: [], restored: false, lastUpdated: null,
      treeSel: null, // the explorer's selected entry
      act: {}, // pane -> the path of the tab on screen in it
      pane: 1, mru: [...PANES], // the pane that I used last, and the panes from the most recently used
      want: {}, // pane -> the id of the tab to show in it once it exists (restored: tabId, grid.js)
      grid: null, // the remembered grid (loadGrid), read at the first restore or shell
      nextShells: [] }; // [{pane, focus}]: terminals asked for, not in the state yet (shells.js, copytab.js)
  }
  return S.work[tid];
}

// a tab that shows a file of the project (the poll, the explorer, files.set)
const isFileTab = (t) => !t.thread && !t.web && !t.shell && !t.untitled && !t.finder;

// ------------------------------------------------------------------ project tabs
// The tabs that a view shows: its own and, for a task, its project's, which come first in each
// pane. w.tabs holds only the tabs that w owns; act, pane and mru belong to the view.
function viewTabs(w) {
  if (isProjectWs(w.id)) return w.tabs;
  const p = wsProject(w.id);
  const pw = p && S.work && S.work[projectWs(p)];
  return pw && pw.tabs.length ? [...pw.tabs, ...w.tabs] : w.tabs;
}

// the view that shows the tabs of `tid` now (the screen's, for a project tab), else their owner's
const viewWork = (tid) => work(inView(tid) ? wsId() : tid);

// the work states whose view shows this tab: its owner, and for a project tab each task of the project
function viewsOf(tab) {
  if (!isProjectWs(tab.tid)) return [work(tab.tid)];
  const project = wsProject(tab.tid);
  return Object.values(S.work || {}).filter((w) => w.id === tab.tid || (!isProjectWs(w.id) && wsProject(w.id) === project));
}

// A tab moves to another owner (a double-click, or a terminal that the backend moved): out of
// its owner's list, into the end of the other's. It keeps its pane and its element.
function reown(tab, to) {
  const from = work(tab.tid);
  const i = from.tabs.indexOf(tab);
  if (i >= 0) from.tabs.splice(i, 1);
  tab.tid = to;
  work(to).tabs.push(tab);
}

// A file that becomes a project tab: its tabs in the tasks of the project close, since every task
// shows the project's now. They share its text (peerTab): no edit is lost.
function dropDuplicates(tab) {
  const project = wsProject(tab.tid);
  for (const w of Object.values(S.work || {})) {
    if (isProjectWs(w.id) || wsProject(w.id) !== project) continue;
    const dup = w.tabs.find((t) => t !== tab && isFileTab(t) && t.path === tab.path);
    if (!dup) continue;
    destroyTab(w, dup);
    saveOpenFiles(w.id);
    saveGrid(w.id);
  }
}

// A double-click on a tab (asked by Renaud on 2026-11-11, instead of a key): a task's tab becomes
// a project tab, which every task of the project shows; a project tab goes back to the task on
// screen (the selected one, or the idle screen's). Not a thread (it belongs to its task), and
// nothing in the project view, which has no task to give it to. A terminal is moved by the
// backend (its shell's owner), and the state moves its tab (renderShells).
function toggleProjectTab(tab) {
  const view = wsId();
  if (!view || tab.thread || tab.closing) return;
  const from = tab.tid;
  const to = isProjectWs(from) ? (isProjectWs(view) ? null : view) : projectWs(wsProject(from));
  if (!to) return;
  uiLog(`tab: ${isProjectWs(to) ? 'becomes a project tab' : 'back to the task'} (${tab.kind})`);
  if (tab.shell) { send('shell.move', { key: tab.key, to }); return; }
  reown(tab, to);
  if (isFileTab(tab) && isProjectWs(to)) dropDuplicates(tab);
  for (const tid of [from, to]) {
    if (isFileTab(tab)) saveOpenFiles(tid);
    else if (tab.web) saveWebTabs(tid);
    else if (tab.finder) saveFinderTabs(tid);
    saveGrid(tid);
  }
  renderFiles();
  renderExplorer();
}

// ------------------------------------------------------------------ one text per file
// The text tabs of one file, in all the tasks of a project, show one text (VS Code: one model per
// file, shown by all its editors). Each tab keeps its own pane, cursor, scroll, Diff and history;
// the text and the state of the file are shared: `tab.file` holds that state, which the tab reads
// and writes through the properties of FILE_STATE (so `tab.dirty = false` sets it for all of them).
// An edit in one tab is applied at once to the others (docChanged). An untitled tab has its own.
const FILE_STATE = ['version', 'head', 'dirty', 'diskChanged', 'gone'];
let syncing = false; // the other tabs of a file are taking a change: it is not a new edit

function shareFile(tab, file) {
  tab.file = file;
  for (const k of FILE_STATE) {
    Object.defineProperty(tab, k, { get() { return this.file[k]; }, set(v) { this.file[k] = v; }, enumerable: true });
  }
}

// a text tab of `path` in another task (or the same) of the project of `tid`
function peerTab(tid, path, except = null) {
  const project = wsProject(tid);
  for (const w of Object.values(S.work || {})) {
    for (const t of w.tabs) if (t !== except && t.file && !t.untitled && t.path === path && wsProject(t.tid) === project) return t;
  }
  return null;
}

// the tabs that show the same text as `tab`, itself included
function sameText(tab) {
  if (!tab.file) return [tab];
  const out = [];
  for (const w of Object.values(S.work || {})) for (const t of w.tabs) if (t.file === tab.file) out.push(t);
  return out;
}

// closing this tab loses unsaved edits: no other tab holds them
const losesEdits = (tab) => tab.dirty && sameText(tab).length === 1;

// `text` replaces the whole text of these tabs (a reload, a Save As), each cursor kept where it can be
function replaceText(tabs, text) {
  syncing = true;
  try {
    for (const t of tabs) {
      const anchor = Math.min(t.view.state.selection.main.anchor, text.length);
      t.view.dispatch({ changes: { from: 0, to: t.view.state.doc.length, insert: text }, selection: { anchor } });
    }
  } finally {
    syncing = false;
  }
}

// An edit in a tab (typing, undo, replace...): the other tabs of the file take the same changes,
// and the first edit marks the file unsaved in all of them.
function docChanged(tab, changes) {
  syncing = true;
  try {
    for (const t of sameText(tab)) {
      if (t === tab) continue;
      if (t.view.state.doc.length === changes.length) t.view.dispatch({ changes });
      else t.view.dispatch({ changes: { from: 0, to: t.view.state.doc.length, insert: tab.view.state.doc } }); // not expected: they differ
    }
  } finally {
    syncing = false;
  }
  if (!tab.dirty) { tab.dirty = true; renderFileTabs(); }
}

function tabName(t) {
  if (t.thread) return `Thread ${t.thread.n}`;
  if (t.web) return webTabName(t);
  if (t.finder) return finderName(t);
  if (t.shell || t.untitled) return t.name;
  return t.path.split('/').pop();
}

// the file on screen in the pane that I used last: marked in 2a
function fileOnScreen(w) {
  for (const n of w.mru) {
    const t = paneActive(w, n);
    if (t && isFileTab(t)) return t.path;
  }
  return null;
}

// ------------------------------------------------------------------ explorer (2a)

async function loadDir(tid, path) {
  const w = work(tid);
  if (w.loading.has(path)) return;
  w.loading.add(path);
  try {
    w.kids.set(path, await request('fs.list', { id: tid, path }));
  } catch (e) {
    w.kids.set(path, []);
    toast(e.message);
  } finally {
    w.loading.delete(path);
  }
  if (tid === wsId()) renderExplorer();
}

function refreshTree(tid) {
  const w = work(tid);
  for (const path of ['', ...w.open]) if (w.kids.has(path)) loadDir(tid, path);
}

function renderExplorer() {
  if (S.treeRename) return; // a redraw would take the name field away (explorer.js redraws after it)
  const tid = wsId(); // the selected task, or the project picked while it has no open task
  const task = tid && S.state.tasks[tid];
  const project = tid && wsProject(tid);
  const el = $('#tree');
  if (!project) { el.innerHTML = ''; return; }
  const w = work(tid);
  if (task && task.updated !== w.lastUpdated) { // an event arrived: the agent may have added files
    const first = w.lastUpdated === null;
    w.lastUpdated = task.updated;
    if (!first) refreshTree(tid);
  }
  if (!w.kids.has('')) { loadDir(tid, ''); el.innerHTML = ''; return; }
  const rows = [];
  const shown = fileOnScreen(w);
  const walk = (path, depth) => {
    for (const e of w.kids.get(path) || []) {
      const pad = 8 + depth * 14;
      const open = e.dir && w.open.has(e.path);
      const cls = ['node', e.dir ? 'dir' : 'file', !e.dir && e.path === shown ? 'active' : '',
        e.path === w.treeSel ? 'sel' : '', isCut(project, e.path) ? 'cut' : ''].join(' ');
      rows.push(`<div class="${cls}" data-node="${esc(e.path)}" data-dir="${e.dir ? 1 : 0}" style="padding-left:${pad}px">`
        + `<span class="caret">${e.dir ? (open ? '▾' : '▸') : ''}</span><span class="name">${esc(e.name)}${e.dir ? '/' : ''}</span></div>`);
      if (open) {
        if (w.kids.has(e.path)) walk(e.path, depth + 1);
        else { loadDir(tid, e.path); rows.push(`<div class="node loading" style="padding-left:${pad + 14}px">...</div>`); }
      }
    }
  };
  walk('', 0);
  el.innerHTML = rows.join('');
  if (G.chord && G.chord.kind === 'tree') drawTreeHints(); // Cmd+J E: the labels follow the new rows (explorer.js)
}

// A click on a row of 2a, or its key after Cmd+J E (explorer.js). double: the second click.
function clickTreeNode(path, dir, double) {
  const w = work(wsId());
  w.treeSel = path;
  if (dir) {
    if (double) return;
    if (w.open.has(path)) w.open.delete(path); else w.open.add(path);
    renderExplorer();
  } else if (double) {
    openFile(wsId(), path, true);
  } else {
    openFile(wsId(), path, false);
    renderExplorer();
  }
}

// ------------------------------------------------------------------ file tabs (3a)

// Cmd+G: every occurrence selected, to edit them all at once (selectAllOccurrences, in
// frontend/codemirror.js). Above basicSetup, whose Cmd+G is find next. In a thread tab
// (read-only) it does nothing.
const SELECT_ALL_KEYS = CM.Prec.high(CM.keymap.of([
  { key: 'Mod-g', run: CM.selectAllOccurrences, scope: 'editor search-panel', preventDefault: true },
]));

// The element joins its pane in addTab (grid.js). `peer`: a tab of the file in another task,
// whose text and state this one shares (its unsaved edits included), rather than the disk's.
function makeTab(tid, path, r, peer = null) {
  const el = document.createElement('div');
  el.className = 'editor';
  const tab = { tid, path, el, kind: 'text', diff: false, diffC: new CM.Compartment(), langC: new CM.Compartment(),
    wrap: stored(wrapKey(tid, path), false), wrapC: new CM.Compartment(), crlf: peer ? peer.crlf : r.text.includes('\r\n') };
  shareFile(tab, peer ? peer.file : { version: r.version, head: r.head, dirty: false, diskChanged: false, gone: false });
  tab.view = new CM.EditorView({
    parent: el,
    state: CM.EditorState.create({
      doc: peer ? peer.view.state.doc : r.text,
      extensions: [
        CM.basicSetup,
        SELECT_ALL_KEYS,
        CM.keymap.of([CM.indentWithTab, { key: 'Mod-s', run: () => { saveTab(tab); return true; } }]),
        CM.oneDark,
        tab.langC.of(CM.languageFor(path)), // a rename may change the language (explorer.js)
        tab.diffC.of([]),
        tab.wrapC.of(tab.wrap ? CM.EditorView.lineWrapping : []),
        tab.crlf ? CM.EditorState.lineSeparator.of('\r\n') : [], // save CRLF files as CRLF
        CM.EditorView.updateListener.of((u) => {
          if (u.docChanged && !syncing) docChanged(tab, u.changes);
          if (u.selectionSet || u.docChanged) syncSelection();
        }),
        CM.EditorView.theme({
          '&': { height: '100%' },
          '.cm-scroller': { fontFamily: 'Menlo, Monaco, monospace', fontSize: '12.5px' },
        }),
      ],
    }),
  });
  return tab;
}

// ------------------------------------------------------------------ viewers: images and PDFs (3a)
// Their bytes come over HTTP, from /file/<task id>/<path>?v=<version> (server.py): a new version
// is a new URL, so a file that the agent rewrote is never an old copy from a cache. A viewer has
// no edits, so it always follows the disk: the 2 s poll reloads it, and a PDF keeps its page and
// zoom. A file that is half written fails to load: the old one stays on screen, and the poll
// tries the next version.

function fileUrl(tid, path, version) {
  return `/file/${encodeURIComponent(tid)}/${path.split('/').map(encodeURIComponent).join('/')}?v=${encodeURIComponent(version || '')}`;
}

function sizeOf(version) { return Number(String(version).split('-').pop()) || 0; } // version = "<mtime_ns>-<size>"

function fmtBytes(n) {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function makeViewer(tid, path, r, cls) {
  const el = document.createElement('div');
  el.className = `editor viewer ${cls}`;
  el.tabIndex = -1; // focusable: arrows and Page Up/Down scroll it
  return { tid, path, el, kind: r.kind, version: r.version, size: sizeOf(r.version), tried: null, error: null,
    dirty: false, diskChanged: false, gone: false };
}

// An image fits the panel (never enlarged); a click on it, or the 100% button, shows it at its size.
function makeImageTab(tid, path, r) {
  const tab = makeViewer(tid, path, r, 'image-viewer fit');
  tab.fit = true;
  tab.img = document.createElement('img');
  tab.img.alt = path;
  tab.img.addEventListener('load', () => {
    tab.dims = `${tab.img.naturalWidth} × ${tab.img.naturalHeight}`;
    tab.error = null;
    if (inView(tab.tid)) renderFiles();
  });
  tab.img.addEventListener('error', () => { tab.error = 'WebKit cannot show this image.'; if (inView(tab.tid)) renderFiles(); });
  tab.img.addEventListener('click', () => setFit(tab, !tab.fit));
  tab.el.appendChild(tab.img);
  tab.img.src = fileUrl(tid, path, r.version);
  return tab;
}

function setFit(tab, fit) {
  tab.fit = fit;
  tab.el.classList.toggle('fit', fit);
  renderFiles();
}

function reloadImage(tab, version) {
  const next = new Image(); // loaded aside, then swapped in: no blank between the two versions
  next.onload = () => {
    tab.img.src = next.src;
    Object.assign(tab, { version, size: sizeOf(version), gone: false, error: null });
    renderFiles();
  };
  next.src = fileUrl(tab.tid, tab.path, version);
}

// PDF.js is loaded at the first PDF: about 2 MB that most sessions never need. One worker
// serves every PDF. The viewer module reads globalThis.pdfjsLib, which pdf.min.mjs sets.
const PDFJS = '/vendor/pdfjs';
let pdfjs = null;

function loadPdfjs() {
  pdfjs = pdfjs || (async () => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = `${PDFJS}/pdf_viewer.css`;
    const cssLoaded = new Promise((done) => { css.onload = done; css.onerror = done; });
    document.head.appendChild(css);
    await import('/pdf_polyfill.mjs');
    const lib = await import(`${PDFJS}/pdf.min.mjs`);
    lib.GlobalWorkerOptions.workerSrc = '/pdf_worker.mjs'; // the polyfill, then PDF.js's worker
    const viewer = await import(`${PDFJS}/pdf_viewer.min.mjs`);
    await cssLoaded;
    return { lib, viewer, worker: new lib.PDFWorker() };
  })().catch((e) => { pdfjs = null; throw e; });
  return pdfjs;
}

const PDF_SCALES = [['page-width', 'Width'], ['page-fit', 'Page'], ['0.5', '50%'], ['0.75', '75%'], ['1', '100%'],
  ['1.25', '125%'], ['1.5', '150%'], ['2', '200%'], ['custom', '']];

// A PDF: PDF.js's page viewer (continuous scroll, text selection, links, find), fit to the
// width at first. Its tools (page, zoom, find) are one element that sits in the bar of 3a while
// the tab is shown; the bar keeps it across renders, so the find field keeps its focus.
function makePdfTab(tid, path, r) {
  const tab = makeViewer(tid, path, r, 'pdf-viewer');
  tab.el.removeAttribute('tabindex'); // the container scrolls, not the tab
  tab.el.innerHTML = '<div class="pdf-container" tabindex="-1"><div class="pdfViewer"></div></div>';
  tab.container = tab.el.firstChild;
  tab.tools = document.createElement('span');
  tab.tools.className = 'pdf-tools';
  tab.tools.innerHTML = `<input class="pdf-page" title="Page (Enter goes to it)"><span class="pdf-pages"></span>
    <button data-pdf="out" title="Zoom out (Cmd+scroll, pinch)">-</button>
    <select class="pdf-scale" title="Zoom">${PDF_SCALES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
    <button data-pdf="in" title="Zoom in (Cmd+scroll, pinch)">+</button>
    <input class="pdf-find" placeholder="Find (Cmd+F)" title="Enter: next · Shift+Enter: previous · Esc: back to the PDF">
    <span class="pdf-count"></span>`;
  const $t = (sel) => tab.tools.querySelector(sel);
  $t('.pdf-page').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== 'Escape') return;
    e.preventDefault();
    const n = parseInt($t('.pdf-page').value, 10);
    if (e.key === 'Enter' && tab.viewer && n >= 1 && n <= tab.viewer.pagesCount) tab.viewer.currentPageNumber = n;
    tab.container.focus();
    pdfTools(tab);
  });
  $t('.pdf-scale').addEventListener('change', (e) => {
    if (tab.viewer && e.target.value !== 'custom') tab.viewer.currentScaleValue = e.target.value;
    tab.container.focus();
  });
  tab.tools.addEventListener('click', (e) => {
    const b = e.target.closest('[data-pdf]');
    if (!b || !tab.viewer) return;
    if (b.dataset.pdf === 'in') tab.viewer.increaseScale(); else tab.viewer.decreaseScale();
  });
  $t('.pdf-find').addEventListener('input', () => pdfFind(tab, ''));
  $t('.pdf-find').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); pdfFind(tab, 'again', e.shiftKey); }
    if (e.key === 'Escape') { e.preventDefault(); tab.container.focus(); }
  });
  // PDF.js's viewer does not fit itself to a new size (its app does it on a window resize). And
  // a tab that was hidden (display: none, another tab, Organize mode) comes back with its scroll
  // position forgotten: tab.loc, the last location that PDF.js reported while it was on screen
  // (page, and the point at the top left, in PDF units), brings it back.
  new ResizeObserver(() => {
    const v = tab.viewer;
    if (!tab.container.clientHeight) { tab.hidden = true; return; }
    if (v && v.pdfDocument) {
      const loc = tab.loc; // before the fit: it scrolls, and PDF.js reports the new location at once
      if (isNaN(v.currentScaleValue)) v.currentScaleValue = v.currentScaleValue; // page-width, page-fit: fit again
      if (tab.hidden && loc) pdfGoTo(tab, loc);
    }
    tab.hidden = false;
  }).observe(tab.container);
  tab.container.addEventListener('wheel', (e) => { // pinch arrives as a wheel event with ctrlKey
    if (!(e.ctrlKey || e.metaKey) || !tab.doc) return;
    e.preventDefault();
    const dy = Math.max(-25, Math.min(25, e.deltaY)); // a mouse wheel notch is 100: one notch, one step
    tab.viewer.updateScale({ scaleFactor: Math.exp(-dy / 100), origin: [e.clientX, e.clientY], drawingDelay: 300 });
  }, { passive: false });
  tab.ready = setupPdf(tab, r.version);
  return tab;
}

async function setupPdf(tab, version) {
  let V;
  try { V = (await loadPdfjs()).viewer; } catch (e) { tab.error = `PDF.js did not load: ${e.message}`; renderFiles(); return; }
  const bus = tab.eventBus = new V.EventBus();
  tab.link = new V.PDFLinkService({ eventBus: bus, externalLinkTarget: V.LinkTarget.BLANK }); // web links: the default browser
  tab.find = new V.PDFFindController({ eventBus: bus, linkService: tab.link });
  tab.viewer = new V.PDFViewer({ container: tab.container, eventBus: bus, linkService: tab.link, findController: tab.find });
  tab.link.setViewer(tab.viewer);
  for (const ev of ['pagesinit', 'pagechanging', 'scalechanging']) bus.on(ev, () => pdfTools(tab));
  // Not while hidden, nor when shown again before the ResizeObserver restored tab.loc: WebKit may
  // have reset the scroll position, and PDF.js would report page 1 first.
  bus.on('updateviewarea', ({ location }) => { if (!tab.hidden && tab.container.clientHeight) tab.loc = location; });
  bus.on('updatefindmatchescount', ({ matchesCount }) => pdfCount(tab, matchesCount));
  bus.on('updatefindcontrolstate', ({ state, matchesCount }) => pdfCount(tab, matchesCount, state));
  await loadPdf(tab, version, true);
}

async function loadPdf(tab, version, first = false) {
  const { lib, worker } = await loadPdfjs();
  const task = lib.getDocument({ // PDF.js 6: a document is freed through its loading task
    url: fileUrl(tab.tid, tab.path, version), worker,
    cMapUrl: `${PDFJS}/cmaps/`, standardFontDataUrl: `${PDFJS}/standard_fonts/`, wasmUrl: `${PDFJS}/wasm/`, iccUrl: `${PDFJS}/iccs/`,
  });
  let doc;
  try {
    doc = await task.promise;
  } catch (e) {
    task.destroy().catch(() => {});
    if (first) { tab.error = `Cannot show this PDF: ${e.message}`; renderFiles(); }
    return; // half written: the old version stays on screen
  }
  if (!tab.el.isConnected) { task.destroy().catch(() => {}); return; } // the tab was closed meanwhile
  const keep = tab.doc && { scale: tab.viewer.currentScaleValue, loc: tab.loc };
  tab.eventBus.on('pagesinit', () => {
    tab.viewer.currentScaleValue = keep ? keep.scale : 'page-width';
    if (keep && keep.loc) pdfGoTo(tab, keep.loc); // hidden: the ResizeObserver does it when it shows
    if (tab.tools.querySelector('.pdf-find').value) pdfFind(tab, ''); // the matches of the new version
  }, { once: true });
  const old = tab.task;
  Object.assign(tab, { doc, task });
  tab.viewer.setDocument(doc);
  tab.link.setDocument(doc, null);
  if (old) old.destroy().catch(() => {});
  Object.assign(tab, { version, size: sizeOf(version), gone: false, error: null });
  if (inView(tab.tid)) renderFiles();
}

function pdfGoTo(tab, loc) {
  const pageNumber = Math.min(loc.pageNumber, tab.viewer.pagesCount);
  tab.viewer.scrollPageIntoView({ pageNumber, destArray: [null, { name: 'XYZ' }, loc.left, loc.top, null], allowNegativeOffset: true });
}

function pdfTools(tab) {
  const v = tab.viewer;
  if (!v || !v.pdfDocument) return;
  const page = tab.tools.querySelector('.pdf-page');
  if (document.activeElement !== page) page.value = v.currentPageNumber;
  tab.tools.querySelector('.pdf-pages').textContent = `/ ${v.pagesCount}`;
  const sel = tab.tools.querySelector('.pdf-scale');
  const custom = sel.querySelector('option[value="custom"]');
  const preset = PDF_SCALES.some(([val]) => val === String(v.currentScaleValue) && val !== 'custom');
  custom.textContent = `${Math.round(v.currentScale * 100)}%`;
  custom.hidden = preset;
  sel.value = preset ? String(v.currentScaleValue) : 'custom';
}

function pdfFind(tab, type, previous = false) {
  if (!tab.eventBus) return;
  const query = tab.tools.querySelector('.pdf-find').value;
  tab.eventBus.dispatch('find', { source: tab, type, query, caseSensitive: false, entireWord: false,
    highlightAll: true, findPrevious: previous, matchDiacritics: false });
  if (!query) pdfCount(tab, null);
}

function pdfCount(tab, counts, state) {
  const query = tab.tools.querySelector('.pdf-find').value;
  const el = tab.tools.querySelector('.pdf-count');
  if (counts && counts.total) el.textContent = `${counts.current} / ${counts.total}`;
  else if (query && state === 1) el.textContent = 'not found'; // FindState.NOT_FOUND
  else if (!query) el.textContent = '';
}

function reloadViewer(tab, version) {
  if (tab.kind === 'image') reloadImage(tab, version);
  else if (tab.ready) tab.ready.then(() => loadPdf(tab, version));
}

function focusTab(tab) {
  if (tab.focus) { tab.focus(); return; } // a browser tab: its native page (browser.js)
  if (tab.shell) { tab.term.term.focus(); return; }
  (tab.view || tab.container || tab.el).focus();
}

// A file already open, in any pane, shows there. Else it opens in `pane`, or in the pane that I
// used last (openPane). `show` false: only added (a restore), not put on screen. `external`: a
// file that 3a cannot show opens in its default app at once (a double-click in a Finder tab).
// A text file open in another task of the project shows that tab's text (peerTab), not the disk's.
// tid: the view where it opens (a task, or a project's tabs). With no task selected, a new tab is a
// project tab (ownerFor); a restore adds it to `tid` itself. A file that is a project tab already
// shows that tab.
async function openFile(tid, path, focus = true, pane = null, show = true, external = false) {
  const w = work(tid);
  const find = () => viewTabs(w).find((t) => t.path === path && !t.thread);
  let tab = find();
  if (!tab) {
    const owner = w.restoring ? tid : ownerFor(tid);
    let r;
    try { r = await request('fs.read', { id: owner, path }); } catch (e) { toast(e.message); return; }
    if ((r.binary || r.tooLarge) && external) { send('fs.open', { id: owner, path }); return; }
    if (r.binary || r.tooLarge) {
      toast(`${path}: ${r.binary ? 'not a text file' : 'too large for the editor'}.`,
        { label: 'Open in default app', run: () => send('fs.open', { id: owner, path }) });
      return;
    }
    tab = find(); // opened meanwhile by another click
    if (!tab) {
      tab = r.kind === 'image' ? makeImageTab(owner, path, r) : r.kind === 'pdf' ? makePdfTab(owner, path, r) : makeTab(owner, path, r, peerTab(owner, path));
      addTab(work(owner), tab, pane || openPane(w));
      if (isProjectWs(owner)) dropDuplicates(tab);
      saveOpenFiles(owner);
    }
  }
  if (show) showTab(tab, focus);
}

// Cmd+N F: an empty text tab, "Untitled-1". The first save asks where to save it (saveAs).
// It is not kept in the task file: it has no path.
function newUntitled(tid, pane) {
  const n = ++G.untitled;
  const tab = makeTab(tid, `untitled:${n}`, { text: '', version: null, head: null });
  Object.assign(tab, { untitled: true, name: `Untitled-${n}` });
  addTab(work(tid), tab, pane);
  showTab(tab, true);
}

// Cmd+click on a file link in a terminal (browser.js): the backend resolves the target
// against the task's cwd (fs.link), then the file opens in 3a, at its line if it has one.
async function openLink(tid, target) {
  let r;
  try { r = await request('fs.link', { id: tid, target }); } catch (e) { toast(e.message); return; }
  await openFile(tid, r.path);
  if (r.line) goToLine(viewTabs(work(tid)).find((t) => t.path === r.path), r.line);
}

function goToLine(tab, n) {
  if (!tab || !tab.view || tab.kind === 'thread') return; // an image or a PDF has no lines
  const doc = tab.view.state.doc;
  const line = doc.line(Math.min(Math.max(n, 1), doc.lines));
  tab.view.dispatch({ selection: { anchor: line.from }, effects: CM.EditorView.scrollIntoView(line.from, { y: 'center' }) });
}

function saveOpenFiles(tid) { // thread tabs are not kept: they are one click away in 1b. Browser tabs: browser.js
  send('files.set', { id: tid, paths: work(tid).tabs.filter(isFileTab).map((t) => t.path) }); // a project's: project.json
}

// ------------------------------------------------------------------ thread tabs (1b --> 3a)

function threadDoc(th) {
  const parts = [`# Thread ${th.n} · ${th.kind} · ${th.ts.replace('T', ' ').slice(0, 16)}`];
  if (th.digest) parts.push('## Digest', th.digest);
  if (th.question) parts.push('## Question', th.question);
  parts.push('## Prompt', th.prompt || '(no prompt: the turn started on its own)');
  if (th.reply) parts.push('## Reply', th.reply);
  return parts.join('\n\n') + '\n';
}

async function openThread(tid, n) {
  const w = work(tid);
  const path = `thread:${n}`;
  let tab = w.tabs.find((t) => t.path === path);
  if (!tab) {
    let th;
    try { th = await request('thread.get', { id: tid, n }); } catch (e) { toast(e.message); return; }
    const el = document.createElement('div');
    el.className = 'editor';
    tab = { tid, path, el, kind: 'thread', thread: th, dirty: false, diskChanged: false, gone: false, diff: false };
    tab.view = new CM.EditorView({
      parent: el,
      state: CM.EditorState.create({
        doc: threadDoc(th),
        extensions: [
          CM.basicSetup, SELECT_ALL_KEYS, CM.oneDark, CM.languageFor('thread.md'), CM.EditorView.lineWrapping,
          CM.EditorState.readOnly.of(true),
          CM.EditorView.theme({ '&': { height: '100%' }, '.cm-scroller': { fontFamily: 'Menlo, Monaco, monospace', fontSize: '12.5px' } }),
        ],
      }),
    });
    const dup = w.tabs.find((t) => t.path === path); // opened meanwhile by another click
    if (dup) { tab.view.destroy(); tab = dup; } else addTab(w, tab, openPane(w));
  }
  showTab(tab);
}

// Scroll 4a to where the thread's prompt appears in the conversation. A short snippet:
// a long one may be split by the wrapping of the terminal.
function findInConversation(tid, th) {
  const t = S.terms.get(tid);
  const first = (th.prompt || '').trim().split('\n')[0].trim();
  if (!t || !first) { toast('Nothing to find: this thread has no prompt.'); return; }
  const snippet = first.slice(0, 30);
  t.term.clearSelection();
  t.term.scrollToBottom();
  if (!t.search.findPrevious(snippet, { caseSensitive: true })) {
    toast('Not found in the scrollback (older than the session shown, or wrapped differently).');
  }
}

// The tabs of the task's last session, each in its pane (the grid that the window remembers,
// grid.js), after its project's tabs (a file that is one of them is not opened twice). Also the
// project's tabs alone (`project:<path>`). The terminals come from the backend, which starts them
// at the select (shells.js). Once per run; the promise of the first call.
function restoreFiles(tid) {
  const w = work(tid);
  const data = ownerData(tid);
  if (w.restoreDone || !data) return w.restoreDone || Promise.resolve();
  w.restored = w.restoring = true;
  w.restoreDone = (async () => {
    const g = w.grid = w.grid || loadGrid(tid);
    w.want = { ...g.act };
    try {
      if (!isProjectWs(tid) && wsProject(tid)) await restoreFiles(projectWs(wsProject(tid)));
      for (const path of data.openFiles || []) await openFile(tid, path, false, g.at[gridKey(tid, path)] || 1, false);
      await restoreBrowserTabs(tid, g); // browser.js
      await restoreFinderTabs(tid, g); // finder.js
    } finally {
      w.restoring = false;
    }
    if (PANES.includes(g.pane) && paneTabs(w, g.pane).length) usePane(w, g.pane); // its tabs may not have come (a page not saved)
    applyWant(w);
    for (const n of Object.keys(w.want)) if (!w.want[n].includes('shell#')) delete w.want[n]; // gone since
    if (inView(tid)) { renderFiles(); renderExplorer(); }
  })();
  return w.restoreDone;
}

// A new task starts with the tabs of the previous one (newTask in app.js). The backend copied
// the lists (files, browser URLs, terminals); here, the view: the open folders of 2a and the grid
// (each tab's pane, the tab on screen in each). A thread tab is not copied (the new task has none).
function copyView(from, to) {
  copyGrid(from, to); // grid.js
  const src = S.work && S.work[from];
  if (!src) return;
  const w = work(to);
  for (const p of src.open) w.open.add(p);
}

// × or Cmd+W. A terminal ends its shell; the tab goes when the state no longer lists it.
// ask false: the caller has asked already (closePane, grid.js). Unsaved edits that a tab of
// another task holds too are not lost: no question.
// tid: the view, or the tab's owner. A project tab closes in every task of the project.
function closeFile(tid, path, ask = true) {
  const tab = viewTabs(work(tid)).find((t) => t.path === path);
  if (!tab) return;
  if (ask && losesEdits(tab) && !confirm(`Discard the unsaved changes to ${tab.untitled ? tab.name : path}?`)) return;
  if (tab.shell) {
    leavePane(tab);
    tab.closing = true;
    send('shell.close', { key: tab.key });
  } else {
    destroyTab(work(tab.tid), tab);
  }
  if (isFileTab(tab)) saveOpenFiles(tab.tid);
  if (tab.finder) saveFinderTabs(tab.tid);
  saveGrid(tid);
  if (tab.tid !== tid) saveGrid(tab.tid);
  renderFiles();
  renderExplorer();
}

function destroyTab(w, tab) { // any tab but a terminal; w: its owner
  if (tab.view) tab.view.destroy();
  if (tab.task) tab.task.destroy().catch(() => {});
  if (tab.close) tab.close(); // a browser tab: its native page (browser.js)
  dropTab(w, tab);
}

// An untitled tab: the save panel of macOS, in the project folder. A file outside the project
// is refused (3a shows files of the project only); the tab stays untitled. The panel asks before
// it replaces a file, so the write is forced; a tab of that file is replaced by this one.
async function saveAs(tab) {
  const w = viewWork(tab.tid);
  let r;
  try {
    const where = await request('fs.saveas', { id: tab.tid, name: tab.name });
    if (!where) return; // cancelled
    r = await request('fs.write', { id: tab.tid, path: where.path, text: tab.view.state.sliceDoc(), base: null, force: true });
    r = { ...r, ...(await request('fs.read', { id: tab.tid, path: where.path })) }; // its HEAD, for Diff
  } catch (e) { toast(e.message); return; }
  const old = viewTabs(w).find((t) => t.path === r.path && t !== tab);
  if (old) { if (old.view) old.view.destroy(); if (old.task) old.task.destroy().catch(() => {}); dropTab(work(old.tid), old); }
  for (const v of viewsOf(tab)) for (const n of PANES) if (v.act[n] === tab.path) v.act[n] = r.path;
  const peer = peerTab(tab.tid, r.path, tab); // the file is open in another task: its tabs take this text
  if (peer) {
    tab.file = peer.file;
    replaceText(sameText(tab).filter((t) => t !== tab), tab.view.state.doc);
  }
  Object.assign(tab, { path: r.path, untitled: false, name: null, version: r.version, head: r.head, dirty: false, diskChanged: false, gone: false });
  tab.view.dispatch({ effects: tab.langC.reconfigure(CM.languageFor(r.path)) });
  if (tab.wrap) store(wrapKey(tab.tid, r.path), true);
  saveOpenFiles(tab.tid);
  saveGrid(tab.tid);
  refreshTree(tab.tid);
  renderFiles();
  renderExplorer();
}

async function saveTab(tab, force = false) {
  if (tab.untitled) return saveAs(tab);
  const text = tab.view.state.sliceDoc(); // joined with the file's own line separator
  let r;
  try { r = await request('fs.write', { id: tab.tid, path: tab.path, text, base: tab.version, force }); } catch (e) { toast(e.message); return; }
  if (r.conflict) {
    tab.diskChanged = true;
    toast(`${tab.path} changed on disk since you opened it: reload it, or overwrite it.`);
  } else {
    tab.version = r.version;
    tab.dirty = false;
    tab.diskChanged = false;
    tab.gone = false;
  }
  renderFiles();
}

// The disk's text, in every tab of the file (sameText). saveTab needs no such loop: the state
// that it sets is shared.
async function reloadTab(tab) {
  let r;
  try { r = await request('fs.read', { id: tab.tid, path: tab.path }); } catch (e) { toast(e.message); return; }
  if (r.text === undefined) { toast(`${tab.path} can no longer be shown.`); return; }
  const tabs = sameText(tab);
  replaceText(tabs, r.text);
  Object.assign(tab, { version: r.version, head: r.head, dirty: false, diskChanged: false, gone: false });
  for (const t of tabs) if (t.diff) setDiff(t, true); // against the new HEAD
  renderFiles();
}

function setDiff(tab, on) {
  tab.diff = Boolean(on && typeof tab.head === 'string');
  const ext = tab.diff ? CM.unifiedMergeView({ original: tab.head, mergeControls: false, gutter: true }) : [];
  tab.view.dispatch({ effects: tab.diffC.reconfigure(ext) });
  renderFiles();
}

// Wrap belongs to the file, as VS Code's Option+Z: every tab of the file follows, in every task of
// the project (they share its files), and it is remembered by the window (localStorage).
function wrapKey(tid, path) {
  return `wrap:${wsProject(tid) || ''}:${path}`;
}

function setWrap(tab, on) {
  const key = wrapKey(tab.tid, tab.path);
  if (on) store(key, true); else try { localStorage.removeItem(key); } catch { /* not remembered */ }
  for (const ws of Object.values(S.work || {})) {
    for (const t of ws.tabs) {
      if (t.kind !== 'text' || !t.wrapC || wrapKey(t.tid, t.path) !== key) continue;
      t.wrap = on;
      t.view.dispatch({ effects: t.wrapC.reconfigure(on ? CM.EditorView.lineWrapping : []) });
    }
  }
  renderFiles();
}

// the tab on screen in the pane that I used last: Cmd+L, Cmd+W, Cmd+F act on it
function activeTab() {
  const w = wsId() && work(wsId());
  return w ? paneActive(w, w.pane) : null;
}

function renderFileTabs() {
  const tid = wsId();
  const w = tid && work(tid);
  for (const n of PANES) {
    const tabs = w ? paneTabs(w, n) : [];
    G.els[n].tabs.innerHTML = tabs.map((t) => {
      // a terminal: the folder where its shell is now (bash tells it, shell_init.bash), else where it started
      const shell = t.shell && shellsOf(t.tid).find((s) => s.key === t.key);
      const title = t.web ? t.url : t.shell ? `Terminal in ${shell && shell.cwd ? shell.cwd : 'the task folder'}` : t.untitled ? 'Not saved yet' : t.finder ? finderTitle(t) : t.path;
      const proj = isProjectWs(t.tid); // a project tab: a small grey star (Renaud picked it from a mockup, 2026-11-11)
      return `<div class="tab ${t.path === w.act[n] ? 'on' : ''} ${t.gone ? 'gone' : ''}" data-file="${esc(t.path)}" title="${esc(title)}${proj ? ' (project tab: in every task of the project)' : ''}">`
        + `${proj ? '<span class="star">★</span>' : ''}${t.finder ? `<img class="tab-icon" src="${fdIcon('folder')}" alt="" draggable="false">` : ''}${esc(tabName(t))}${t.dirty ? '<span class="dirty">●</span>' : ''}<span class="x" data-close-file="${esc(t.path)}">×</span></div>`;
    }).join('') + (tabs.length ? PANE_X : '');
  }
}

// at the right end of a pane's tab bar: closes all its tabs (closePane, grid.js)
const PANE_X = '<button class="pane-x" data-close-pane title="Close all tabs in this pane">'
  + '<svg viewBox="0 0 16 16" width="12" height="12" stroke="currentColor" stroke-width="1.4" aria-hidden="true">'
  + '<path d="M4 4l8 8M12 4l-8 8"/></svg></button>';

// The bar is rebuilt only when its content changes: render() runs at every state push, and a
// rebuild would take the focus out of the PDF's find field. `tools` (a PDF's, a page's) takes the
// place of the bar-slot element. One bar per pane.
function setBar(n, html, tools = null) {
  const bar = G.els[n].bar;
  if (bar.html === html && bar.tools === tools) return;
  bar.innerHTML = html;
  if (tools) bar.querySelector('.bar-slot').replaceWith(tools);
  Object.assign(bar, { html, tools });
}

function renderFiles() {
  const tid = wsId();
  const w = tid && work(tid);
  if (w) {
    applyWant(w);
    for (const n of PANES) { // no tab on screen in a pane that has tabs: its last one
      const tabs = paneTabs(w, n);
      if (tabs.length && !paneActive(w, n)) w.act[n] = tabs[tabs.length - 1].path;
    }
  }
  const shown = new Set(w ? PANES.map((n) => paneActive(w, n)) : []); // the view's: a task's tabs and its project's
  for (const ws of Object.values(S.work || {})) {
    for (const t of ws.tabs) t.el.style.display = shown.has(t) && !t.closing ? '' : 'none';
  }
  layoutPanes(w); // grid.js
  renderFileTabs();
  syncSelection(); // the tab on screen, the task or its session may have changed
  for (const n of PANES) {
    const tab = w && paneActive(w, n);
    renderBar(n, tab);
    if (tab && tab.shell) fitPaneShell(n);
  }
}

function renderBar(n, tab) {
  const setBarN = (html, tools) => setBar(n, html, tools);
  if (!tab || tab.shell || tab.finder) return setBarN(''); // a Finder tab has its own bars (finder.js)
  if (tab.thread) {
    return setBarN(`<span class="path">Thread ${tab.thread.n} of this task (read-only)</span>
      <button data-file-act="find" title="Scroll the conversation (4a) to this thread's prompt">Find in conversation</button>`);
  }
  if (tab.web) return setBarN('<span class="bar-slot"></span>', tab.tools); // browser.js keeps its toolbar up to date
  const notes = [];
  if (tab.gone) notes.push('<span class="note">Deleted on disk.</span>');
  if (tab.error) notes.push(`<span class="note">${esc(tab.error)}</span>`);
  const external = '<button data-file-act="external" title="Open the file with its default app of macOS">Open in default app</button>';
  if (tab.kind === 'image') {
    const meta = [tab.dims, fmtBytes(tab.size)].filter(Boolean).join(' · ');
    return setBarN(`<span class="path">${esc(tab.path)}</span>${notes.join('')}<span class="meta">${meta}</span>
      <button data-file-act="fit" class="${tab.fit ? '' : 'on'}" title="Show the image at its size (a click on the image does the same)">100%</button>${external}`);
  }
  if (tab.kind === 'pdf') {
    return setBarN(`<span class="path">${esc(tab.path)}</span>${notes.join('')}<span class="meta">${fmtBytes(tab.size)}</span>
      <span class="bar-slot"></span>${external}`, tab.tools);
  }
  if (tab.diskChanged) {
    notes.push('<span class="note">Changed on disk.</span><button data-file-act="reload">Reload (drop my edits)</button>'
      + '<button data-file-act="overwrite">Overwrite with mine</button>');
  }
  const canDiff = typeof tab.head === 'string';
  setBarN(`<span class="path">${tab.untitled ? `${esc(tab.name)} (not saved yet)` : esc(tab.path)}</span>${notes.join('')}
    <button data-file-act="wrap" class="${tab.wrap ? 'on' : ''}" title="Wrap long lines (this file, in every task)">Wrap</button>
    <button data-file-act="diff" class="${tab.diff ? 'on' : ''}" ${canDiff ? '' : 'disabled'}
      title="${canDiff ? 'Show the changes against git HEAD' : 'Not in git HEAD (new, untracked, or no repository)'}">Diff vs HEAD</button>
    <button data-file-act="save" ${tab.dirty || tab.diskChanged || tab.untitled ? '' : 'disabled'} title="Cmd+S${tab.untitled ? ': asks where to save it' : ''}">Save</button>
    ${!tab.untitled && tab.path.endsWith('.py') ? `<button data-file-act="run" class="run" title="Run Python file: python ${esc(tab.path)}, in a new terminal of pane 3 at the root of the project (saves it first)">${PLAY_SVG}</button>` : ''}`);
}

const PLAY_SVG = '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M4.5 2.5v11l9-5.5z" fill="currentColor"/></svg>';

setInterval(async () => {
  const tid = wsId();
  const w = tid && S.mode === 'dev' && S.work && S.work[tid];
  const fileTabs = w ? viewTabs(w).filter(isFileTab) : [];
  if (!fileTabs.length) return;
  let v;
  try { v = await request('fs.versions', { id: tid, paths: fileTabs.map((t) => t.path) }); } catch { return; }
  for (const tab of fileTabs) {
    const now = v[tab.path];
    if (now === undefined) continue;
    if (now === tab.version) { if (tab.gone) { tab.gone = false; renderFiles(); } continue; } // back as it was
    if (now === null) { if (!tab.gone) { tab.gone = true; renderFiles(); } continue; }
    if (tab.kind === 'image' || tab.kind === 'pdf') { // no edits in a viewer: it follows the disk
      if (now !== tab.tried) { tab.tried = now; reloadViewer(tab, now); } // tried once: a broken file is not fetched every 2 s
      continue;
    }
    if (!tab.dirty) await reloadTab(tab); // the agent changed it, and I have no edits: follow
    else if (!tab.diskChanged) { tab.diskChanged = true; renderFiles(); }
  }
}, POLL_MS);

// ------------------------------------------------------------------ selection as context (DESIGN_DETAILS.md)
// As in VS Code, without a key: the selection of the active file tab goes with my next prompt
// to the selected task. The backend keeps it in ~/.brain_fry/selection.json; the hook prints it
// at UserPromptSubmit, and Claude Code adds that to the model's context. Only for a session that
// runs in the app, and only while 3a is on screen: a selection that I cannot see is not sent.

const SEL_MAX = 10000; // characters sent; the backend cuts there too and the hook says so
const SEL_WAIT_MS = 120; // a drag selects many times per second
let selSent; // undefined: the first sync always sends (a reloaded page clears a stale selection)
let selTimer = null;

// The text tab on screen in the pane that I used last among those that show one: as VS Code's
// active editor, it stays the same while I type in a terminal.
function editorTab() {
  const w = wsId() && S.work && S.work[wsId()];
  if (!w) return null;
  for (const n of w.mru) {
    const t = paneActive(w, n);
    if (t && t.kind === 'text' && !t.untitled && t.view) return t;
  }
  return null;
}

function currentSelection() {
  const task = S.selected && S.state && S.state.tasks[S.selected];
  const tab = editorTab();
  if (!task || !task.pty || !tab) return null;
  const st = tab.view.state;
  const r = st.selection.main;
  if (r.empty) return null;
  const first = st.doc.lineAt(r.from).number;
  const end = st.doc.lineAt(r.to);
  const last = r.to === end.from && end.number > first ? end.number - 1 : end.number; // ends at column 0: not that line
  const text = st.sliceDoc(r.from, r.to);
  return { id: task.id, path: tab.path, from: first, to: last, text: text.slice(0, SEL_MAX), total: text.length, dirty: tab.dirty };
}

function syncSelection(now = false) {
  clearTimeout(selTimer);
  if (!now) { selTimer = setTimeout(() => syncSelection(true), SEL_WAIT_MS); return; }
  const sel = currentSelection();
  renderSelChip(sel);
  const key = JSON.stringify(sel);
  if (key === selSent) return;
  selSent = key;
  send('selection.set', sel || { id: null });
}

function renderSelChip(sel) {
  const el = $('#sel-chip');
  const n = sel ? sel.to - sel.from + 1 : 0;
  const html = sel ? `⧉ ${n} line${n === 1 ? '' : 's'} selected · ${esc(sel.path.split('/').pop())}${sel.dirty ? ' (unsaved)' : ''}`
    + '<span class="x" data-sel-clear title="Unselect">×</span>' : '';
  el.hidden = !sel;
  if (el.dataset.html === html) return;
  el.innerHTML = html;
  el.dataset.html = html;
  el.title = sel ? `${sel.path}, lines ${sel.from}-${sel.to}: goes with my next prompt to this task, as in VS Code` : '';
}

// ------------------------------------------------------------------ events

document.addEventListener('click', (e) => {
  if (e.target.closest('[data-sel-clear]')) {
    const tab = editorTab();
    if (tab && tab.view) tab.view.dispatch({ selection: { anchor: tab.view.state.selection.main.head } });
    return;
  }
  // As in VS Code: a click opens the file and leaves the keyboard in the explorer (Enter renames,
  // explorer.js); a double-click, handled as the second click (see DESIGN_DETAILS.md "Modes"), moves it to the file.
  if (e.target.closest('#tree .tree-rename')) return; // a click in the name field (explorer.js)
  const node = e.target.closest('#tree .node[data-node]');
  if (node && wsId()) { clickTreeNode(node.dataset.node, node.dataset.dir === '1', e.detail === 2); return; }
  const threadEl = e.target.closest('#threads .thread[data-n]');
  if (threadEl && S.selected) { openThread(S.selected, Number(threadEl.dataset.n)); return; }
  const close = e.target.closest('[data-close-file]');
  if (close && wsId()) { e.stopPropagation(); closeFile(wsId(), close.dataset.closeFile); return; }
  const closeAll = e.target.closest('#middle [data-close-pane]');
  if (closeAll) { closePane(paneOf(closeAll)); return; }
  const tabEl = e.target.closest('#middle .pane-tabs .tab[data-file]');
  if (tabEl && wsId()) {
    const t = viewTabs(work(wsId())).find((x) => x.path === tabEl.dataset.file);
    if (t && e.detail === 2) toggleProjectTab(t); // the second click (see DESIGN_DETAILS.md "Modes")
    else if (t) showTab(t, true);
    return;
  }
  const act = e.target.closest('[data-file-act]');
  const tab = act && wsId() && paneActive(work(wsId()), paneOf(act)); // the tab of that pane's bar
  if (act && tab && !act.disabled) {
    const a = act.dataset.fileAct;
    if (a === 'find') findInConversation(tab.tid, tab.thread);
    else if (a === 'diff') setDiff(tab, !tab.diff);
    else if (a === 'wrap') setWrap(tab, !tab.wrap);
    else if (a === 'run') runPython(tab);
    else if (a === 'save') saveTab(tab);
    else if (a === 'reload') reloadTab(tab);
    else if (a === 'overwrite') saveTab(tab, true);
    else if (a === 'fit') setFit(tab, !tab.fit);
    else if (a === 'external') send('fs.open', { id: tab.tid, path: tab.path });
    return;
  }
}, true);

// Cmd+F: the find of the tab on screen in the pane that I used last, wherever the keyboard is
// (the explorer, a tab bar, nothing focused): a PDF's find field, CodeMirror's find panel in a
// text or thread tab (asked by Renaud on 2026-11-11). Not from a text field nor a terminal
// (xterm.js's textarea): they keep their keys. A CodeMirror editor that has the keyboard opens
// its own panel. Browser pages: browser.js.
document.addEventListener('keydown', (e) => {
  if (!e.metaKey || e.key.toLowerCase() !== 'f' || S.mode !== 'dev' || S.picker) return;
  const tab = activeTab();
  if (!tab || !(tab.kind === 'pdf' || tab.view)) return;
  const a = document.activeElement;
  if (a && a.closest && a.closest('.cm-editor')) return;
  const pdfField = tab.kind === 'pdf' && tab.tools.contains(a);
  if (!pdfField && a && a.matches && a.matches('input, select, textarea, [contenteditable]')) return;
  e.preventDefault();
  e.stopPropagation();
  if (tab.view) { CM.openSearchPanel(tab.view); return; }
  const f = tab.tools.querySelector('.pdf-find');
  f.focus();
  f.select();
}, true);

// Cmd+W: closes the tab on screen in the pane that I used last, of any kind, wherever the
// keyboard is, as the tab's × does (asked by Renaud on 2026-11-11). Never the window: pywebview's
// view closes it on a Cmd+W that the page leaves unhandled, so the key is always taken, also
// with nothing to close (no tab, Organize or Review mode, a picker open). The keyboard follows to
// the tab that takes the place, if it was in the closed tab or nowhere; not from 4a or 2a.
// After app.js's listener: a Cmd+N or Cmd+J chord that waits takes the key first.
document.addEventListener('keydown', (e) => {
  if (!e.metaKey || e.shiftKey || e.altKey || e.ctrlKey || e.key.toLowerCase() !== 'w') return;
  e.preventDefault();
  e.stopPropagation();
  const tab = S.mode === 'dev' && !S.picker ? activeTab() : null;
  if (!tab) return;
  const a = document.activeElement;
  const follow = !a || a === document.body || tab.el.contains(a);
  closeFile(tab.tid, tab.path);
  const next = follow && activeTab();
  if (next && next !== tab) focusTab(next);
}, true);
