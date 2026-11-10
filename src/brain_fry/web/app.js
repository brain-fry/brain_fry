'use strict';
// Brain Fry front end. The backend pushes the whole state on /ctl; commands go back on /ctl.
// Each task with a live session has its own xterm.js terminal, fed by /pty/<task id>.

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const KINDS = new Set(['review', 'answer', 'rule', 'FYI', 'blocked']);
const PRIOS = ['working', 'planning', 'later', 'more_later', 'never'];
const HIDDEN_PRIOS = new Set(['later', 'more_later', 'never']);
const OUR_KEYS = new Set(['arrowdown', 'arrowup', 'n', 'p', 'd', 'e', 'b', '1', '2', 'j']);

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
  picker: null, // {mode, items, index, moved}; mode 'new' (Cmd+N: new task) or 'open' (Cmd+P: switch project)
  othersOpen: stored('othersOpen', true), // the region of other sessions under the board
  otherSel: null, // the other session under the mouse cursor (click)
  adopting: null, // {id, open}: adopt sent, waiting for the task in the state
  dragging: false, // no re-render of the board while a card is dragged
  autoAdvance: stored('autoAdvance', true), // after an answer, select the next inbox item
  advance: null, // {from, order, at, timer}: an advance waiting for its second
  lastAnswered: null, // the task that the last advance (answer or archive) left: Cmd+Up from the first item goes back
  project: null, // the project picked in the dropdown when it had no task to land on (path)
};

// the task that the header and the actions apply to
const targetId = () => (S.mode === 'organize' ? S.boardSel : S.selected);

// ------------------------------------------------------------------ control channel

let ctl;
function connectCtl() {
  ctl = new WebSocket(`ws://${location.host}/ctl`);
  ctl.onopen = () => { for (const m of window.__errors) reportError(m); }; // errors from before the connection
  ctl.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === 'state') onState(m);
    else if (m.type === 'prompted') onPrompted(m);
    else if (m.type === 'browser') onBrowser(m); // browser.js
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

// JS errors go to the backend's log (bf.log), as well as to window.__errors for the selftest
function reportError(msg) { send('log', { level: 'error', msg: String(msg).slice(0, 4000) }); }
{
  const push = window.__errors.push.bind(window.__errors);
  window.__errors.push = (m) => { reportError(m); return push(m); };
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason; // WebKit's stack does not hold the message
    window.__errors.push(`unhandled rejection: ${r instanceof Error ? `${r.name}: ${r.message}\n${r.stack}` : r}`);
  });
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
    const task = st.tasks[st.selected];
    const from = task.parent || S.newFrom; // whose tabs the backend copied: the parent of a fork
    S.newFrom = null;
    if (from && st.tasks[from] && st.tasks[from].projectPath === task.projectPath) copyView(from, task.id); // files.js
    setMode('dev', false);
    return select(st.selected, false);
  }
  if (S.starting && st.tasks[S.starting] && (st.tasks[S.starting].pty || st.tasks[S.starting].outside)) S.starting = null;
  if (S.adopting && st.tasks[S.adopting.id]) { // a session of the bottom region is now a task
    const { id, open } = S.adopting;
    S.adopting = null;
    S.boardSel = id;
    if (open) return openInDev(id);
  }
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
  browserTermLinks(term); // Cmd+click on a URL: a browser tab (browser.js)
  try {
    const gl = new WebglAddon.WebglAddon();
    gl.onContextLoss(() => gl.dispose());
    term.loadAddon(gl);
  } catch (e) { window.__errors.push('webgl: ' + e); }
  t = { id, el, term, fit, search, gen: null, ws: null, shell };
  // a hidden terminal (Organize mode) must never type into Claude: drop its input
  term.onData((d) => { if (S.mode === 'dev') { t.lastInput = Date.now(); wsSend(t, { t: 'in', d }); } });
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

// ------------------------------------------------------------------ project dropdown (header)
// Picking a project lands in Dev mode on the task of that project that I opened last
// (`opened`, set by the backend at each select), with its files and terminals.

