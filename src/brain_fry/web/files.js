'use strict';
// 2a (explorer) and 3a (file tabs, CodeMirror). Everything belongs to the selected task:
// each task has its own tree state and its own open files, restored when it is selected.
// Uses S, $, esc, request, send, toast, store, stored from app.js, which loads first.

const POLL_MS = 2000; // checks the open files for changes on disk (the agent edits them too)

function work(tid) {
  S.work = S.work || {};
  if (!S.work[tid]) {
    S.work[tid] = { open: new Set(), kids: new Map(), loading: new Set(), tabs: [], active: null,
      restored: false, lastUpdated: null, shell: null };
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
  const tid = S.selected;
  const task = tid && S.state.tasks[tid];
  const el = $('#tree');
  if (!task) { el.innerHTML = '<div class="empty">No task selected.</div>'; $('#explorer-root').textContent = ''; return; }
  $('#explorer-root').textContent = task.project;
  $('#explorer-root').title = task.projectPath;
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
      const cls = ['node', e.dir ? 'dir' : 'file', !e.dir && e.path === w.active ? 'active' : ''].join(' ');
      rows.push(`<div class="${cls}" data-node="${esc(e.path)}" data-dir="${e.dir ? 1 : 0}" style="padding-left:${pad}px">`
        + `<span class="caret">${e.dir ? (open ? '▾' : '▸') : ''}</span>${esc(e.name)}</div>`);
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
  const tab = { tid, path, el, version: r.version, head: r.head, dirty: false, diskChanged: false, gone: false,
    diff: false, diffC: new CM.Compartment(), quiet: false };
  tab.view = new CM.EditorView({
    parent: el,
    state: CM.EditorState.create({
      doc: r.text,
      extensions: [
        CM.basicSetup,
        CM.keymap.of([CM.indentWithTab, { key: 'Mod-s', run: () => { saveTab(tab); return true; } }]),
        CM.oneDark,
        CM.languageFor(path),
        tab.diffC.of([]),
        r.text.includes('\r\n') ? CM.EditorState.lineSeparator.of('\r\n') : [], // save CRLF files as CRLF
        CM.EditorView.updateListener.of((u) => {
          if (u.docChanged && !tab.quiet && !tab.dirty) { tab.dirty = true; renderFileTabs(); }
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

async function openFile(tid, path, focus = true) {
  const w = work(tid);
  let tab = w.tabs.find((t) => t.path === path);
  if (!tab) {
    let r;
    try { r = await request('fs.read', { id: tid, path }); } catch (e) { toast(e.message); return; }
    if (r.binary || r.tooLarge) { toast(`${path}: ${r.binary ? 'a binary file' : 'too large'}, not opened`); return; }
    tab = w.tabs.find((t) => t.path === path); // opened meanwhile by another click
    if (!tab) {
      tab = makeTab(tid, path, r);
      w.tabs.push(tab);
      saveOpenFiles(tid);
    }
  }
  w.active = path;
  store(`active:${tid}`, path);
  if (tid === S.selected) {
    renderFiles();
    renderExplorer();
    if (focus) tab.view.focus();
  }
}

function saveOpenFiles(tid) { // thread tabs are not kept: they are one click away in 1b
  send('files.set', { id: tid, paths: work(tid).tabs.filter((t) => !t.thread).map((t) => t.path) });
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
    tab = { tid, path, el, thread: th, dirty: false, diskChanged: false, gone: false, diff: false };
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
  for (const path of task.openFiles || []) await openFile(tid, path, false);
  const active = stored(`active:${tid}`, null);
  if (active && w.tabs.some((t) => t.path === active)) w.active = active;
  if (tid === S.selected) renderFiles();
}

function closeFile(tid, path) {
  const w = work(tid);
  const tab = w.tabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.dirty && !confirm(`Discard the unsaved changes to ${path}?`)) return;
  tab.view.destroy();
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

function activeTab() {
  const w = S.selected && work(S.selected);
  return w ? w.tabs.find((t) => t.path === w.active) : null;
}

function renderFileTabs() {
  const tid = S.selected;
  const w = tid && work(tid);
  $('#file-tabs').innerHTML = w ? w.tabs.map((t) => {
    const name = t.thread ? `Thread ${t.thread.n}` : t.path.split('/').pop();
    return `<div class="tab ${t.path === w.active ? 'on' : ''} ${t.gone ? 'gone' : ''}" data-file="${esc(t.path)}" title="${esc(t.path)}">`
      + `${esc(name)}${t.dirty ? '<span class="dirty">●</span>' : ''}<span class="x" data-close-file="${esc(t.path)}">×</span></div>`;
  }).join('') : '';
}

function renderFiles() {
  const tid = S.selected;
  const w = tid && work(tid);
  for (const ws of Object.values(S.work || {})) {
    for (const t of ws.tabs) t.el.style.display = w && t.tid === tid && t.path === w.active ? '' : 'none';
  }
  renderFileTabs();
  const tab = activeTab();
  $('#file-empty').hidden = Boolean(tab);
  const bar = $('#file-bar');
  if (!tab) { bar.innerHTML = ''; return; }
  if (tab.thread) {
    bar.innerHTML = `<span class="path">Thread ${tab.thread.n} of this task (read-only)</span>
      <button data-file-act="find" title="Scroll the conversation (4a) to this thread's prompt">Find in conversation</button>`;
    return;
  }
  const notes = [];
  if (tab.gone) notes.push('<span class="note">Deleted on disk.</span>');
  if (tab.diskChanged) {
    notes.push('<span class="note">Changed on disk.</span><button data-file-act="reload">Reload (drop my edits)</button>'
      + '<button data-file-act="overwrite">Overwrite with mine</button>');
  }
  const canDiff = typeof tab.head === 'string';
  bar.innerHTML = `<span class="path">${esc(tab.path)}</span>${notes.join('')}
    <button data-file-act="diff" class="${tab.diff ? 'on' : ''}" ${canDiff ? '' : 'disabled'}
      title="${canDiff ? 'Show the changes against git HEAD' : 'Not in git HEAD (new, untracked, or no repository)'}">Diff vs HEAD</button>
    <button data-file-act="save" ${tab.dirty || tab.diskChanged ? '' : 'disabled'} title="Cmd+S">Save</button>`;
}

setInterval(async () => {
  const tid = S.selected;
  const w = tid && S.mode === 'dev' && S.work && S.work[tid];
  const fileTabs = w ? w.tabs.filter((t) => !t.thread) : [];
  if (!fileTabs.length) return;
  let v;
  try { v = await request('fs.versions', { id: tid, paths: fileTabs.map((t) => t.path) }); } catch { return; }
  for (const tab of fileTabs) {
    const now = v[tab.path];
    if (now === undefined || now === tab.version) continue;
    if (now === null) { if (!tab.gone) { tab.gone = true; renderFiles(); } continue; }
    if (!tab.dirty) await reloadTab(tab); // the agent changed it, and I have no edits: follow
    else if (!tab.diskChanged) { tab.diskChanged = true; renderFiles(); }
  }
}, POLL_MS);

// ------------------------------------------------------------------ events

document.addEventListener('click', (e) => {
  const node = e.target.closest('#tree .node[data-node]');
  if (node && S.selected) {
    const w = work(S.selected);
    const path = node.dataset.node;
    if (node.dataset.dir === '1') {
      if (w.open.has(path)) w.open.delete(path); else w.open.add(path);
      renderExplorer();
    } else {
      openFile(S.selected, path);
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
    else if (a === 'save') saveTab(tab);
    else if (a === 'reload') reloadTab(tab);
    else if (a === 'overwrite') saveTab(tab, true);
    return;
  }
  if (e.target.closest('#tree-refresh') && S.selected) refreshTree(S.selected);
}, true);
