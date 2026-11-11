'use strict';
// Cmd+N O: copy a tab of another task into the pane that I use (DESIGN_DETAILS.md, "2a, 3a, 3b").
// The list holds the other tasks of the selected task's project, the one that I opened last first,
// each with its files, pages and terminals, then the pages of the tasks of other projects. A tab
// that is already open in the selected task is not listed. Enter or a click on a tab copies it; on
// a task, all its tabs that the list shows. On the right, a preview of the tab under the mouse (else
// of the selected one), as it looks now in its task. Typing filters.
// What a copy is: a file opens as any file does, so it shows the other task's text, unsaved edits
// included (One text per file, DESIGN_DETAILS.md), a page loads its URL, a terminal is a new shell in the
// folder where the other one is now (shell.copy; bash tells its folder and its last command,
// shell_init.bash).
// Uses S, $, esc, request, toast, uiLog, wsId (app.js), work, openFile, fileUrl (files.js),
// openWebTab, webTabName (browser.js), shellBoxSize (shells.js), at call time.

const C = { el: null, open: false, tid: null, pane: 1, srcs: [], rows: [], sel: 0, hover: null, q: '',
  pv: null, pvKey: null, pvToken: 0, shots: new Map(), back: null };

const CT_ICONS = {
  text: '<svg class="ct-ico" viewBox="0 0 16 16" fill="none" stroke="#a9adb7" stroke-width="1.2"><path d="M4 1.5h5.2L12.5 4.8v9.7h-8.5z"/><path d="M9 1.5v3.5h3.5"/></svg>',
  image: '<svg class="ct-ico" viewBox="0 0 16 16" fill="none" stroke="#7fc8a0" stroke-width="1.2"><rect x="1.8" y="3" width="12.4" height="10" rx="1.2"/><path d="M2.5 11.5l3.5-3.6 2.6 2.6 1.8-1.8 3.2 3.2"/><circle cx="10.6" cy="5.9" r="1"/></svg>',
  pdf: '<svg class="ct-ico" viewBox="0 0 16 16" fill="none" stroke="#e5737a" stroke-width="1.2"><path d="M4 1.5h5.2L12.5 4.8v9.7h-8.5z"/><path d="M9 1.5v3.5h3.5"/><path d="M6 9h4.5M6 11.5h4.5"/></svg>',
  web: '<svg class="ct-ico" viewBox="0 0 16 16" fill="none" stroke="#6aa1ff" stroke-width="1.2"><circle cx="8" cy="8" r="6.3"/><path d="M1.7 8h12.6M8 1.7c-2.2 2.2-2.2 10.4 0 12.6M8 1.7c2.2 2.2 2.2 10.4 0 12.6"/></svg>',
  term: '<svg class="ct-ico" viewBox="0 0 16 16" fill="none" stroke="#d9b96a" stroke-width="1.2"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M4.3 6l2.3 2-2.3 2M8.4 10.4h3.3"/></svg>',
};
const CT_IMAGES = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'ico', 'tif', 'tiff', 'heic', 'heif']); // files.IMAGE_TYPES

// ------------------------------------------------------------------ what the list holds

function ctKind(path) {
  const ext = (path.split('/').pop().split('.').pop() || '').toLowerCase();
  return ext === 'pdf' ? 'pdf' : CT_IMAGES.has(ext) ? 'image' : 'text';
}

function ctHost(url) {
  try { return new URL(url).host || url; } catch { return url; }
}

// A terminal's folder as the list shows it: relative to the project ("./" is the project).
function ctFolder(project, cwd) {
  if (!cwd || cwd === project) return './';
  return cwd.startsWith(`${project}/`) ? `${cwd.slice(project.length + 1)}/` : cwd;
}

