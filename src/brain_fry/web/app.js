'use strict';
// Brain Fry front end. The backend pushes the whole state on /ctl; commands go back on /ctl.
// Each task with a live session has its own xterm.js terminal, fed by /pty/<task id>.

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const KINDS = new Set(['review', 'answer', 'rule', 'FYI', 'blocked']);
const PRIOS = ['working', 'planning', 'later', 'more_later', 'never'];
const HIDDEN_PRIOS = new Set(['later', 'more_later', 'never']);
const OUR_KEYS = new Set(['arrowdown', 'arrowup', 'n', 'd', 'e', 'b', '1', '2', 'j']);

function stored(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function store(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: not remembered */ }
}

const S = {
  state: null,
  mode: 'dev', // or 'organize'
  selected: null, // the task in Dev mode (4a)
  boardSel: null, // the card under the cursor in Organize mode
  showHidden: stored('showHidden', false),
  starting: null, // task id: select sent, waiting for its session to start
  terms: new Map(), // task id -> {id, el, term, fit, search, gen, ws}
  archiveOpen: false,
  picker: null, // {items, index}
};

// the task that the header and the actions apply to
const targetId = () => (S.mode === 'organize' ? S.boardSel : S.selected);

// ------------------------------------------------------------------ control channel

let ctl;
function connectCtl() {
  ctl = new WebSocket(`ws://${location.host}/ctl`);
  ctl.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === 'state') onState(m);
    else if (m.type === 'reply') {
      const p = pending.get(m.req);
      if (p) { pending.delete(m.req); if (m.ok) p.resolve(m.data); else p.reject(new Error(m.error)); }
    } else if (m.type === 'error') toast(m.message);
  };
  ctl.onclose = () => {
    banner('Lost the connection to the backend. Restart <code>bf</code>.', true);
    for (const p of pending.values()) p.reject(new Error('lost the connection to the backend'));
    pending.clear();
  };
}

function send(cmd, extra = {}) {
  if (ctl && ctl.readyState === WebSocket.OPEN) ctl.send(JSON.stringify({ cmd, ...extra }));
}

// a command whose reply we need (files, new shell): a Promise of the reply's data
let reqSeq = 0;
const pending = new Map();
function request(cmd, extra = {}) {
  return new Promise((resolve, reject) => {
    if (!ctl || ctl.readyState !== WebSocket.OPEN) { reject(new Error('not connected to the backend')); return; }
    const req = ++reqSeq;
    pending.set(req, { resolve, reject });
    ctl.send(JSON.stringify({ cmd, req, ...extra }));
  });
}

const SPLASH_MIN_MS = 900;
function hideSplash() {
  const el = $('#splash');
  if (!el || el.classList.contains('gone')) return;
  setTimeout(() => {
    el.classList.add('gone');
    setTimeout(() => el.remove(), 500);
  }, Math.max(0, SPLASH_MIN_MS - performance.now()));
}
setTimeout(hideSplash, 8000); // never hide the app behind it: the banner explains a dead backend

function onState(st) {
  const prev = S.state;
  S.state = st;
  hideSplash();
  if (!prev && st.selected && st.tasks[st.selected]) return select(st.selected);
  // new and forked tasks: the backend selects them; follow it, into Dev mode
  if (prev && st.selected && !prev.tasks[st.selected] && st.tasks[st.selected]) {
    setMode('dev', false);
    return select(st.selected, false);
  }
  if (S.starting && st.tasks[S.starting] && (st.tasks[S.starting].pty || st.tasks[S.starting].outside)) S.starting = null;
  render();
}

// ------------------------------------------------------------------ terminals

// the size of a 4a terminal, for a session about to start
function termSize() {
  const t = S.selected && S.terms.get(S.selected);
  if (t && t.el.style.display !== 'none') return { rows: t.term.rows, cols: t.term.cols };
  for (const x of S.terms.values()) if (!x.shell) return { rows: x.term.rows, cols: x.term.cols };
  const box = $('#agent').getBoundingClientRect(); // estimate for Menlo 13px
  return { rows: Math.max(10, Math.floor((box.height - 6) / 17)), cols: Math.max(40, Math.floor((box.width - 8) / 7.8)) };
}