function currentProject() {
  const task = S.state && S.state.tasks[S.selected];
  return task ? task.projectPath : S.project;
}

// archived tasks are done: never landed on (newTask counts them). Never opened (older tasks): the last updated.
function lastOpened(path, archived = false) {
  const time = (iso) => Date.parse(iso) || 0;
  const tasks = Object.values(S.state.tasks).filter((t) => t.projectPath === path && (archived || t.status !== 'done'));
  tasks.sort((a, b) => time(b.opened) - time(a.opened) || time(b.updated) - time(a.updated));
  return tasks.length ? tasks[0].id : null;
}

function openProject(path) {
  const id = lastOpened(path);
  const name = path.split('/').pop();
  if (id) {
    uiLog(`project ${name}: lands on ${id.slice(0, 8)} ${JSON.stringify(S.state.tasks[id].title)}`);
    return openInDev(id);
  }
  uiLog(`project ${name}: no open task to land on`);
  setMode('dev', false);
  cancelAdvance();
  S.project = path;
  S.selected = null;
  S.starting = null;
  showTerm(null);
  send('select', { id: null });
  render();
}

function renderProjectSelect(st) {
  const el = $('#project-select');
  if (document.activeElement === el) return; // its menu may be open
  const cur = currentProject() || '';
  const count = {};
  for (const p of st.projects) count[p.name] = (count[p.name] || 0) + 1;
  // two folders with one name: add the parent folder
  const label = (p) => (count[p.name] > 1 ? `${p.name} (${p.path.split('/').slice(-2, -1)[0]})` : p.name);
  const html = (cur ? '' : '<option value="" disabled selected>Project</option>')
    + st.projects.map((p) => `<option value="${esc(p.path)}" ${p.path === cur ? 'selected' : ''}>${esc(label(p))}</option>`).join('');
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
  if (cur) el.value = cur;
  else el.selectedIndex = 0; // the placeholder
}

// ------------------------------------------------------------------ selection and actions

function select(id, notify = true) {
  if (S.advance && S.advance.from !== id) cancelAdvance();
  S.selected = id;
  S.project = null; // the dropdown follows the task's project
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
  const i = ids.indexOf(S.selected);
  const back = S.lastAnswered;
  if (step < 0 && i <= 0 && back && back !== S.selected && S.state.tasks[back] && !ids.includes(back)) {
    return select(back); // to watch it, or to press Esc when its agent goes the wrong way
  }
  if (!ids.length) return;
  select(ids[i < 0 ? 0 : (i + step + ids.length) % ids.length]);
}

// ------------------------------------------------------------------ auto-advance (DESIGN_DETAILS.md)
// When I answer the selected task (its UserPromptSubmit, or the dialog of a blocked task
// that closed: m.why), select the next inbox item about 1 s later: the second lets me see
// that the prompt was accepted.

const ADVANCE_MS = 1000;

function uiLog(msg) { send('log', { level: 'info', msg }); }

function onPrompted(m) {
  if (m.id !== S.selected || S.mode !== 'dev') return;
  const title = (S.state.tasks[m.id] || {}).title || m.id;
  if (!S.autoAdvance) return uiLog(`auto-advance off: stays on ${m.id.slice(0, 8)} ${JSON.stringify(title)}`);
  if (m.slash) return uiLog(`auto-advance: no advance for a slash command in ${m.id.slice(0, 8)}`);
  cancelAdvance();
  // the inbox order before the answer: the "next" item is the one after it
  S.advance = { from: m.id, order: [...S.state.inbox], at: Date.now(), why: m.why };
  S.advance.timer = setTimeout(doAdvance, ADVANCE_MS);
}

function cancelAdvance() {
  if (S.advance) clearTimeout(S.advance.timer);
  S.advance = null;
}