// The tabs of task t: those on screen in its panes if it was built since the app started, else
// what its task file keeps (files, URLs) and its shells.
function ctTabsOf(t) {
  const w = S.work && S.work[t.id];
  const shellOf = (key) => (t.shells || []).find((s) => s.key === key) || {};
  if (w && w.restored) {
    return w.tabs.filter((x) => !x.closing && !x.idle && !x.thread && !x.untitled && !x.finder).map((x) => { // Finder tabs: not copied
      if (x.web) return x.url && x.url !== 'about:blank' ? { kind: 'web', name: webTabName(x), right: x.url, url: x.url, live: x } : null;
      if (x.shell) {
        const s = shellOf(x.key);
        return { kind: 'term', name: s.cmd || '', right: ctFolder(t.projectPath, s.cwd), key: x.key, live: x };
      }
      return { kind: x.kind === 'image' || x.kind === 'pdf' ? x.kind : 'text', name: x.path.split('/').pop(), right: x.path, path: x.path, live: x };
    }).filter(Boolean);
  }
  const out = (t.openFiles || []).map((p) => ({ kind: ctKind(p), name: p.split('/').pop(), right: p, path: p }));
  for (const u of t.browserTabs || []) out.push({ kind: 'web', name: ctHost(u), right: u, url: u });
  if ((t.shells || []).length) {
    for (const s of t.shells) out.push({ kind: 'term', name: s.cmd || '', right: ctFolder(t.projectPath, s.cwd), key: s.key });
  } else {
    for (let i = 0; i < (t.terminalTabs || 0); i++) out.push({ kind: 'term', name: '', right: './' }); // no shell runs: a new one
  }
  return out;
}

// [{task, other, tabs}]: the other tasks of my project, then the tasks of other projects (pages only).
function ctSources(tid) {
  const st = S.state;
  const me = st.tasks[tid];
  const mine = work(tid).tabs.filter((x) => !x.closing);
  const files = new Set(mine.filter((x) => isFileTab(x)).map((x) => x.path));
  const urls = new Set(mine.filter((x) => x.web).map((x) => x.url));
  const when = (t) => Date.parse(t.opened || t.updated || '') || 0;
  const tasks = Object.values(st.tasks).filter((t) => t.id !== tid).sort((a, b) => when(b) - when(a));
  const out = [];
  for (const other of [false, true]) {
    for (const t of tasks) {
      if ((t.projectPath !== me.projectPath) !== other) continue;
      const tabs = ctTabsOf(t).filter((x) => (x.kind === 'web' ? !urls.has(x.url) : other ? false : x.kind === 'term' || !files.has(x.path)));
      if (tabs.length) out.push({ task: t, other, tabs });
    }
  }
  return out;
}

const ctTitle = (src) => (src.other ? `${src.task.project} / ${src.task.title}` : src.task.title);
const ctText = (x) => `${x.kind === 'term' && !x.name ? 'terminal' : x.name} ${x.right}`.toLowerCase();

// The rows on screen: a task (its tabs shown under it), then its tabs. A task whose title matches
// the filter shows all its tabs; else only the tabs that match.
function ctRows() {
  const q = C.q;
  const rows = [];
  for (const src of C.srcs) {
    const tabs = !q || ctTitle(src).toLowerCase().includes(q) ? src.tabs : src.tabs.filter((x) => ctText(x).includes(q));
    if (!tabs.length) continue;
    rows.push({ head: true, src, tabs });
    for (const tab of tabs) rows.push({ head: false, src, tab });
  }
  return rows;
}

// ------------------------------------------------------------------ open, draw, close

function openCopyList() {
  const tid = S.selected;
  if (S.mode !== 'dev' || !tid || wsId() !== tid || !S.state.tasks[tid]) { toast('Select a task first: tabs belong to a task.'); return; }
  const srcs = ctSources(tid);
  if (!srcs.length) { toast('Nothing to copy: the other tasks have no tab that is not open here.'); return; }
  const w = work(tid);
  Object.assign(C, { open: true, tid, pane: w.pane || 1, srcs, q: '', hover: null, pvKey: null, back: document.activeElement });
  C.shots.clear();
  C.rows = ctRows();
  C.sel = C.rows.length > 1 ? 1 : 0; // the first tab
  C.el.innerHTML = `<div class="box ct-box"><h3>Copy from another task <span class="hint">into pane ${C.pane}</span></h3>
    <div class="ct-cols"><div class="ct-left"><input class="ct-filter" placeholder="Type to filter" spellcheck="false" autocomplete="off">
    <div class="ct-list"></div></div><div class="ct-right"><div class="ct-pv"></div></div></div></div>`;
  C.el.hidden = false;
  const input = C.el.querySelector('.ct-filter');
  input.addEventListener('input', () => {
    C.q = input.value.trim().toLowerCase();
    C.rows = ctRows();
    C.sel = Math.min(C.rows.findIndex((r) => !r.head), C.rows.length - 1);
    if (C.sel < 0) C.sel = 0;
    C.hover = null;
    ctRender();
  });
  ctRender();
  input.focus();
  uiLog(`copy list: opened, ${srcs.length} task(s), ${srcs.reduce((n, s) => n + s.tabs.length, 0)} tab(s), into pane ${C.pane}`);
}