// One xterm per PTY. Key: a task id (its Claude Code session, in 4a) or a shell key (3b).
function ensureTerm(id, container = $('#terms'), shell = false) {
  let t = S.terms.get(id);
  if (t) return t;
  const el = document.createElement('div');
  el.className = 'term';
  container.appendChild(el);
  const term = new Terminal({
    allowProposedApi: true, macOptionIsMeta: true, scrollback: 50000, cursorBlink: true,
    fontFamily: 'Menlo, Monaco, monospace', fontSize: 13, theme: { background: '#1e1e1e' },
  });
  const fit = new FitAddon.FitAddon();
  const search = new SearchAddon.SearchAddon();
  term.loadAddon(fit);
  term.loadAddon(search);
  term.loadAddon(new Unicode11Addon.Unicode11Addon());
  term.unicode.activeVersion = '11';
  term.open(el);
  try {
    const gl = new WebglAddon.WebglAddon();
    gl.onContextLoss(() => gl.dispose());
    term.loadAddon(gl);
  } catch (e) { window.__errors.push('webgl: ' + e); }
  t = { id, el, term, fit, search, gen: null, ws: null, shell };
  // a hidden terminal (Organize mode) must never type into Claude: drop its input
  term.onData((d) => { if (S.mode === 'dev') wsSend(t, { t: 'in', d }); });
  term.onBinary((d) => { if (S.mode === 'dev') wsSend(t, { t: 'bin', d }); });
  term.onResize(({ cols, rows }) => wsSend(t, { t: 'resize', cols, rows }));
  term.attachCustomKeyEventHandler((e) => {
    if (S.mode !== 'dev') return false;
    if (e.metaKey && OUR_KEYS.has(e.key.toLowerCase())) return false;
    if (!shell && e.key === 'Enter' && e.shiftKey && !e.metaKey) { // new line in Claude Code: ESC CR
      if (e.type === 'keydown') wsSend(t, { t: 'in', d: '\x1b\r' });
      return false;
    }
    return true;
  });
  S.terms.set(id, t);
  return t;
}

function wsSend(t, msg) {
  if (t.ws && t.ws.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify(msg));
}

