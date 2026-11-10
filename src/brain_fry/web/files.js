'use strict';
// 2a (explorer) and 3a (file tabs: CodeMirror for text, viewers for images and PDFs). Everything
// belongs to the selected task: each task has its own tree state and its own open files,
// restored when it is selected. A tab's `kind`: text, image, pdf, or thread (1b).
// Uses S, $, esc, request, send, toast, store, stored from app.js, which loads first.

const POLL_MS = 2000; // checks the open files for changes on disk (the agent edits them too)

function work(tid) {
  S.work = S.work || {};
  if (!S.work[tid]) {
    S.work[tid] = { open: new Set(), kids: new Map(), loading: new Set(), tabs: [], active: null,
      restored: false, lastUpdated: null, shell: null, treeSel: null }; // treeSel: the explorer's selected entry
  }
  return S.work[tid];
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
  if (tid === S.selected) renderExplorer();
}

function refreshTree(tid) {
  const w = work(tid);
  for (const path of ['', ...w.open]) if (w.kids.has(path)) loadDir(tid, path);
}

function renderExplorer() {
  if (S.treeRename) return; // a redraw would take the name field away (explorer.js redraws after it)
  const tid = S.selected;
  const task = tid && S.state.tasks[tid];
  const el = $('#tree');
  if (!task) { el.innerHTML = '<div class="empty">No task selected.</div>'; return; }
  const w = work(tid);
  if (task.updated !== w.lastUpdated) { // an event arrived: the agent may have added files
    const first = w.lastUpdated === null;
    w.lastUpdated = task.updated;
    if (!first) refreshTree(tid);
  }
  if (!w.kids.has('')) { loadDir(tid, ''); el.innerHTML = '<div class="empty">Loading...</div>'; return; }
  const rows = [];
  const walk = (path, depth) => {
    for (const e of w.kids.get(path) || []) {
      const pad = 8 + depth * 14;
      const open = e.dir && w.open.has(e.path);
      const cls = ['node', e.dir ? 'dir' : 'file', !e.dir && e.path === w.active ? 'active' : '',
        e.path === w.treeSel ? 'sel' : '', isCut(task, e.path) ? 'cut' : ''].join(' ');
      rows.push(`<div class="${cls}" data-node="${esc(e.path)}" data-dir="${e.dir ? 1 : 0}" style="padding-left:${pad}px">`
        + `<span class="caret">${e.dir ? (open ? '▾' : '▸') : ''}</span><span class="name">${esc(e.name)}${e.dir ? '/' : ''}</span></div>`);
      if (open) {
        if (w.kids.has(e.path)) walk(e.path, depth + 1);
        else { loadDir(tid, e.path); rows.push(`<div class="node loading" style="padding-left:${pad + 14}px">...</div>`); }
      }
    }
  };
  walk('', 0);
  el.innerHTML = rows.join('') || '<div class="empty">Empty folder.</div>';
}

// ------------------------------------------------------------------ file tabs (3a)