// only items that need me: a pinned task without a reply or a block is skipped
function nextAfter(from, order) {
  const inbox = S.state.inbox.filter((id) => id !== from && S.state.tasks[id].inbox);
  const i = order.indexOf(from);
  if (i >= 0) for (const id of [...order.slice(i + 1), ...order.slice(0, i)]) if (inbox.includes(id)) return id;
  return inbox[0] || null;
}

function doAdvance() {
  const a = S.advance;
  S.advance = null;
  if (!a || S.mode !== 'dev' || S.selected !== a.from || !S.autoAdvance) return;
  const t = S.terms.get(a.from);
  if (t && t.lastInput > a.at) return uiLog(`auto-advance: stays on ${a.from.slice(0, 8)}, I am typing in it`);
  const next = nextAfter(a.from, a.order);
  if (!next) return uiLog(`auto-advance: stays on ${a.from.slice(0, 8)}, nothing else needs me`);
  uiLog(`auto-advance${a.why ? ` (${a.why})` : ''}: ${a.from.slice(0, 8)} --> ${next.slice(0, 8)} ${JSON.stringify(S.state.tasks[next].title)}`);
  S.lastAnswered = a.from;
  select(next);
}

function act(cmd) {
  const id = targetId();
  if (!id) return;
  const task = S.state.tasks[id];
  const archiving = cmd === 'archive' && S.mode === 'dev' && id === S.selected && task && task.status !== 'done';
  const next = archiving ? nextAfter(id, [...S.state.inbox]) : null; // the inbox order before the archive
  const sh = cmd === 'fork' ? shellBoxSize() : null; // a fork starts the parent's terminal tabs
  send(cmd, { id, ...termSize(), ...(sh ? { shellRows: sh.rows, shellCols: sh.cols } : {}) });
  if (archiving) afterArchive(id, next);
}