function closeCopyList(refocus = true) {
  if (!C.open) return;
  C.open = false;
  ctDropPreview();
  C.el.hidden = true;
  C.el.innerHTML = '';
  C.shots.clear();
  const back = C.back;
  C.back = null;
  if (refocus && back && back.isConnected && back !== document.body) back.focus();
}

function ctHl(s) {
  const q = C.q;
  const i = q ? s.toLowerCase().indexOf(q) : -1;
  return i < 0 ? esc(s) : `${esc(s.slice(0, i))}<mark>${esc(s.slice(i, i + q.length))}</mark>${esc(s.slice(i + q.length))}`;
}

function ctRender() {
  const list = C.el.querySelector('.ct-list');
  let html = '';
  let group = false;
  C.rows.forEach((r, i) => {
    const sel = i === C.sel;
    if (r.head && r.src.other && !group) { group = true; html += '<div class="ct-group">Other projects</div>'; }
    if (r.head) {
      html += `<div class="ct-row ct-head ${sel ? 'sel' : ''}" data-ct="${i}"><span class="ct-dot" style="background:var(--${r.src.task.status})"></span>
        <span class="ct-name">${esc(ctTitle(r.src))}</span><span class="ct-meta">${sel ? `copy all ${r.tabs.length}` : ''}</span>${sel ? '<kbd>Enter</kbd>' : ''}</div>`;
      return;
    }
    const x = r.tab;
    const name = x.kind === 'term' && !x.name ? '<span class="ct-name none">Terminal</span>'
      : `<span class="ct-name ${x.kind === 'term' ? 'cmd' : ''}">${ctHl(x.name)}</span>`;
    html += `<div class="ct-row ct-tab ${sel ? 'sel' : ''}" data-ct="${i}">${CT_ICONS[x.kind]}${name}
      <span class="ct-path">&lrm;${ctHl(x.right)}&lrm;</span>${sel ? '<kbd>Enter</kbd>' : ''}</div>`;
  });
  list.innerHTML = html;
  const s = list.querySelector('.ct-row.sel');
  if (s) s.scrollIntoView({ block: 'nearest' });
  ctPreview();
}

function ctMove(step) {
  const n = C.rows.length;
  if (!n) return;
  C.sel = (C.sel + step + n) % n;
  C.hover = null; // the keyboard: the preview follows the selection until the mouse moves
  ctRender();
}

// ------------------------------------------------------------------ copy

async function ctCopy(row) {
  const tid = C.tid, n = C.pane;
  closeCopyList(false);
  if (!row || wsId() !== tid || !S.state.tasks[tid]) { toast('The selected task changed: nothing copied.'); return; }
  const tabs = row.head ? row.tabs : [row.tab];
  const kinds = {};
  for (const x of tabs) kinds[x.kind] = (kinds[x.kind] || 0) + 1;
  uiLog(`copy: ${Object.entries(kinds).map(([k, c]) => `${c} ${k}`).join(', ')} from ${row.src.task.id.slice(0, 8)} into pane ${n}`);
  for (const [i, x] of tabs.entries()) {
    const focus = i === tabs.length - 1; // the last one shows and takes the keyboard
    if (x.kind === 'web') await openWebTab(tid, x.url, { pane: n, activate: focus });
    else if (x.kind === 'term') await ctCopyShell(tid, n, x.key, focus);
    else await openFile(tid, x.path, focus, n);
  }
}