function connect(t, gen) {
  if (t.gen === gen) return; // same session: connected, or ended (the state will say so)
  if (t.ws) { t.ws.onclose = null; t.ws.close(); }
  if (t.gen !== null) t.term.reset();
  t.gen = gen;
  const ws = new WebSocket(`ws://${location.host}/pty/${t.id}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => ws.send(JSON.stringify({ t: 'resize', cols: t.term.cols, rows: t.term.rows }));
  ws.onmessage = (ev) => t.term.write(new Uint8Array(ev.data));
  ws.onclose = () => { if (t.ws === ws) t.ws = null; };
  t.ws = ws;
}

// Fit only a terminal that is on screen: a hidden one (Organize mode, hidden panel) has no
// size, and fitting it would shrink its PTY and make the program re-render for nothing.
function fitSafely(t) {
  const box = t.el.getBoundingClientRect();
  if (box.width > 50 && box.height > 30) t.fit.fit();
}

function showTerm(id) {
  for (const x of S.terms.values()) if (!x.shell) x.el.style.display = x.id === id ? '' : 'none';
  const t = S.terms.get(id);
  if (t && S.mode === 'dev') requestAnimationFrame(() => { fitSafely(t); t.term.focus(); });
}

new ResizeObserver(() => {
  const t = S.selected && S.terms.get(S.selected);
  if (t) requestAnimationFrame(() => fitSafely(t));
}).observe($('#agent'));

// ------------------------------------------------------------------ modes

function setMode(mode, doRender = true) {
  if (mode === S.mode) return;
  S.mode = mode;
  $('#dev').hidden = mode !== 'dev';
  $('#board').hidden = mode !== 'organize';
  if (mode === 'organize') {
    if (document.activeElement) document.activeElement.blur(); // out of the terminal
    S.boardSel = S.boardSel && S.state && S.state.tasks[S.boardSel] ? S.boardSel : S.selected;
    $('#board').focus();
  } else if (S.selected) {
    showTerm(S.selected);
  }
  if (doRender) render();
}

function openInDev(id) {
  setMode('dev', false);
  select(id);
}

// ------------------------------------------------------------------ selection and actions

function select(id, notify = true) {
  S.selected = id;
  const t = ensureTerm(id);
  showTerm(id);
  const task = S.state && S.state.tasks[id];
  if (task && !task.pty && !task.outside) S.starting = id;
  render();
  if (notify) {
    requestAnimationFrame(() => {
      fitSafely(t);
      send('select', { id, rows: t.term.rows, cols: t.term.cols, shellRows: shellBoxSize().rows, shellCols: shellBoxSize().cols });
    });
  }
  if (task) restoreFiles(id); // the file tabs of its last session
}

function visibleInbox() {
  const st = S.state;
  const ids = [...st.inbox];
  if (S.selected && st.tasks[S.selected] && !ids.includes(S.selected)) ids.unshift(S.selected);
  return ids;
}

function move(step) {
  const ids = visibleInbox();
  if (!ids.length) return;
  const i = ids.indexOf(S.selected);
  select(ids[i < 0 ? 0 : (i + step + ids.length) % ids.length]);
}

function act(cmd) {
  const id = targetId();
  if (id) send(cmd, { id, ...termSize() });
}

// ------------------------------------------------------------------ board (Organize mode)

function hue(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function boardColumns(st) {
  const tasks = Object.values(st.tasks).filter((t) => S.showHidden || t.status !== 'done');
  return PRIOS.filter((p) => S.showHidden || !HIDDEN_PRIOS.has(p)).map((prio) => ({
    prio,
    ids: tasks.filter((t) => t.prio === prio).sort((a, b) => Date.parse(b.updated) - Date.parse(a.updated)).map((t) => t.id),
  }));
}

function card(t) {
  const cls = ['card', t.id === S.boardSel ? 'sel' : '', t.status === 'done' ? 'archived' : '', t.unread ? 'unread' : ''].join(' ');
  return `<div class="${cls}" data-card="${t.id}" draggable="true">
    <div class="top"><span class="dot ${t.status}" title="${t.status}"></span><span class="live ${t.pty || t.live ? 'on' : ''}"></span>
      ${t.inbox ? '<span class="inbox-badge">inbox</span>' : ''}<span class="proj" style="--h:${hue(t.project)}">${esc(t.project)}</span></div>
    <div class="title">${esc(t.title)}</div>
    ${t.inbox && t.line ? `<div class="line">${esc(t.line)}</div>` : ''}
  </div>`;
}

// the card under the cursor must be on the board (it may have been hidden or moved)
function ensureBoardSel(cols) {
  if (!S.boardSel || !cols.some((c) => c.ids.includes(S.boardSel))) {
    const first = cols.find((c) => c.ids.length);
    S.boardSel = first ? first.ids[0] : null;
  }
}

function renderBoard(st) {
  $('#show-hidden').checked = S.showHidden;
  const cols = boardColumns(st);
  ensureBoardSel(cols);
  $('#columns').innerHTML = cols.map((c) => `
    <div class="column" data-prio="${c.prio}">
      <h3>${c.prio.replace('_', ' ')} <span>${c.ids.length || ''}</span></h3>
      <div class="cards">${c.ids.length ? c.ids.map((id) => card(st.tasks[id])).join('') : '<div class="empty">Empty.</div>'}</div>
    </div>`).join('');
  const sel = document.querySelector('.card.sel');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}

function setShowHidden(on) {
  S.showHidden = on;
  store('showHidden', on);
  render();
}

function boardMove(dx, dy) {
  const cols = boardColumns(S.state);
  let ci = cols.findIndex((c) => c.ids.includes(S.boardSel));
  if (ci < 0) return render();
  let ri = cols[ci].ids.indexOf(S.boardSel);
  if (dx) {
    let j = ci + dx;
    while (j >= 0 && j < cols.length && !cols[j].ids.length) j += dx; // skip empty columns
    if (j < 0 || j >= cols.length) return;
    ci = j;
    ri = Math.min(ri, cols[ci].ids.length - 1);
  } else {
    ri = Math.max(0, Math.min(cols[ci].ids.length - 1, ri + dy));
  }
  S.boardSel = cols[ci].ids[ri];
  render();
}

function movePrio(step) {
  const t = S.state.tasks[S.boardSel];
  if (!t) return;
  const i = PRIOS.indexOf(t.prio) + step;
  if (i < 0 || i >= PRIOS.length) return;
  t.prio = PRIOS[i]; // at once on screen; the backend confirms with the next state
  send('prio', { id: t.id, prio: t.prio });
  if (HIDDEN_PRIOS.has(t.prio) && !S.showHidden) setShowHidden(true); // keep the card in view
  else render();
}

function newTask(projectPath) { // also called by the selftest
  send('new', { project: projectPath, ...termSize() });
}

// ------------------------------------------------------------------ rendering

function render() {
  const st = S.state;
  if (!st) return;
  for (const b of document.querySelectorAll('.mode')) b.classList.toggle('on', b.dataset.mode === S.mode);
  if (S.mode === 'organize') ensureBoardSel(boardColumns(st));
  renderBanner(st);
  renderHead(st);
  if (S.mode === 'organize') return renderBoard(st);
  renderInbox(st);
  renderThreads(st);
  renderArchive(st);
  renderAgent(st);
  renderExplorer();
  renderFiles();
  renderShells();
}

function renderBanner(st) {
  if (!st.hookInstalled) return banner('The Brain Fry hook is not installed, so tasks will not update. Run <code>bf install</code> in a terminal, then restart your Claude Code sessions.');
  if (st.digestError) return banner(esc(st.digestError));
  banner(null);
}

function banner(html, sticky = false) {
  const b = $('#banner');
  if (b.dataset.sticky && !sticky) return;
  if (sticky) b.dataset.sticky = '1';
  b.hidden = !html;
  if (html) b.innerHTML = html;
}

function renderHead(st) {
  const task = st.tasks[targetId()];
  const head = $('#task-head');
  if (!task) { head.innerHTML = ''; return; }
  const editing = head.querySelector('input.rename');
  if (editing) return; // do not clobber a rename in progress
  head.innerHTML = `
    <span class="dot ${task.status}" title="${task.status}"></span>
    <span class="title" title="Double-click to rename">${esc(task.title)}</span>
    <span class="meta">${esc(task.project)} · ${task.status}${task.parent ? ' · fork' : ''}</span>
    <select class="prio" title="Prio column">${PRIOS.map((p) => `<option value="${p}" ${p === task.prio ? 'selected' : ''}>${p.replace('_', ' ')}</option>`).join('')}</select>
    <span class="actions">
      <button data-act="dismiss" ${task.inbox ? '' : 'disabled'} title="Out of the inbox, nothing to do (Cmd+D)">Dismiss</button>
      <button data-act="archive" title="Cmd+E">${task.status === 'done' ? 'Unarchive' : 'Archive'}</button>
      <button data-act="fork" title="Split: a new task with a copy of this conversation (Cmd+Shift+D)">Fork</button>
    </span>`;
}

function row(task, extra = '') {
  const cls = ['row', extra, task.unread ? 'unread' : '', task.id === S.selected ? 'sel' : ''].join(' ');
  const kind = KINDS.has(task.kind) ? ` · <span class="kind ${task.kind}">${task.kind}</span>` : '';
  const live = task.pty ? 'on' : task.live ? 'on' : '';
  const liveTitle = task.pty ? 'live in Brain Fry' : task.live ? 'live in another terminal' : 'no live session';
  return `<div class="${cls}" data-id="${task.id}">
    <div class="marks"><span class="dot ${task.status}" title="${task.status}"></span><span class="live ${live}" title="${liveTitle}"></span></div>
    <div class="main">
      <div class="title">${esc(task.title)}</div>
      <div class="meta">${esc(task.project)} · ${task.prio.replace('_', ' ')}${kind}</div>
      ${task.line ? `<div class="line">${esc(task.line)}</div>` : ''}
      ${task.question ? `<div class="q">Q: ${esc(task.question)}</div>` : ''}
    </div></div>`;
}

function renderInbox(st) {
  const ids = visibleInbox();
  $('#inbox-count').textContent = st.inbox.length ? `(${st.inbox.length})` : '';
  $('#inbox').innerHTML = ids.length
    ? ids.map((id) => row(st.tasks[id], st.inbox.includes(id) ? '' : 'current')).join('')
    : '<div class="empty">Nothing needs you.</div>';
}

function renderThreads(st) {
  const task = st.tasks[S.selected];
  const threads = task ? [...task.threads].reverse() : [];
  $('#threads').innerHTML = threads.length
    ? threads.map((th) => {
      const kind = th.kind === 'working' || th.kind === 'interrupted' || th.kind === 'pending' ? th.kind : `<span class="kind ${th.kind}">${th.kind}</span>`;
      const txt = th.digest || th.prompt || '(no prompt)';
      return `<div class="thread" data-n="${th.n}" title="${esc(th.prompt)} (click: open in 3a)"><span class="n">${th.n}</span>${kind}<span class="txt">${esc(txt)}</span></div>`;
    }).join('')
    : '<div class="empty">No threads yet.</div>';
}

function renderArchive(st) {
  $('#archive-count').textContent = st.archive.length ? `(${st.archive.length})` : '';
  $('#archive-toggle').classList.toggle('open', S.archiveOpen);
  const el = $('#archive');
  el.hidden = !S.archiveOpen;
  if (S.archiveOpen) el.innerHTML = st.archive.length ? st.archive.map((id) => row(st.tasks[id])).join('') : '<div class="empty">Empty.</div>';
}

function renderAgent(st) {
  const task = st.tasks[S.selected];
  if (!S.selected) return overlay('<div class="big">Select a task in the inbox, or start one with Cmd+N.</div>');
  if (!task) return overlay('<div class="big">This task is gone.</div>');
  const t = S.terms.get(task.id);
  if (task.pty) {
    if (t) connect(t, task.pty);
    return overlay(null);
  }
  if (task.outside) {
    return overlay(`<div class="big">This session runs in another terminal.</div>
      <div>It becomes resumable here when it ends. Resuming it now runs two processes on one conversation.</div>
      <button data-act="resume">Resume here anyway</button>`);
  }
  if (S.starting === task.id) return overlay('<div>Starting...</div>');
  overlay(`<div class="big">No live session.</div><button data-act="resume">Resume</button>`);
}

function overlay(html) {
  const o = $('#overlay');
  o.hidden = !html;
  if (html) o.innerHTML = html;
}

function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 6000);
}

// ------------------------------------------------------------------ project picker (new task)

function openPicker() {
  const projects = S.state ? S.state.projects : [];
  S.picker = { items: projects, index: 0 };
  renderPicker();
  $('#picker').hidden = false;
}

function closePicker() {
  $('#picker').hidden = true;
  S.picker = null;
  const t = S.selected && S.terms.get(S.selected);
  if (S.mode === 'organize') $('#board').focus();
  else if (t) t.term.focus();
}

function renderPicker() {
  const p = S.picker;
  $('#picker').innerHTML = `<div class="box"><h3>New task in project...</h3>${p.items.length
    ? p.items.map((it, i) => `<div class="item ${i === p.index ? 'sel' : ''}" data-i="${i}"><div>${esc(it.name)}</div><div class="path">${esc(it.path)}</div></div>`).join('')
    : '<div class="empty">No project yet. Run <code>bf init</code> in a project folder.</div>'}</div>`;
}

function pick(i) {
  const it = S.picker && S.picker.items[i];
  closePicker();
  if (it) newTask(it.path);
}

// ------------------------------------------------------------------ events

document.addEventListener('keydown', (e) => {
  if (S.picker) {
    const n = S.picker.items.length;
    if (e.key === 'Escape') closePicker();
    else if (e.key === 'ArrowDown' && n) { S.picker.index = (S.picker.index + 1) % n; renderPicker(); }
    else if (e.key === 'ArrowUp' && n) { S.picker.index = (S.picker.index - 1 + n) % n; renderPicker(); }
    else if (e.key === 'Enter') pick(S.picker.index);
    else return;
    e.preventDefault(); e.stopPropagation();
    return;
  }
  if (e.target.matches && e.target.matches('input, select, textarea')) return; // a rename in progress
  if (S.mode === 'organize' && !e.metaKey && !e.ctrlKey && !e.altKey) {
    const k = e.key;
    if (k === 'ArrowLeft') e.shiftKey ? movePrio(-1) : boardMove(-1, 0);
    else if (k === 'ArrowRight') e.shiftKey ? movePrio(1) : boardMove(1, 0);
    else if (k === 'ArrowUp') boardMove(0, -1);
    else if (k === 'ArrowDown') boardMove(0, 1);
    else if (k === 'Enter') { if (S.boardSel) openInDev(S.boardSel); }
    else if (k === 'Escape') setMode('dev');
    else if (k === 'h' || k === 'H') setShowHidden(!S.showHidden);
    else if (k === 'e') act('archive');
    else if (k === 'd') act('dismiss');
    else if (k === 'f') act('fork');
    else if (k === 'n') openPicker();
    // any other key is swallowed too: nothing typed in Organize mode may reach a terminal
    e.preventDefault(); e.stopPropagation();
    return;
  }
  if (!e.metaKey) return;
  const k = e.key.toLowerCase();
  // in a code editor, the editor keeps Cmd+D (next occurrence) and Cmd+Up/Down (start/end)
  if (e.target.closest && e.target.closest('.cm-editor') && ['d', 'arrowup', 'arrowdown'].includes(k)) return;
  if (k === 'b') setMode(S.mode === 'dev' ? 'organize' : 'dev');
  else if (k === '1' && S.mode === 'dev') toggleLayout('explorer');
  else if (k === '2' && S.mode === 'dev') toggleLayout('middle');
  else if (k === 'j' && S.mode === 'dev') toggleLayout('shells');
  else if (k === 'arrowdown' && S.mode === 'dev') move(1);
  else if (k === 'arrowup' && S.mode === 'dev') move(-1);
  else if (k === 'n') openPicker();
  else if (k === 'd' && e.shiftKey) act('fork');
  else if (k === 'd') act('dismiss');
  else if (k === 'e') act('archive');
  else return;
  e.preventDefault(); e.stopPropagation();
}, true);

document.addEventListener('click', (e) => {
  const modeBtn = e.target.closest('.mode');
  if (modeBtn) return setMode(modeBtn.dataset.mode);
  const cardEl = e.target.closest('.card');
  if (cardEl) { S.boardSel = cardEl.dataset.card; $('#board').focus(); return render(); }
  const rowEl = e.target.closest('.list .row');
  if (rowEl) return select(rowEl.dataset.id);
  const btn = e.target.closest('[data-act]');
  if (btn && !btn.disabled) return act(btn.dataset.act);
  const item = e.target.closest('#picker .item');
  if (item) return pick(Number(item.dataset.i));
  if (e.target.id === 'picker') return closePicker();
  if (e.target.closest('#archive-toggle')) { S.archiveOpen = !S.archiveOpen; render(); }
});

$('#new-btn').addEventListener('click', openPicker);

document.addEventListener('change', (e) => {
  if (e.target.matches('#task-head select.prio') && targetId()) send('prio', { id: targetId(), prio: e.target.value });
  if (e.target.id === 'show-hidden') { setShowHidden(e.target.checked); $('#board').focus(); }
});

// drag a card to another column: same as Shift+Left/Right
document.addEventListener('dragstart', (e) => {
  const c = e.target.closest && e.target.closest('.card');
  if (c) e.dataTransfer.setData('text/plain', c.dataset.card);
});
document.addEventListener('dragover', (e) => {
  const col = e.target.closest && e.target.closest('.column');
  if (!col) return;
  e.preventDefault();
  for (const x of document.querySelectorAll('.column.drop')) if (x !== col) x.classList.remove('drop');
  col.classList.add('drop');
});
document.addEventListener('drop', (e) => {
  const col = e.target.closest && e.target.closest('.column');
  const id = e.dataTransfer.getData('text/plain');
  for (const x of document.querySelectorAll('.column.drop')) x.classList.remove('drop');
  if (!col || !S.state.tasks[id]) return;
  e.preventDefault();
  S.state.tasks[id].prio = col.dataset.prio;
  S.boardSel = id;
  send('prio', { id, prio: col.dataset.prio });
  render();
});

document.addEventListener('dblclick', (e) => {
  const cardEl = e.target.closest('.card');
  if (cardEl) return openInDev(cardEl.dataset.card);
  const title = e.target.closest('#task-head .title');
  const id = targetId();
  if (!title || !id) return;
  const input = document.createElement('input');
  input.className = 'rename';
  input.value = S.state.tasks[id].title;
  title.replaceWith(input);
  input.focus();
  input.select();
  const done = (save) => {
    if (save && input.value.trim()) send('rename', { id, title: input.value.trim() });
    input.remove();
    render();
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') done(true);
    if (ev.key === 'Escape') done(false);
    ev.stopPropagation();
  });
  input.addEventListener('blur', () => done(false));
});

connectCtl();