// Archiving the selected task (Dev mode): it is done, so it leaves 1a at once and the next
// item that needs me is selected, whatever the auto-advance switch (that switch is about
// watching an agent after an answer). Nothing needs me: 4a empties. Cmd+Up goes back to the
// archived task, to unarchive it.
function afterArchive(id, next) {
  S.lastAnswered = id;
  S.state.inbox = S.state.inbox.filter((x) => x !== id); // no flash of its row before the state push
  uiLog(`archive: ${id.slice(0, 8)} --> ${next ? `${next.slice(0, 8)} ${JSON.stringify(S.state.tasks[next].title)}` : 'nothing needs me'}`);
  if (next) return select(next);
  cancelAdvance();
  S.selected = null;
  S.starting = null;
  S.project = null;
  showTerm(null);
  send('select', { id: null });
  render();
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
    <div class="top"><span class="dot ${t.status}" title="${t.status}"></span>
      ${t.inbox ? '<span class="inbox-badge">inbox</span>' : ''}${t.pinned ? '<span class="pin-badge">pinned</span>' : ''}${runBadge(t.elsewhere)}<span class="proj" style="--h:${hue(t.project)}">${esc(t.project)}</span></div>
    <div class="title" title="${esc(t.title)}">${esc(t.shortTitle || t.title)}</div>
    ${t.digest ? `<div class="desc" title="${esc(t.digest)}">${esc(t.digest)}</div>` : ''}
    ${t.inbox && t.line ? `<div class="line need">${esc(t.line)}</div>` : ''}
  </div>`;
}

// where a session runs outside Brain Fry: "VS Code · idle"
function runBadge(run) {
  if (!run) return '';
  return `<span class="run ${esc(run.status)}" title="runs now in ${esc(run.where)} (pid ${run.pid})">${esc(run.where)}${run.status ? ' · ' + esc(run.status) : ''}</span>`;
}

function ago(iso) {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 90) return 'just now';
  if (s < 90 * 60) return `${Math.round(s / 60)} min ago`;
  if (s < 36 * 3600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// a session of my projects that is not a task: drag it onto a column to adopt it
function otherCard(o) {
  const tip = [o.cwd, `${o.prompts} prompt(s)`, `first: ${o.firstPrompt}`, `last: ${o.lastPrompt}`].join('\n');
  return `<div class="card other ${o.id === S.otherSel ? 'sel' : ''}" data-other="${o.id}" draggable="true" title="${esc(tip)}">
    <div class="top">${runBadge(o.running)}<span class="proj" style="--h:${hue(o.project)}">${esc(o.project)}</span></div>
    <div class="title">${esc(o.title)}</div>
    <div class="line">${o.prompts} prompt${o.prompts === 1 ? '' : 's'} · ${ago(o.updated)}${o.lastPrompt ? ' · last: ' + esc(o.lastPrompt) : ''}</div>
  </div>`;
}

function renderOthers(st) {
  const list = (st.others || []).filter((o) => !st.tasks[o.id]);
  $('#others-count').textContent = list.length ? `(${list.length})` : '';
  $('#others-toggle').classList.toggle('open', S.othersOpen);
  const el = $('#others-cards');
  el.hidden = !S.othersOpen;
  if (S.othersOpen) {
    el.innerHTML = list.length ? list.map(otherCard).join('')
      : '<div class="empty">None: every session of your Brain Fry projects is a task.</div>';
  }
}

function setOthersOpen(on) {
  S.othersOpen = on;
  store('othersOpen', on);
  render();
}

function adopt(sessionId, prio, open = false) {
  S.adopting = { id: sessionId, open };
  send('adopt', { session: sessionId, prio });
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
  renderOthers(st);
  const sel = document.querySelector('#columns .card.sel');
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

// A new task starts with the tabs of the previous task (files, browser, terminals; DESIGN_DETAILS.md,
// "2a, 3a, 3b"): the selected task if it is in that project, else the task of that project that I
// opened last, archived ones included (a task that I just archived is the one that I continue).
function newTask(projectPath) { // also called by the selftest
  const sel = S.state && S.state.tasks[S.selected];
  const from = sel && sel.projectPath === projectPath ? sel.id : lastOpened(projectPath, true);
  S.newFrom = from; // onState copies its view when the new task arrives
  const sh = shellBoxSize();
  send('new', { project: projectPath, from, ...termSize(), shellRows: sh.rows, shellCols: sh.cols });
}

// ------------------------------------------------------------------ rendering

function render() {
  const st = S.state;
  if (!st) return;
  if (S.dragging && S.mode === 'organize') return; // replacing the dragged card would end the drag; dragend renders
  for (const b of document.querySelectorAll('.mode')) b.classList.toggle('on', b.dataset.mode === S.mode);
  $('#auto-advance').checked = S.autoAdvance;
  if (S.mode === 'organize') ensureBoardSel(boardColumns(st));
  renderBanner(st);
  renderProjectSelect(st);
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
  if (st.hookOutdated) return banner('The Brain Fry hook in <code>~/.brain_fry/</code> is older than this version. Run <code>bf install</code> in a terminal (no restart needed).');
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
    <span class="meta">${esc(task.project)} · ${task.status}${task.parent ? ' · fork' : ''}${task.elsewhere ? ' · runs in ' + esc(task.elsewhere.where) : ''}</span>
    <select class="prio" title="Prio column">${PRIOS.map((p) => `<option value="${p}" ${p === task.prio ? 'selected' : ''}>${p.replace('_', ' ')}</option>`).join('')}</select>
    <span class="actions">
      <button data-act="dismiss" ${task.inbox ? '' : 'disabled'} title="Out of the inbox, nothing to do (Cmd+D)">Dismiss</button>
      ${task.pinned
        ? '<button data-act="pin" title="Leaves the inbox when nothing waits for me">Unpin</button>'
        : `<button data-act="pin" ${task.status === 'done' ? 'disabled title="Unarchive it first"' : 'title="Stays in the inbox until I unpin it"'}>Pin</button>`}
      ${task.status === 'done'
        ? '<button data-act="archive" title="Back from the archive (Cmd+E)">Unarchive</button>'
        : '<button data-act="archive" title="Done: out of the inbox, on to the next item (Cmd+E)">Archive</button>'}
      <button data-act="fork" title="Split: a new task with a copy of this conversation (Cmd+Shift+D)">Fork</button>
    </span>`;
}