// A new shell in pane n: in the folder of the shell `key` (shell.copy), or in the task's folder
// when no shell runs for that tab (a task not opened since the app started).
async function ctCopyShell(tid, n, key, focus) {
  const w = work(tid);
  const next = { pane: n, focus };
  w.nextShells.push(next); // shells.js puts it in pane n when the state lists it
  try {
    await request(key ? 'shell.copy' : 'shell.new', { id: tid, ...(key ? { from: key } : {}), ...shellBoxSize(n) });
  } catch (e) {
    const i = w.nextShells.indexOf(next);
    if (i >= 0) w.nextShells.splice(i, 1);
    toast(e.message);
  }
}

// ------------------------------------------------------------------ preview

function ctDropPreview() {
  C.pvToken++;
  if (C.pv) { C.pv.destroy(); C.pv = null; }
}

function ctPreview() {
  const i = C.hover !== null && C.rows[C.hover] ? C.hover : C.sel;
  const row = C.rows[i];
  const x = row && !row.head ? row.tab : null;
  const key = !row ? '' : row.head ? `h:${row.src.task.id}` : `${row.src.task.id}:${x.kind}:${x.path || x.url || x.key || ''}:${i}`;
  if (key === C.pvKey) return;
  C.pvKey = key;
  ctDropPreview();
  const token = C.pvToken;
  const pv = C.el.querySelector('.ct-pv');
  if (!row) { pv.innerHTML = ''; return; }
  if (row.head) {
    const t = row.src.task;
    pv.innerHTML = `<div class="ct-digest"><div class="ct-dh"><span class="ct-dot" style="background:var(--${t.status})"></span>${esc(ctTitle(row.src))}</div>
      <div class="ct-dt">${esc(t.digest || t.notes || '')}</div></div>`;
    return;
  }
  const name = x.kind === 'term' ? x.name || 'Terminal' : x.name;
  pv.innerHTML = `<div class="ct-bar"><span class="ct-bt">${CT_ICONS[x.kind]}<span>${esc(name)}</span></span>
    <span class="ct-bp">&lrm;${esc(x.right)}&lrm;</span></div><div class="ct-body"></div>`;
  const body = pv.querySelector('.ct-body');
  const none = () => {
    body.innerHTML = `<div class="ct-none"><div>${esc(name)}</div><div>${esc(x.right)}</div></div>`;
  };
  const fill = { text: ctPvText, image: ctPvImage, pdf: ctPvPdf, web: ctPvWeb, term: ctPvTerm }[x.kind];
  fill(body, row, token, none).catch(() => { if (token === C.pvToken) none(); });
}

// A text file: an editor that cannot be edited, at the place where the other task's tab is
// scrolled (its unsaved edits included); a tab not open since the start: the file from the top.
async function ctPvText(body, row, token, none) {
  const x = row.tab, live = x.live;
  let doc = live && live.view ? live.view.state.doc : null;
  if (!doc) {
    const r = await request('fs.read', { id: row.src.task.id, path: x.path });
    if (token !== C.pvToken) return;
    if (!r || r.binary || r.tooLarge || typeof r.text !== 'string') { none(); return; }
    doc = r.text;
  }
  C.pv = new CM.EditorView({
    parent: body,
    state: CM.EditorState.create({
      doc,
      extensions: [CM.basicSetup, CM.oneDark, CM.languageFor(x.path), CM.EditorState.readOnly.of(true), CM.EditorView.editable.of(false),
        live && live.wrap ? CM.EditorView.lineWrapping : [],
        CM.EditorView.theme({ '&': { height: '100%' }, '.cm-scroller': { fontFamily: 'Menlo, Monaco, monospace', fontSize: '11.5px' } })],
    }),
  });
  const top = live && live.ctTop ? Math.min(live.ctTop, C.pv.state.doc.length) : 0;
  const view = C.pv;
  // once the editor has its size: scrollIntoView at once would only bring the line into view
  if (top) requestAnimationFrame(() => { if (C.pv === view) view.scrollDOM.scrollTop = view.lineBlockAt(top).top + view.documentPadding.top; });
}

async function ctPvImage(body, row, token, none) {
  const x = row.tab;
  let version = x.live ? x.live.version : null;
  if (!x.live) {
    const r = await request('fs.read', { id: row.src.task.id, path: x.path });
    if (token !== C.pvToken) return;
    if (!r || r.kind !== 'image') { none(); return; }
    version = r.version;
  }
  body.innerHTML = `<div class="ct-img"><img alt="" src="${esc(fileUrl(row.src.task.id, x.path, version))}"></div>`;
  body.querySelector('img').onerror = none;
}