function makeTab(tid, path, r) {
  const el = document.createElement('div');
  el.className = 'editor';
  $('#editors').appendChild(el);
  const tab = { tid, path, el, kind: 'text', version: r.version, head: r.head, dirty: false, diskChanged: false,
    gone: false, diff: false, diffC: new CM.Compartment(), langC: new CM.Compartment(), quiet: false,
    wrap: stored(wrapKey(tid, path), false), wrapC: new CM.Compartment() };
  tab.view = new CM.EditorView({
    parent: el,
    state: CM.EditorState.create({
      doc: r.text,
      extensions: [
        CM.basicSetup,
        CM.keymap.of([CM.indentWithTab, { key: 'Mod-s', run: () => { saveTab(tab); return true; } }]),
        CM.oneDark,
        tab.langC.of(CM.languageFor(path)), // a rename may change the language (explorer.js)
        tab.diffC.of([]),
        tab.wrapC.of(tab.wrap ? CM.EditorView.lineWrapping : []),
        r.text.includes('\r\n') ? CM.EditorState.lineSeparator.of('\r\n') : [], // save CRLF files as CRLF
        CM.EditorView.updateListener.of((u) => {
          if (u.docChanged && !tab.quiet && !tab.dirty) { tab.dirty = true; renderFileTabs(); }
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
  $('#editors').appendChild(el);
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
    if (tab.tid === S.selected) renderFiles();
  });
  tab.img.addEventListener('error', () => { tab.error = 'WebKit cannot show this image.'; if (tab.tid === S.selected) renderFiles(); });
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
  if (tab.tid === S.selected) renderFiles();
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
  (tab.view || tab.container || tab.el).focus();
}

async function openFile(tid, path, focus = true) {
  const w = work(tid);
  let tab = w.tabs.find((t) => t.path === path);
  if (!tab) {
    let r;
    try { r = await request('fs.read', { id: tid, path }); } catch (e) { toast(e.message); return; }
    if (r.binary || r.tooLarge) {
      toast(`${path}: ${r.binary ? 'not a text file' : 'too large for the editor'}.`,
        { label: 'Open in default app', run: () => send('fs.open', { id: tid, path }) });
      return;
    }
    tab = w.tabs.find((t) => t.path === path); // opened meanwhile by another click
    if (!tab) {
      tab = r.kind === 'image' ? makeImageTab(tid, path, r) : r.kind === 'pdf' ? makePdfTab(tid, path, r) : makeTab(tid, path, r);
      w.tabs.push(tab);
      saveOpenFiles(tid);
    }
  }
  w.active = path;
  store(`active:${tid}`, path);
  if (tid === S.selected) {
    renderFiles();
    renderExplorer();
    if (focus) focusTab(tab);
  }
}

// Cmd+click on a file link in a terminal (browser.js): the backend resolves the target
// against the task's cwd (fs.link), then the file opens in 3a, at its line if it has one.
async function openLink(tid, target) {
  let r;
  try { r = await request('fs.link', { id: tid, target }); } catch (e) { toast(e.message); return; }
  if (!S.layout.middle_on) toggleLayout('middle');
  await openFile(tid, r.path);
  if (r.line) goToLine(work(tid).tabs.find((t) => t.path === r.path), r.line);
}

function goToLine(tab, n) {
  if (!tab || !tab.view || tab.kind === 'thread') return; // an image or a PDF has no lines
  const doc = tab.view.state.doc;
  const line = doc.line(Math.min(Math.max(n, 1), doc.lines));
  tab.view.dispatch({ selection: { anchor: line.from }, effects: CM.EditorView.scrollIntoView(line.from, { y: 'center' }) });
}

function saveOpenFiles(tid) { // thread tabs are not kept: they are one click away in 1b. Browser tabs: browser.js
  send('files.set', { id: tid, paths: work(tid).tabs.filter((t) => !t.thread && !t.web).map((t) => t.path) });
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
    $('#editors').appendChild(el);
    tab = { tid, path, el, kind: 'thread', thread: th, dirty: false, diskChanged: false, gone: false, diff: false };
    tab.view = new CM.EditorView({
      parent: el,
      state: CM.EditorState.create({
        doc: threadDoc(th),
        extensions: [
          CM.basicSetup, CM.oneDark, CM.languageFor('thread.md'), CM.EditorView.lineWrapping,
          CM.EditorState.readOnly.of(true),
          CM.EditorView.theme({ '&': { height: '100%' }, '.cm-scroller': { fontFamily: 'Menlo, Monaco, monospace', fontSize: '12.5px' } }),
        ],
      }),
    });
    w.tabs.push(tab);
  }
  w.active = path;
  if (!S.layout.middle_on) toggleLayout('middle');
  if (tid === S.selected) renderFiles();
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

async function restoreFiles(tid) {
  const w = work(tid);
  const task = S.state.tasks[tid];
  if (w.restored || !task) return;
  w.restored = true;
  const active = w.activeHint || stored(`active:${tid}`, null); // before openFile overwrites it
  for (const path of task.openFiles || []) await openFile(tid, path, false);
  await restoreBrowserTabs(tid); // browser.js
  const web = /^web#(\d+)$/.exec(active || '');
  const tab = web ? w.tabs.filter((t) => t.web)[+web[1]] : w.tabs.find((t) => t.path === active);
  if (tab) { w.active = tab.path; store(`active:${tid}`, tab.path); }
  if (tid === S.selected) renderFiles();
}

// A new task starts with the tabs of the previous one (newTask in app.js). The backend copied
// the lists (files, browser URLs, terminals); here, the view: the open folders of 2a and the tab
// on screen. A browser tab is found by its position among the saved ones (restoreBrowserTabs
// opens them in that order): its key is new. A thread tab is not copied (the new task has none).
function copyView(from, to) {
  const src = S.work && S.work[from];
  if (!src) return; // not shown since the start: restoreFiles picks the tab, as for any task
  const w = work(to);
  for (const p of src.open) w.open.add(p);
  const tab = src.tabs.find((t) => t.path === src.active);
  const saved = src.tabs.filter((t) => t.web && t.url && t.url !== 'about:blank'); // as saveWebTabs
  if (tab && tab.web && saved.includes(tab)) w.activeHint = `web#${saved.indexOf(tab)}`;
  else if (tab && !tab.web && !tab.thread) w.activeHint = tab.path;
}

function closeFile(tid, path) {
  const w = work(tid);
  const tab = w.tabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.dirty && !confirm(`Discard the unsaved changes to ${path}?`)) return;
  if (tab.view) tab.view.destroy();
  if (tab.task) tab.task.destroy().catch(() => {});
  if (tab.close) tab.close(); // a browser tab: its native page (browser.js)
  tab.el.remove();
  const i = w.tabs.indexOf(tab);
  w.tabs.splice(i, 1);
  if (w.active === path) w.active = w.tabs.length ? w.tabs[Math.min(i, w.tabs.length - 1)].path : null;
  saveOpenFiles(tid);
  renderFiles();
  renderExplorer();
}

async function saveTab(tab, force = false) {
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

async function reloadTab(tab) {
  let r;
  try { r = await request('fs.read', { id: tab.tid, path: tab.path }); } catch (e) { toast(e.message); return; }
  if (r.text === undefined) { toast(`${tab.path} can no longer be shown.`); return; }
  const anchor = Math.min(tab.view.state.selection.main.anchor, r.text.length);
  tab.quiet = true;
  tab.view.dispatch({ changes: { from: 0, to: tab.view.state.doc.length, insert: r.text }, selection: { anchor } });
  tab.quiet = false;
  Object.assign(tab, { version: r.version, head: r.head, dirty: false, diskChanged: false, gone: false });
  if (tab.diff) setDiff(tab, true); // against the new HEAD
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
  const t = S.state && S.state.tasks[tid];
  return `wrap:${t ? t.projectPath : ''}:${path}`;
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

function activeTab() {
  const w = S.selected && work(S.selected);
  return w ? w.tabs.find((t) => t.path === w.active) : null;
}

function renderFileTabs() {
  const tid = S.selected;
  const w = tid && work(tid);
  $('#file-tabs').innerHTML = w ? w.tabs.map((t) => {
    const name = t.thread ? `Thread ${t.thread.n}` : t.web ? webTabName(t) : t.path.split('/').pop();
    return `<div class="tab ${t.path === w.active ? 'on' : ''} ${t.gone ? 'gone' : ''}" data-file="${esc(t.path)}" title="${esc(t.web ? t.url : t.path)}">`
      + `${esc(name)}${t.dirty ? '<span class="dirty">●</span>' : ''}<span class="x" data-close-file="${esc(t.path)}">×</span></div>`;
  }).join('') + '<div class="tab new-web" data-new-web title="New browser tab (Cmd+T)">+ Browser</div>' : '';
}

// The bar is rebuilt only when its content changes: render() runs at every state push, and a
// rebuild would take the focus out of the PDF's find field. `tools` (a PDF's) takes the place of
// the bar-slot element.
function setBar(html, tools = null) {
  const bar = $('#file-bar');
  if (bar.html === html && bar.tools === tools) return;
  bar.innerHTML = html;
  if (tools) bar.querySelector('.bar-slot').replaceWith(tools);
  Object.assign(bar, { html, tools });
}

function renderFiles() {
  const tid = S.selected;
  const w = tid && work(tid);
  for (const ws of Object.values(S.work || {})) {
    for (const t of ws.tabs) t.el.style.display = w && t.tid === tid && t.path === w.active ? '' : 'none';
  }
  renderFileTabs();
  syncSelection(); // the active tab, the task or its session may have changed
  const tab = activeTab();
  $('#file-empty').hidden = Boolean(tab);
  if (!tab) return setBar('');
  if (tab.thread) {
    return setBar(`<span class="path">Thread ${tab.thread.n} of this task (read-only)</span>
      <button data-file-act="find" title="Scroll the conversation (4a) to this thread's prompt">Find in conversation</button>`);
  }
  if (tab.web) return setBar('<span class="bar-slot"></span>', tab.tools); // browser.js keeps its toolbar up to date
  const notes = [];
  if (tab.gone) notes.push('<span class="note">Deleted on disk.</span>');
  if (tab.error) notes.push(`<span class="note">${esc(tab.error)}</span>`);
  const external = '<button data-file-act="external" title="Open the file with its default app of macOS">Open in default app</button>';
  if (tab.kind === 'image') {
    const meta = [tab.dims, fmtBytes(tab.size)].filter(Boolean).join(' · ');
    return setBar(`<span class="path">${esc(tab.path)}</span>${notes.join('')}<span class="meta">${meta}</span>
      <button data-file-act="fit" class="${tab.fit ? '' : 'on'}" title="Show the image at its size (a click on the image does the same)">100%</button>${external}`);
  }
  if (tab.kind === 'pdf') {
    return setBar(`<span class="path">${esc(tab.path)}</span>${notes.join('')}<span class="meta">${fmtBytes(tab.size)}</span>
      <span class="bar-slot"></span>${external}`, tab.tools);
  }
  if (tab.diskChanged) {
    notes.push('<span class="note">Changed on disk.</span><button data-file-act="reload">Reload (drop my edits)</button>'
      + '<button data-file-act="overwrite">Overwrite with mine</button>');
  }
  const canDiff = typeof tab.head === 'string';
  setBar(`<span class="path">${esc(tab.path)}</span>${notes.join('')}
    <button data-file-act="wrap" class="${tab.wrap ? 'on' : ''}" title="Wrap long lines (this file, in every task)">Wrap</button>
    <button data-file-act="diff" class="${tab.diff ? 'on' : ''}" ${canDiff ? '' : 'disabled'}
      title="${canDiff ? 'Show the changes against git HEAD' : 'Not in git HEAD (new, untracked, or no repository)'}">Diff vs HEAD</button>
    <button data-file-act="save" ${tab.dirty || tab.diskChanged ? '' : 'disabled'} title="Cmd+S">Save</button>`);
}

setInterval(async () => {
  const tid = S.selected;
  const w = tid && S.mode === 'dev' && S.work && S.work[tid];
  const fileTabs = w ? w.tabs.filter((t) => !t.thread && !t.web) : [];
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

function currentSelection() {
  const task = S.selected && S.state && S.state.tasks[S.selected];
  const tab = activeTab();
  if (!task || !task.pty || !S.layout.middle_on || !tab || tab.kind !== 'text' || !tab.view) return null;
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
    const tab = activeTab();
    if (tab && tab.view) tab.view.dispatch({ selection: { anchor: tab.view.state.selection.main.head } });
    return;
  }
  // As in VS Code: a click opens the file and leaves the keyboard in the explorer (Enter renames,
  // explorer.js); a double-click, handled as the second click (see DESIGN.md "Modes"), moves it to the file.
  if (e.target.closest('#tree .tree-rename')) return; // a click in the name field (explorer.js)
  const node = e.target.closest('#tree .node[data-node]');
  if (node && S.selected) {
    const w = work(S.selected);
    const path = node.dataset.node;
    w.treeSel = path;
    if (node.dataset.dir === '1') {
      if (e.detail === 2) return;
      if (w.open.has(path)) w.open.delete(path); else w.open.add(path);
      renderExplorer();
    } else if (e.detail === 2) {
      openFile(S.selected, path, true);
    } else {
      openFile(S.selected, path, false);
      renderExplorer();
    }
    return;
  }
  const threadEl = e.target.closest('#threads .thread[data-n]');
  if (threadEl && S.selected) { openThread(S.selected, Number(threadEl.dataset.n)); return; }
  const close = e.target.closest('[data-close-file]');
  if (close && S.selected) { e.stopPropagation(); closeFile(S.selected, close.dataset.closeFile); return; }
  const tabEl = e.target.closest('#file-tabs .tab[data-file]');
  if (tabEl && S.selected) { openFile(S.selected, tabEl.dataset.file); return; }
  const act = e.target.closest('[data-file-act]');
  const tab = activeTab();
  if (act && tab && !act.disabled) {
    const a = act.dataset.fileAct;
    if (a === 'find') findInConversation(tab.tid, tab.thread);
    else if (a === 'diff') setDiff(tab, !tab.diff);
    else if (a === 'wrap') setWrap(tab, !tab.wrap);
    else if (a === 'save') saveTab(tab);
    else if (a === 'reload') reloadTab(tab);
    else if (a === 'overwrite') saveTab(tab, true);
    else if (a === 'fit') setFit(tab, !tab.fit);
    else if (a === 'external') send('fs.open', { id: tab.tid, path: tab.path });
    return;
  }
}, true);

// Cmd+F in a PDF goes to its find field. Elsewhere it stays with its owner: CodeMirror has its
// own search, and a terminal does not get Cmd keys.
document.addEventListener('keydown', (e) => {
  if (!e.metaKey || e.key.toLowerCase() !== 'f' || S.mode !== 'dev') return;
  const tab = activeTab();
  const a = document.activeElement;
  if (!tab || tab.kind !== 'pdf' || !(a === document.body || tab.el.contains(a) || tab.tools.contains(a))) return;
  e.preventDefault();
  e.stopPropagation();
  const f = tab.tools.querySelector('.pdf-find');
  f.focus();
  f.select();
}, true);