function row(task, extra = '') {
  const cls = ['row', extra, task.unread ? 'unread' : '', task.id === S.selected ? 'sel' : ''].join(' ');
  const kind = KINDS.has(task.kind) ? ` · <span class="kind ${task.kind}">${task.kind}</span>` : '';
  return `<div class="${cls}" data-id="${task.id}">
    <div class="marks"><span class="dot ${task.status}" title="${task.status}"></span></div>
    <div class="main">
      <div class="title">${esc(task.title)}</div>
      <div class="meta">${esc(task.project)} · ${task.prio.replace('_', ' ')}${kind}${task.pinned ? ' · <span class="pin-badge">pinned</span>' : ''}</div>
      ${task.line ? `<div class="line">${esc(task.line)}</div>` : ''}
      ${task.question ? `<div class="q">Q: ${esc(task.question)}</div>` : ''}
    </div></div>`;
}

function renderInbox(st) {
  const ids = visibleInbox();
  const waiting = st.inbox.filter((id) => st.tasks[id].inbox).length; // pinned tasks that need nothing do not count
  $('#inbox-count').textContent = waiting ? `(${waiting})` : '';
  $('#inbox').innerHTML = ids.length
    ? ids.map((id) => row(st.tasks[id], st.inbox.includes(id) ? '' : 'current')).join('')
    : '<div class="empty">Nothing needs you.</div>';
}