// A PDF: the page that the other tab shows, copied from its canvas. A PDF not open since the start
// has no canvas: its name only.
async function ctPvPdf(body, row, token, none) {
  const live = row.tab.live;
  const page = (live && live.loc && live.loc.pageNumber) || 1;
  const src = live && live.el.querySelector(`.page[data-page-number="${page}"] canvas`);
  if (!src || !src.width) { none(); return; }
  const c = document.createElement('canvas');
  c.width = src.width;
  c.height = src.height;
  c.getContext('2d').drawImage(src, 0, 0);
  body.innerHTML = '<div class="ct-img ct-pdf"></div>';
  body.firstChild.appendChild(c);
}

// A page: a snapshot of its native view (app/browser.py). A page that never loaded (its task was
// not opened since the start) has none: its URL only.
async function ctPvWeb(body, row, token, none) {
  none();
  const live = row.tab.live;
  if (!live) return;
  let url = C.shots.get(live.key);
  if (url === undefined) {
    try { url = await request('browser.snapshot', { tab: live.key }); } catch { url = null; }
    C.shots.set(live.key, url);
  }
  if (token !== C.pvToken || !url) return;
  body.innerHTML = `<div class="ct-shot"><img alt="" src="${esc(url)}"></div>`;
}

// A terminal: its screen as it is now, as text. No shell (its task was not opened since the
// start): its name and folder only.
async function ctPvTerm(body, row, token, none) {
  const t = row.tab.key && S.terms.get(row.tab.key);
  if (!t) { none(); return; }
  const term = t.term, b = term.buffer.active;
  const lines = [];
  for (let i = 0; i < term.rows; i++) {
    const l = b.getLine(b.viewportY + i);
    lines.push(l ? l.translateToString(true) : '');
  }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  body.innerHTML = `<pre class="ct-term">${esc(lines.join('\n'))}</pre>`;
}

// Where each text tab is scrolled: a hidden tab (another task's) reads 0, so it is noted while it scrolls.
document.addEventListener('scroll', (e) => {
  const el = e.target;
  if (!el.classList || !el.classList.contains('cm-scroller') || (C.el && C.el.contains(el))) return;
  const ed = el.closest('.editor');
  for (const w of Object.values(S.work || {})) {
    const tab = w.tabs.find((t) => t.el === ed && t.view);
    if (tab) { tab.ctTop = tab.view.lineBlockAtHeight(Math.max(0, el.scrollTop - tab.view.documentPadding.top)).from; return; }
  }
}, true);

// ------------------------------------------------------------------ keys and mouse
// Before app.js's listeners (window, capture): while the list is open, it takes the keys. Letters
// go to the filter; Cmd keys do nothing, except the editing keys of the filter.

window.addEventListener('keydown', (e) => {
  if (!C.open) return;
  const k = e.key;
  if (k === 'Escape') closeCopyList();
  else if (k === 'ArrowDown') ctMove(1);
  else if (k === 'ArrowUp') ctMove(-1);
  else if (k === 'Enter') ctCopy(C.rows[C.sel]);
  else if (e.metaKey || e.ctrlKey) { if (e.metaKey && !e.ctrlKey && ['a', 'c', 'v', 'x', 'z'].includes(k.toLowerCase())) return; }
  else return;
  e.preventDefault();
  e.stopImmediatePropagation();
}, true);

function ctSetup() {
  C.el = $('#copytab');
  C.el.addEventListener('mousemove', (e) => {
    const r = e.target.closest && e.target.closest('.ct-row');
    const i = r ? Number(r.dataset.ct) : null;
    if (i === C.hover) return;
    C.hover = i;
    ctPreview();
  });
  C.el.addEventListener('mousedown', (e) => { if (!e.target.closest('.ct-filter')) e.preventDefault(); }); // the filter keeps the keyboard
  C.el.addEventListener('click', (e) => {
    const r = e.target.closest('.ct-row');
    if (r) ctCopy(C.rows[Number(r.dataset.ct)]);
    else if (e.target === C.el) closeCopyList();
  });
}
ctSetup();