function renderThreads(st) {
  const task = st.tasks[S.selected];
  const threads = task ? [...task.threads].reverse() : [];
  $('#threads').innerHTML = threads.length
    ? threads.map((th) => {
      const kind = KINDS.has(th.kind) ? `<span class="kind ${th.kind}">${th.kind}</span>` : th.kind; // working, interrupted, pending, imported
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
  if (!S.selected && S.project) {
    const name = S.project.split('/').pop();
    return overlay(`<div class="big">No open task in ${esc(name)}.</div><button data-new-in="${esc(S.project)}">New task in ${esc(name)}</button>`);
  }
  if (!S.selected) return overlay('<div class="big">Select a task in the inbox, or start one with Cmd+N.</div>');
  if (!task) return overlay('<div class="big">This task is gone.</div>');
  const t = S.terms.get(task.id);
  if (task.pty) {
    if (t) connect(t, task.pty);
    return overlay(null);
  }
  if (task.outside) {
    const run = task.elsewhere;
    const where = run ? `${run.where}${run.status ? ` (${run.status}, pid ${run.pid})` : ''}` : 'another terminal';
    return overlay(`<div class="big">This session runs in ${esc(where)}.</div>
      <div>Close it there, then resume it here. Resuming it now runs two processes on one conversation: their messages get mixed.</div>
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

function toast(msg, action = null) { // action: { label, run }, a button in the toast
  const el = $('#toast');
  el.textContent = msg;
  if (action) {
    const b = document.createElement('button');
    b.textContent = action.label;
    b.addEventListener('click', () => { el.hidden = true; action.run(); });
    el.append(' ', b);
  }
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 6000);
}

// ------------------------------------------------------------------ project picker (new task, switch project)

// Cmd+N (new task) and Cmd+P (switch project; picking one does what the dropdown does) show the
// list of the dropdown: the most recently used project first (views.project_order). Enter opens
// the highlighted one, the digits 1 to 9 open the n-th one. Cmd+N highlights the first project,
// Cmd+P the first one that is not the current one: the current project is usually first, so
// Cmd+P, Enter goes back to the project before it.
function openPicker(mode = 'new') {
  S.picker = { mode, items: S.state ? S.state.projects : [], index: 0, moved: false };
  S.picker.index = pickerStart(S.picker);
  renderPicker();
  $('#picker').hidden = false;
  // projects.json may have changed since the last state (bf init in a terminal): ask again
  request('projects').then((items) => {
    const p = S.picker;
    if (!p || p.mode !== mode) return; // closed, or the other picker opened since
    const current = p.items[p.index];
    p.items = items;
    p.index = p.moved && current ? Math.max(0, items.findIndex((it) => it.path === current.path)) : pickerStart(p);
    renderPicker();
  }).catch((e) => toast(`Could not read the projects: ${e.message}`));
}

function pickerStart(p) {
  const i = p.mode === 'open' ? p.items.findIndex((it) => it.path !== currentProject()) : 0;
  return Math.max(0, i);
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
  const key = (i) => (i === p.index ? 'Enter' : i < 9 ? String(i + 1) : '');
  const head = `${p.mode === 'open' ? 'Switch to project...' : 'New task in project...'} <span class="hint">most recently used first</span>`;
  const use = (it) => (it.path === currentProject() ? '<span class="recent">current</span>' : '')
    + (it.last ? `<span class="recent" title="last opened task of this project">${ago(it.last)}</span>` : '');
  $('#picker').innerHTML = `<div class="box"><h3>${head}</h3>${p.items.length
    ? p.items.map((it, i) => `<div class="item ${i === p.index ? 'sel' : ''}" data-i="${i}">
        <div class="main"><div>${esc(it.name)}</div><div class="path">${esc(it.path)}</div></div>
        ${use(it)}
        ${key(i) ? `<kbd>${key(i)}</kbd>` : ''}</div>`).join('')
    : '<div class="empty">No project yet. Run <code>bf init</code> in a project folder.</div>'}</div>`;
}

function pick(i) {
  const it = S.picker && S.picker.items[i];
  const mode = S.picker && S.picker.mode;
  closePicker();
  if (it && mode === 'open') openProject(it.path);
  else if (it) newTask(it.path);
}

// ------------------------------------------------------------------ events

document.addEventListener('keydown', (e) => {
  if (S.picker) {
    const n = S.picker.items.length;
    if (e.key === 'Escape') closePicker();
    else if (e.key === 'ArrowDown' && n) { S.picker.index = (S.picker.index + 1) % n; S.picker.moved = true; renderPicker(); }
    else if (e.key === 'ArrowUp' && n) { S.picker.index = (S.picker.index - 1 + n) % n; S.picker.moved = true; renderPicker(); }
    else if (e.key === 'Enter') pick(S.picker.index);
    else if (/^[1-9]$/.test(e.key) && !e.metaKey && !e.ctrlKey && !e.altKey) { if (Number(e.key) <= n) pick(Number(e.key) - 1); }
    else return;
    e.preventDefault(); e.stopPropagation();
    return;
  }
  // a rename, a resume note, an address field keep their keys. Not xterm.js's hidden textarea:
  // it has the focus whenever a terminal (4a, 3b) has it, and the terminal's Cmd keys are ours
  const field = e.target.matches && e.target.matches('input, select, textarea');
  if (field && !e.target.classList.contains('xterm-helper-textarea')) return;
  if (S.mode === 'organize' && !e.metaKey && !e.ctrlKey && !e.altKey) {
    const k = e.key;
    if (k === 'ArrowLeft') e.shiftKey ? movePrio(-1) : boardMove(-1, 0);
    else if (k === 'ArrowRight') e.shiftKey ? movePrio(1) : boardMove(1, 0);
    else if (k === 'ArrowUp') boardMove(0, -1);
    else if (k === 'ArrowDown') boardMove(0, 1);
    else if (k === 'Enter') { if (S.boardSel) openInDev(S.boardSel); }
    else if (k === 'Escape') setMode('dev');
    else if (k === 'h' || k === 'H') setShowHidden(!S.showHidden);
    else if (k === 'o' || k === 'O') setOthersOpen(!S.othersOpen);
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
  else if (k === 'p') openPicker('open');
  else if (k === 'd' && e.shiftKey) act('fork');
  else if (k === 'd') act('dismiss');
  else if (k === 'e') act('archive');
  else return;
  e.preventDefault(); e.stopPropagation();
}, true);

document.addEventListener('click', (e) => {
  const modeBtn = e.target.closest('.mode');
  if (modeBtn) return setMode(modeBtn.dataset.mode);
  // A double-click on a card is the second click (detail 2), not a dblclick listener: the
  // first click re-renders the board, so the second click's card is gone from the page
  // before WebKit sends it the dblclick, and the event never reaches the document.
  const otherEl = e.target.closest('.card.other');
  if (otherEl && e.detail === 2) return adopt(otherEl.dataset.other, 'working', true);
  if (otherEl) { S.otherSel = otherEl.dataset.other; $('#board').focus(); return render(); }
  if (e.target.closest('#others-toggle')) return setOthersOpen(!S.othersOpen);
  const cardEl = e.target.closest('.card');
  if (cardEl && e.detail === 2) return openInDev(cardEl.dataset.card);
  if (cardEl) { S.boardSel = cardEl.dataset.card; $('#board').focus(); return render(); }
  const rowEl = e.target.closest('.list .row');
  if (rowEl) return select(rowEl.dataset.id);
  const newIn = e.target.closest('[data-new-in]');
  if (newIn) return newTask(newIn.dataset.newIn);
  const btn = e.target.closest('[data-act]');
  if (btn && !btn.disabled) return act(btn.dataset.act);
  const item = e.target.closest('#picker .item');
  if (item) return pick(Number(item.dataset.i));
  if (e.target.id === 'picker') return closePicker();
  if (e.target.closest('#archive-toggle')) { S.archiveOpen = !S.archiveOpen; render(); }
});

$('#new-btn').addEventListener('click', () => openPicker());

document.addEventListener('change', (e) => {
  if (e.target.matches('#task-head select.prio') && targetId()) send('prio', { id: targetId(), prio: e.target.value });
  if (e.target.id === 'show-hidden') { setShowHidden(e.target.checked); $('#board').focus(); }
  if (e.target.id === 'project-select') {
    e.target.blur(); // the selected task's terminal takes the focus
    if (e.target.value) openProject(e.target.value);
  }
  if (e.target.id === 'auto-advance') {
    S.autoAdvance = e.target.checked;
    store('autoAdvance', S.autoAdvance);
    if (!S.autoAdvance) cancelAdvance();
    uiLog(`auto-advance switched ${S.autoAdvance ? 'on' : 'off'}`);
    const t = S.selected && S.terms.get(S.selected);
    if (S.mode === 'dev' && t) t.term.focus(); // back to the terminal, not the checkbox
  }
});

// drag a card to another column: same as Shift+Left/Right. Drag an other session onto a
// column: it becomes a task with that prio.
document.addEventListener('dragstart', (e) => {
  const c = e.target.closest && e.target.closest('.card');
  if (!c) return;
  e.dataTransfer.setData('text/plain', c.dataset.other ? `other:${c.dataset.other}` : c.dataset.card);
  S.dragging = true;
});
document.addEventListener('dragend', () => {
  S.dragging = false;
  for (const x of document.querySelectorAll('.column.drop')) x.classList.remove('drop');
  render();
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
  S.dragging = false;
  if (col && id.startsWith('other:')) {
    e.preventDefault();
    adopt(id.slice('other:'.length), col.dataset.prio);
    if (HIDDEN_PRIOS.has(col.dataset.prio) && !S.showHidden) setShowHidden(true);
    return render();
  }
  if (!col || !S.state.tasks[id]) return render();
  e.preventDefault();
  S.state.tasks[id].prio = col.dataset.prio;
  S.boardSel = id;
  send('prio', { id, prio: col.dataset.prio });
  render();
});

// double-click on a card: see the click listener
document.addEventListener('dblclick', (e) => {
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
