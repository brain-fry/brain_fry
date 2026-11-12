'use strict';
// Brain Fry front end. The backend pushes the whole state on /ctl; commands go back on /ctl.
// Each task with a live session has its own xterm.js terminal, fed by /pty/<task id>.

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const KINDS = new Set(['review', 'answer', 'rule', 'FYI', 'wait', 'blocked']);
const PRIOS = ['working', 'planning', 'later', 'more_later', 'never'];
const HIDDEN_PRIOS = new Set(['later', 'more_later', 'never']);
const OUR_KEYS = new Set(['arrowdown', 'arrowup', 'n', 't', 'j', 'p', 'd', 'e', 'b', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'g']);

function stored(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function store(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: not remembered */ }
}

const S = {
  state: null,
  mode: 'dev', // or 'organize', or 'review' (review.js)
  selected: null, // the task in Dev mode (4a)
  boardSel: null, // the card under the cursor in Organize mode
  showHidden: stored('showHidden', false),
  starting: null, // task id: select sent, waiting for its session to start
  terms: new Map(), // task id -> {id, el, term, fit, search, gen, ws}
  picker: null, // {mode, items, index, moved}; mode 'new' (Cmd+T: new task) or 'open' (Cmd+P: switch project)
  plan: null, // {project, prio}: the dialog of a new task in Organize mode (a card, no session)
  planDraft: null, // {title, notes}: what that dialog held when it was closed without creating: back at the next N
  planned: null, // task id: plan answered, the cursor goes to its card when it is in the state
  dragging: false, // no re-render of the board while a card is dragged
  autoAdvance: stored('autoAdvance', true), // after an answer, select the next inbox item (the header's switch)
  advance: null, // {from, order, at, timer}: an advance waiting for its second
  lastAnswered: null, // the task that the last advance (answer or archive) left: Cmd+Up from the first item goes back
  project: null, // the project picked in the dropdown when it had no task to land on (path)
  idle: null, // task id: nothing is selected (4a: a photo), 2a and column 3 still show this task's tabs (showIdle)
  shownTerm: null, // the task whose terminal 4a shows (showTerm), or null: the photo or the overlay
  instant: false, // 4a swaps at once in this frame, nothing fades (swapAtOnce)
};

// the task that the header's actions apply to
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
    swapAtOnce(); // no fade in 4a: neither the old task (or the photo) nor the new one
    return select(st.selected, false);
  }
  if (S.starting && st.tasks[S.starting] && (st.tasks[S.starting].pty || st.tasks[S.starting].outside)) S.starting = null;
  if (S.planned && st.tasks[S.planned]) { S.boardSel = S.planned; S.planned = null; } // a new card (Organize mode)
  if (selectArrival()) return; // it renders
  if (!prev && !S.selected && !S.project) showIdle(lastOpenedAny()); // at start, nothing needs me
  if (S.idle && !st.tasks[S.idle]) S.idle = null; // deleted: 2a and column 3 empty
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
  // a hidden terminal (Organize mode) must never type into Claude: drop its input. In Review
  // mode, 4a types while it is on screen (Cmd+4); the shells of 3b are hidden there
  const live = () => S.mode === 'dev' || (!shell && S.mode === 'review' && S.reviewAgent);
  term.onData((d) => { if (live()) { t.lastInput = Date.now(); wsSend(t, { t: 'in', d }); } });
  term.onBinary((d) => { if (live()) wsSend(t, { t: 'bin', d }); });
  term.onResize(({ cols, rows }) => wsSend(t, { t: 'resize', cols, rows }));
  term.attachCustomKeyEventHandler((e) => {
    if (!live()) return false;
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

// ------------------------------------------------------------------ 4a's fades (DESIGN_DETAILS.md, "auto-advance")
// A change of what 4a shows in Dev mode (a select, an advance, an archive, the photo): the old
// content fades out, 4a stays empty for a moment, then the new one fades in. The photo fades in
// each time it appears, at start too. Renaud chose them from a mockup on 2026-11-11.
const FADE_OUT_MS = 250;
const FADE_PAUSE_MS = 500;
const FADE_IN_MS = 1000; // ease-in
const PHOTO_IN_MS = 1000; // ease-in-out
let fadeAt = -1e9; // the start of the last faded switch: what fades in waits for its fade out and pause

// no fade where 4a was not on screen (Organize and Review mode), nor in a frame of swapAtOnce
const fades = () => S.mode === 'dev' && !S.instant;

// The changes of 4a in this frame are instant: the switch into Dev mode (4a was not on screen),
// and a new task or a fork that I just created (Renaud, 2026-11-11: "there should be no fade-in
// for Claude Code"; the fade is for moving from one inbox item to the next)
function swapAtOnce() {
  S.instant = true;
  requestAnimationFrame(() => { S.instant = false; });
}
// 4a shows something: a terminal, the photo or the overlay (nothing before the first select)
const agentShows = () => (S.shownTerm !== null && S.terms.has(S.shownTerm)) || !$('#calm').hidden || !$('#overlay').hidden;

function fadeOut(el, done) {
  if (el.fadingOut) return;
  const from = +getComputedStyle(el).opacity; // a fade in that was running: from where it is
  el.getAnimations().forEach((a) => a.cancel());
  el.style.pointerEvents = 'none';
  const a = el.animate([{ opacity: from }, { opacity: 0 }], { duration: FADE_OUT_MS, easing: 'ease-in', fill: 'forwards' });
  el.fadingOut = a;
  // the promise, not onfinish: a.finish() resolves it at once also in a hidden page (selftests); cancel rejects it
  a.finished.then(() => { el.fadingOut = null; done(); a.cancel(); el.style.pointerEvents = ''; }, () => {});
}

// after the fade out and the pause of the last switch; from where it is when it was fading
function fadeIn(el, ms, easing) {
  const from = el.getAnimations().length ? +getComputedStyle(el).opacity : 0;
  stopFade(el);
  const delay = Math.max(0, fadeAt + FADE_OUT_MS + FADE_PAUSE_MS - performance.now());
  el.animate([{ opacity: from }, { opacity: 1 }], { duration: ms, delay, easing, fill: 'backwards' });
}

function stopFade(el) {
  el.getAnimations().forEach((a) => a.cancel());
  el.fadingOut = null;
  el.style.pointerEvents = '';
}

// The card of a switch that fades (auto-advance, a click or a key in 1a, an arrival): the next
// task's title, the goal line of its digest and the start of my first prompt, over the top of 4a
// from the pause on, until my first key, click or scroll (Renaud, 2026-11-11, from a mockup: "4B"
// plus the first prompt). With auto-advance my eyes stay on 4a and never see 1a.
const CARD_IN_MS = 300; // ease-out, when 4a is empty
const CARD_OUT_MS = 600; // ease-in, at my first key
let cardGen = 0; // a newer show or hide wins over the end of an older fade
let cardAt = 0; // the events from before (the key or click that made the switch) do not close it

function showCard(id) {
  const el = $('#switch-card');
  const task = S.state && S.state.tasks[id];
  if (!task) return hideCard(0);
  const gen = ++cardGen;
  cardAt = performance.now();
  const put = () => {
    if (gen !== cardGen) return;
    const goal = task.goal ? `<div class="txt"><span class="lb">Goal</span> ${esc(task.goal)}</div>` : '';
    const asked = task.firstPrompt ? `<div class="txt asked"><span class="lb">Asked</span> ${esc(task.firstPrompt)}</div>` : '';
    el.innerHTML = `<div class="head"><span class="dot ${task.status}"></span><span>${esc(task.title)}</span></div>${goal}${asked}`;
    el.hidden = false;
    const delay = Math.max(0, fadeAt + FADE_OUT_MS - performance.now());
    el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: CARD_IN_MS, delay, easing: 'ease-out', fill: 'backwards' });
  };
  if (el.hidden) put();
  else fadeCard(el, FADE_OUT_MS, put); // the old card goes with the old task
}

function hideCard(ms) { // 0: at once
  const el = $('#switch-card');
  ++cardGen;
  if (el.hidden) return;
  if (ms) return fadeCard(el, ms, () => {});
  el.getAnimations().forEach((a) => a.cancel());
  el.hidden = true;
}

function fadeCard(el, ms, then) {
  const gen = cardGen;
  const from = +getComputedStyle(el).opacity;
  el.getAnimations().forEach((a) => a.cancel()); // rejects the promise of an older fade: its `then` never runs
  const a = el.animate([{ opacity: from }, { opacity: 0 }], { duration: ms, easing: 'ease-in', fill: 'forwards' });
  a.finished.then(() => { if (gen === cardGen) el.hidden = true; a.cancel(); then(); }, () => {});
}

// my first key (not a modifier alone), click or scroll after the card came: in any part of the window
function closeCard(e) {
  if ($('#switch-card').hidden || e.timeStamp < cardAt) return;
  if (e.type === 'keydown' && ['Shift', 'Meta', 'Control', 'Alt', 'CapsLock'].includes(e.key)) return;
  hideCard(CARD_OUT_MS);
}
window.addEventListener('keydown', closeCard, true);
window.addEventListener('mousedown', closeCard, true);
$('#agent').addEventListener('wheel', closeCard, { capture: true, passive: true });

// The new terminal is on screen at opacity 0 from the start of the switch, so that it has the
// keyboard at once: a key typed during the fade goes to the selected task, never to the old one.
function showTerm(id) {
  const fade = fades() && agentShows();
  const prev = S.shownTerm;
  S.shownTerm = id;
  if (fade && prev === id) return focusTerm(id); // the same task again: its fade runs on
  if (fade) fadeAt = performance.now();
  if (fade && id) showCard(id);
  else hideCard(fade ? FADE_OUT_MS : 0); // the photo, a project with no task: it goes with the old task
  for (const x of S.terms.values()) {
    if (x.shell) continue;
    if (x.id === id) {
      x.el.style.display = '';
      if (fade) fadeIn(x.el, FADE_IN_MS, 'ease-in');
      else stopFade(x.el);
    } else if (fade && (x.id === prev || x.el.fadingOut)) {
      fadeOut(x.el, () => { x.el.style.display = 'none'; });
    } else {
      stopFade(x.el);
      x.el.style.display = 'none';
    }
  }
  if (fade) fadeIn($('#overlay'), FADE_IN_MS, 'ease-in'); // "No live session"
  else stopFade($('#overlay')); // the fade in of a switch just before
  focusTerm(id);
}

function focusTerm(id) {
  const t = S.terms.get(id);
  if (t && (S.mode === 'dev' || (S.mode === 'review' && S.reviewAgent))) requestAnimationFrame(() => { fitSafely(t); t.term.focus(); });
}

new ResizeObserver(() => {
  const t = S.selected && S.terms.get(S.selected);
  if (t) requestAnimationFrame(() => fitSafely(t));
}).observe($('#agent'));

// ------------------------------------------------------------------ modes

function setMode(mode, doRender = true) {
  if (mode === S.mode) return;
  const prev = S.mode;
  if (prev === 'dev') { // 4a keeps this width in Review mode, so that its PTY keeps its size
    const w = $('#agent').getBoundingClientRect().width;
    if (w > 100) S.agentWidth = Math.round(w);
  }
  S.mode = mode;
  if (mode === 'dev') swapAtOnce(); // the selects that follow in this frame swap 4a at once (showTerm)
  $('#dev').hidden = mode === 'organize'; // Review mode shows its panels in #dev, beside 4a
  $('#board').hidden = mode !== 'organize';
  if (prev === 'review') leaveReview();
  applyReviewLayout(); // review.js
  if (mode === 'organize') {
    if (document.activeElement) document.activeElement.blur(); // out of the terminal
    S.boardSel = S.boardSel && S.state && S.state.tasks[S.boardSel] ? S.boardSel : S.selected;
    $('#board').focus();
  } else if (mode === 'review') {
    enterReview(prev);
  } else if (S.selected) {
    showTerm(S.selected);
  }
  // doRender false: the caller selects a task itself (openInDev, a new task, a project)
  if (doRender && !selectArrival()) render();
}

function openInDev(id) {
  setMode('dev', false);
  select(id);
}

// ------------------------------------------------------------------ project dropdown (header)
// Picking a project lands in Dev mode on the task of that project that I opened last
// (`opened`, set by the backend at each select), with its files and terminals.

function currentProject() {
  const task = S.state && S.state.tasks[S.selected || S.idle];
  return task ? task.projectPath : S.project;
}

// What 2a and column 3 show: the selected task's files and tabs; with no task selected, those of
// the task that I left (the idle screen, showIdle), or those of the project picked in the dropdown
// (it has no open task): `project:<path>`, a work state of files.js that no task file keeps. The
// backend reads the same id (fs.* commands, /file/).
function wsId() {
  return S.selected || S.idle || (S.project ? projectWs(S.project) : null);
}

function wsProject(id) {
  if (id.startsWith('project:')) return id.slice('project:'.length);
  const t = S.state && S.state.tasks[id];
  return t ? t.projectPath : null;
}

// Project tabs (DESIGN_DETAILS.md, "Project tabs"): tabs that belong to a project rather than to
// one task. Their owner is `project:<path>`, the work state of the project view (files.js), and
// every view of that project shows them, first in each pane: a task, the idle screen, the project
// view. A tab's `tid` is its owner: a task, or `project:<path>`. The backend keeps them in
// <project>/.brain_fry/project.json, and the state lists them under the project (`tabs`).
const projectWs = (path) => `project:${path}`;
const isProjectWs = (id) => Boolean(id) && id.startsWith('project:');

// the owners whose tabs are on screen: the view (wsId) and, for a task, its project's tabs
function viewIds() {
  const id = wsId();
  const p = id && !isProjectWs(id) && wsProject(id);
  return !id ? [] : p ? [id, projectWs(p)] : [id];
}
const inView = (tid) => viewIds().includes(tid);

// Where a tab that I open from the view `tid` goes: the selected task's own tabs; with no task
// selected (the idle screen, the project view), the project's tabs (Renaud, 2026-11-11).
function ownerFor(tid) {
  if (!tid || tid === S.selected) return tid;
  const p = wsProject(tid);
  return p ? projectWs(p) : tid;
}

// What the state says of an owner's tabs (openFiles, browserTabs, finderTabs, shells, terminalTabs).
function ownerData(tid) {
  if (!S.state || !tid) return null;
  if (!isProjectWs(tid)) return S.state.tasks[tid] || null;
  const p = S.state.projects.find((x) => x.path === wsProject(tid));
  return (p && p.tabs) || null;
}
const shellsOf = (tid) => (ownerData(tid) || {}).shells || [];

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
  leaveIdle();
  S.project = path;
  S.selected = null;
  S.starting = null;
  showTerm(null);
  send('select', { id: null });
  showProjectTabs(projectWs(path));
  render();
}

// The project's tabs, once per run, as a task's at its first select: its files, pages and Finder
// tabs here (restoreFiles), its terminals by the backend (`idle` with the project's id).
function showProjectTabs(id) {
  restoreFiles(id); // files.js
  const sh = shellBoxSize();
  send('idle', { id, shellRows: sh.rows, shellCols: sh.cols });
}

// the projects of the dropdown, Cmd+P and Cmd+T: without the ones removed with the x of Cmd+P
// (`"active": false` in projects.json; bf init in the folder puts one back)
function activeProjects(projects) {
  return projects.filter((p) => p.active !== false);
}

function renderProjectSelect(st) {
  const el = $('#project-select');
  if (document.activeElement === el) return; // its menu may be open
  const cur = currentProject() || '';
  // a removed project stays in the dropdown while it is the current one, so that it shows
  const shown = st.projects.filter((p) => p.active !== false || p.path === cur);
  const count = {};
  for (const p of shown) count[p.name] = (count[p.name] || 0) + 1;
  // two folders with one name: add the parent folder
  const label = (p) => (count[p.name] > 1 ? `${p.name} (${p.path.split('/').slice(-2, -1)[0]})` : p.name);
  const html = (cur ? '' : '<option value="" disabled selected>Project</option>')
    + shown.map((p) => `<option value="${esc(p.path)}" ${p.path === cur ? 'selected' : ''}>${esc(label(p))}</option>`).join('');
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
  if (cur) el.value = cur;
  else el.selectedIndex = 0; // the placeholder
}

// ------------------------------------------------------------------ selection and actions

function select(id, notify = true) {
  if (S.advance && S.advance.from !== id) cancelAdvance();
  leaveIdle();
  S.selected = id;
  S.project = null; // the dropdown follows the task's project
  const t = ensureTerm(id);
  showTerm(id);
  const task = S.state && S.state.tasks[id];
  if (task && !task.pty && !task.outside) S.starting = id;
  if (task && S.mode === 'dev' && !work(id).restored) veil(id); // its tabs fade in once all built (veil.js)
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
  if (!S.autoAdvance) return uiLog(`auto-advance off: stays on ${m.id.slice(0, 8)}`);
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
  uiLog(`auto-advance${a.why ? ` (${a.why})` : ''}: ${a.from.slice(0, 8)} --> ${next ? `${next.slice(0, 8)} ${JSON.stringify(S.state.tasks[next].title)}` : 'nothing needs me'}`);
  S.lastAnswered = a.from;
  if (next) return select(next);
  selectNone(); // the answered task leaves 4a also when nothing else needs me (Renaud, 2026-11-10)
}

// Nothing needs me, after an answer or an archive: no task is selected, 4a shows a calm photo.
// Cmd+Up goes back to the task that I left.
function selectNone() {
  cancelAdvance();
  const left = S.selected;
  S.selected = null;
  S.starting = null;
  S.project = null;
  showTerm(null);
  send('select', { id: null });
  showIdle(left);
  render();
}

// The idle screen (Renaud, 2026-11-11): while nothing is selected, only 4a changes (the photo).
// The dropdown, 2a and column 3 keep showing the task that I left, or at start the task that I
// opened last: its project and its layout, terminals and browser tabs included. That task is not
// selected: no session is started for it, `opened` does not change. A tab opened there is not
// added to it: it is a project tab (ownerFor), which every task of the project shows.
function showIdle(id) {
  S.idle = id && S.state && S.state.tasks[id] ? id : null;
  if (!S.idle) return;
  if (S.mode === 'dev' && !work(id).restored) veil(id); // at start: its tabs fade in once built (veil.js)
  restoreFiles(id); // files.js: its file and browser tabs and its project's, once per run, as at a select
  const sh = shellBoxSize();
  send('idle', { id, shellRows: sh.rows, shellCols: sh.cols }); // the backend starts its terminals and its project's, once per run
}

function leaveIdle() {
  S.idle = null;
}

// the task that I opened last, in any project, archived or not
function lastOpenedAny() {
  let best = null;
  for (const t of Object.values(S.state.tasks)) if (t.opened && (!best || Date.parse(t.opened) > Date.parse(best.opened))) best = t;
  return best ? best.id : null;
}

// The calm photo shows only while nothing needs me (Renaud, 2026-11-10): in Dev mode with no task
// selected, the first inbox item that needs me (a reply or a block) is selected at once. Checked at
// each state push and when I switch to Dev mode. Not while Cmd+T, Cmd+P or a plan dialog is open:
// the selection would take the keyboard focus from it. Returns true when it selected.
function selectArrival() {
  if (!S.state || S.mode !== 'dev' || S.selected || S.project || S.picker || S.plan) return false;
  const next = S.state.inbox.find((id) => S.state.tasks[id] && S.state.tasks[id].inbox);
  if (!next) return false;
  uiLog(`inbox: nothing was selected --> ${next.slice(0, 8)} ${JSON.stringify(S.state.tasks[next].title)}`);
  select(next);
  return true;
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
// item that needs me is selected, with no second to wait, whatever the auto-advance switch
// (that switch is about staying on a task after an answer). Nothing needs me: 4a empties.
// Cmd+Up goes back to the archived task, to unarchive it.
function afterArchive(id, next) {
  S.lastAnswered = id;
  S.state.inbox = S.state.inbox.filter((x) => x !== id); // no flash of its row before the state push
  uiLog(`archive: ${id.slice(0, 8)} --> ${next ? `${next.slice(0, 8)} ${JSON.stringify(S.state.tasks[next].title)}` : 'nothing needs me'}`);
  if (next) return select(next);
  selectNone();
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

// pos: the card's position in the working column (1 to 9), the digit that opens it; 0 for none
function card(t, pos = 0) {
  const cls = ['card', t.id === S.boardSel ? 'sel' : '', t.status === 'done' ? 'archived' : '', t.unread ? 'unread' : ''].join(' ');
  return `<div class="${cls}" data-card="${t.id}" draggable="true">
    <div class="top">${pos ? `<span class="pos" title="press ${pos} to open">${pos}</span>` : ''}<span class="dot ${t.status}" title="${t.status}"></span>
      ${t.inbox && t.prio === 'working' ? '<span class="inbox-badge">inbox</span>' : ''}${t.pinned ? '<span class="pin-badge">pinned</span>' : ''}<span class="proj" style="--h:${hue(t.project)}">${esc(t.project)}</span></div>
    <div class="title" title="${esc(t.title)}">${esc(t.shortTitle || t.title)}</div>
    ${t.digest || t.notes ? `<div class="desc" title="${esc(t.digest || t.notes)}">${esc(t.digest || t.notes)}</div>` : ''}
    ${t.inbox && t.prio === 'working' && t.line ? `<div class="line need">${esc(t.line)}</div>` : ''}
  </div>`;
}

function ago(iso) {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 90) return 'just now';
  if (s < 90 * 60) return `${Math.round(s / 60)} min ago`;
  if (s < 36 * 3600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
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
  const cur = currentProject(), name = cur && cur.split('/').pop();
  const park = $('#park-btn');
  park.disabled = !parkable(st, cur).length;
  park.title = cur ? `Moves every task of the working column that is not in ${name} to planning: they leave the inbox` : '';
  const cols = boardColumns(st);
  ensureBoardSel(cols);
  $('#columns').innerHTML = cols.map((c) => `
    <div class="column" data-prio="${c.prio}">
      <h3>${c.prio.replace('_', ' ')} <span>${c.ids.length || ''}</span></h3>
      <div class="cards">${c.ids.map((id, i) => card(st.tasks[id], c.prio === 'working' && i < 9 ? i + 1 : 0)).join('')}</div>
    </div>`).join('');
  const sel = document.querySelector('#columns .card.sel');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}

// 1 to 9 in Organize mode: the card at that position in the working column, as shown now
function openWorking(n) {
  const col = boardColumns(S.state).find((c) => c.prio === 'working');
  const id = col && col.ids[n - 1];
  if (!id) return;
  S.boardSel = id; // as Enter: the cursor is on it when I come back to the board
  openInDev(id);
}

// The button of the board bar: the working tasks of the other projects (archived ones too) go to
// planning, so that only the current project's tasks are in the inbox. The backend chooses
// them again (actions.park_others); this list only enables the button.
function parkable(st, cur) {
  return cur ? Object.values(st.tasks).filter((t) => t.prio === 'working' && t.projectPath !== cur) : [];
}

async function parkOthers() {
  const cur = currentProject();
  if (!cur) return;
  try {
    const { ids } = await request('park', { project: cur });
    if (!ids.length) return;
    toast(`Moved ${ids.length} task${ids.length > 1 ? 's' : ''} to planning`, {
      label: 'Undo', run: () => ids.forEach((id) => send('prio', { id, prio: 'working' })),
    });
  } catch (e) {
    toast(`Could not move the tasks: ${e.message}`);
  }
}

function setShowHidden(on) {
  S.showHidden = on;
  store('showHidden', on);
  render();
}

// cards per row: planning shows two (app.css), in reading order
const perRow = (col) => (col.prio === 'planning' ? 2 : 1);

// The arrows follow the screen: in planning, Left/Right first go to the other card of the row,
// Up/Down go a whole row. Across columns, the cursor keeps its row.
function boardMove(dx, dy) {
  const cols = boardColumns(S.state);
  let ci = cols.findIndex((c) => c.ids.includes(S.boardSel));
  if (ci < 0) return render();
  let ri = cols[ci].ids.indexOf(S.boardSel);
  const n = perRow(cols[ci]), len = cols[ci].ids.length;
  if (dx) {
    const side = (ri % n) + dx;
    if (side >= 0 && side < n && ri + dx < len) ri += dx; // the other card of the row
    else {
      const row = Math.floor(ri / n);
      let j = ci + dx;
      while (j >= 0 && j < cols.length && !cols[j].ids.length) j += dx; // skip empty columns
      if (j < 0 || j >= cols.length) return;
      ci = j;
      const m = perRow(cols[ci]); // from the left: the row's first card; from the right: its last
      ri = Math.min(row * m + (dx > 0 ? 0 : m - 1), cols[ci].ids.length - 1);
    }
  } else {
    const next = ri + dy * n;
    if (next < 0) return;
    if (next >= len) { // the last row may hold one card: go there, else stay
      if (Math.floor((len - 1) / n) === Math.floor(ri / n)) return;
      ri = len - 1;
    } else ri = next;
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
  if (S.mode === 'organize') ensureBoardSel(boardColumns(st));
  renderBanner(st);
  renderProjectSelect(st);
  renderHead(st);
  $('#auto-advance').checked = S.autoAdvance;
  if (S.mode === 'organize') return renderBoard(st);
  if (S.mode === 'review') { renderAgent(st); return renderReview(st); } // review.js
  renderInbox(st);
  renderThreads(st);
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
  // only the actions: the task itself is on its row in 1a or its card (Renaud, 2026-11-10)
  head.innerHTML = `
    <span class="actions">
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
  // An empty inbox: only its heading, no text, and no Threads section (Renaud, 2026-11-10)
  $('#inbox').innerHTML = ids.map((id) => row(st.tasks[id], st.inbox.includes(id) ? '' : 'current')).join('');
}

function renderThreads(st) {
  const task = st.tasks[S.selected];
  $('#threads-section').hidden = !task; // no task selected, which an empty inbox implies
  if (!task) return;
  const threads = [...task.threads].reverse();
  $('#threads').innerHTML = threads.length
    ? threads.map((th) => {
      const kind = KINDS.has(th.kind) ? `<span class="kind ${th.kind}">${th.kind}</span>` : th.kind; // working, interrupted, pending, imported
      const txt = th.digest || th.prompt || '(no prompt)';
      return `<div class="thread" data-n="${th.n}" title="${esc(th.prompt)} (click: open in 3a)"><span class="n">${th.n}</span>${kind}<span class="txt">${esc(txt)}</span></div>`;
    }).join('')
    : task.notes ? `<div class="notes"><div class="label">My notes (no thread yet)</div>${esc(task.notes)}</div>`
    : '';
}

function renderAgent(st) {
  const task = st.tasks[S.selected];
  calm(!S.selected && !S.project);
  if (!S.selected && S.project) {
    const name = S.project.split('/').pop();
    return overlay(`<div class="big">No open task in ${esc(name)}.</div><button data-new-in="${esc(S.project)}">New task in ${esc(name)}</button>`);
  }
  if (!S.selected) return overlay(null);
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
  if (S.starting === task.id) return overlay(null);
  overlay(`<div class="big">No live session.</div><button data-act="resume">Resume</button>`);
}

// The empty 4a (no task selected): a calm photo, no text, nothing to watch or start (Renaud,
// 2026-11-10). A different photo each time it appears. Credits: web/calm/CREDITS.md.
const CALM_PHOTOS = ['beech-forest-fog', 'lone-tree-fog', 'tree-mist-reflection', 'snowy-lake-fog',
  'snowfield-mist', 'misty-hills', 'peak-in-cloud', 'argentine-ridge', 'diablerets-summit',
  'diablerets-rampart', 'creux-de-champ'];

function calm(on) {
  const el = $('#calm');
  if (on && (el.hidden || el.fadingOut)) { // fading out: back, with the same photo
    if (el.hidden) {
      const others = CALM_PHOTOS.filter((p) => p !== el.dataset.photo);
      el.dataset.photo = others[Math.floor(Math.random() * others.length)];
      el.style.backgroundImage = `url(calm/${el.dataset.photo}.jpg)`;
      el.hidden = false;
    }
    fadeIn(el, PHOTO_IN_MS, 'ease-in-out'); // after the task's fade out and the pause (showTerm)
  } else if (!on && !el.hidden) {
    if (fades()) fadeOut(el, () => { el.hidden = true; });
    else { stopFade(el); el.hidden = true; }
  }
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

// Cmd+T (new task) and Cmd+P (switch project; picking one does what the dropdown does) show the
// list of the dropdown: the most recently used project first (views.project_order). Enter opens
// the highlighted one, the digits 1 to 9 open the n-th one. Cmd+T highlights the first project,
// Cmd+P the first one that is not the current one: the current project is usually first, so
// Cmd+P, Enter goes back to the project before it.
function openPicker(mode = 'new') {
  S.picker = { mode, items: S.state ? activeProjects(S.state.projects) : [], index: 0, moved: false };
  S.picker.index = pickerStart(S.picker);
  renderPicker();
  $('#picker').hidden = false;
  // projects.json may have changed since the last state (bf init in a terminal): ask again
  request('projects').then((items) => {
    const p = S.picker;
    if (!p || p.mode !== mode) return; // closed, or the other picker opened since
    const current = p.items[p.index];
    p.items = activeProjects(items);
    p.index = p.moved && current ? Math.max(0, items.findIndex((it) => it.path === current.path)) : pickerStart(p);
    renderPicker();
  }).catch((e) => toast(`Could not read the projects: ${e.message}`));
}

// File > Add New Project... (main.py: macOS's folder dialog): bf init on the folder, then switch
// to it as Cmd+P does. A folder that is already a project: a toast, then the switch.
function addProject(path) {
  if (S.picker) closePicker();
  request('project.init', { path }).then((r) => {
    if (!r.new) toast(`${r.name} is already a project`);
    openProject(r.path);
  }).catch((e) => toast(`Could not add ${path}: ${e.message}`));
}

function pickerStart(p) {
  const i = p.mode === 'open' ? p.items.findIndex((it) => it.path !== currentProject()) : 0;
  return Math.max(0, i);
}

function closePicker() {
  if (S.plan) { // Esc or a click outside: the text comes back at the next N, a long note is not lost
    const title = $('#picker .plan-title').value, notes = $('#picker .plan-notes').value;
    S.planDraft = title.trim() || notes.trim() ? { title, notes } : null;
  }
  $('#picker').hidden = true;
  S.picker = null;
  S.plan = null;
  const t = S.selected && S.terms.get(S.selected);
  if (S.mode === 'organize') $('#board').focus();
  else if (t) t.term.focus();
}

function renderPicker() {
  const p = S.picker;
  const key = (i) => (i === p.index ? 'Enter' : i < 9 ? String(i + 1) : '');
  // Cmd+P only: the x removes the project from the lists (shown on hover and on the highlighted row, as in VS Code)
  const remove = (i) => (p.mode === 'open'
    ? `<button class="remove" data-remove="${i}" title="Remove from this list (bf init in the folder puts it back)">×</button>` : '');
  const planned = p.mode === 'new' && S.mode === 'organize' ? 'on the board, not started · ' : '';
  const head = `${p.mode === 'open' ? 'Switch to project...' : 'New task in project...'} <span class="hint">${planned}most recently used first</span>`;
  const use = (it) => (it.path === currentProject() ? '<span class="recent">current</span>' : '')
    + (it.last ? `<span class="recent" title="last opened task of this project">${ago(it.last)}</span>` : '');
  $('#picker').innerHTML = `<div class="box"><h3>${head}</h3>${p.items.length
    ? p.items.map((it, i) => `<div class="item ${i === p.index ? 'sel' : ''}" data-i="${i}">
        <div class="main"><div>${esc(it.name)}</div><div class="path">${esc(it.path)}</div></div>
        ${use(it)}
        ${key(i) ? `<kbd>${key(i)}</kbd>` : ''}${remove(i)}</div>`).join('')
    : ''}</div>`;
}

function removeProject(i) {
  const p = S.picker;
  const it = p && p.items[i];
  if (!it) return;
  p.items.splice(i, 1);
  if (i < p.index || p.index >= p.items.length) p.index = Math.max(0, p.index - 1);
  renderPicker();
  request('project.deactivate', { path: it.path }).catch((e) => toast(`Could not remove ${it.name}: ${e.message}`));
}

function pick(i) {
  const it = S.picker && S.picker.items[i];
  const mode = S.picker && S.picker.mode;
  closePicker();
  if (it && mode === 'open') openProject(it.path);
  else if (it && S.mode === 'organize') openPlan(it.path);
  else if (it) newTask(it.path);
}

// ------------------------------------------------------------------ new task in Organize mode
// Organize mode is for organizing: a new task is only a card, no session (Renaud, 2026-11-10).
// After the project, a title and notes. The card lands in the column of the card under the
// cursor (working on an empty board) and the cursor moves to it. Opening it in Dev mode starts
// its session (`claude --session-id`, as for any task that was never prompted).
function openPlan(projectPath) {
  const cur = S.state.tasks[S.boardSel];
  S.plan = { project: projectPath, prio: cur ? cur.prio : 'working' };
  const d = S.planDraft || { title: '', notes: '' };
  $('#picker').innerHTML = `<div class="box"><h3>New task in ${esc(projectPath.split('/').pop())} · ${esc(S.plan.prio.replace('_', ' '))}
    <span class="hint">on the board, not started</span></h3>
    <input class="plan-title" placeholder="Title" spellcheck="false" value="${esc(d.title)}">
    <textarea class="plan-notes" placeholder="Notes" rows="8">${esc(d.notes)}</textarea>
    <div class="plan-keys">Enter: to the notes · Cmd+Enter: create · Esc: close (the text is kept)</div></div>`;
  $('#picker').hidden = false;
  const title = $('#picker .plan-title'), notes = $('#picker .plan-notes');
  const onKey = (e) => { // the document's handler leaves a text field its keys
    if (e.isComposing) return;
    if (e.key === 'Enter' && e.metaKey) { e.preventDefault(); submitPlan(); }
    else if (e.key === 'Enter' && e.target === title) { e.preventDefault(); notes.focus(); }
    else if (e.key === 'Escape') { e.preventDefault(); closePicker(); }
  };
  title.addEventListener('keydown', onKey);
  notes.addEventListener('keydown', onKey);
  title.focus();
}

function submitPlan() {
  const p = S.plan;
  if (!p) return;
  const titleEl = $('#picker .plan-title');
  const title = titleEl.value.trim(), notes = $('#picker .plan-notes').value.trim();
  if (!title) return titleEl.focus(); // the card needs a heading
  S.plan = null; // created: no draft to keep
  S.planDraft = null;
  closePicker();
  request('plan', { project: p.project, title, prio: p.prio, notes }).then((r) => {
    if (S.state.tasks[r.id]) { S.boardSel = r.id; render(); } else S.planned = r.id; // else onState moves the cursor
  }).catch((e) => {
    S.planDraft = { title, notes }; // back at the next N
    toast(`Could not create the task: ${e.message}`);
  });
}

// ------------------------------------------------------------------ events

document.addEventListener('keydown', (e) => {
  // Cmd+N, then T, B or F, then a digit: a terminal, a page or a new file in that pane (grid.js).
  // Cmd+J, then a digit: the keyboard to that pane.
  // First, also before a text field: while the chord waits, no key may reach a field or a terminal
  if (chordKey(e)) { e.preventDefault(); e.stopImmediatePropagation(); return; }
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
    else if (/^[1-9]$/.test(k)) openWorking(Number(k));
    else if (k === 'Escape') setMode('dev');
    else if (k === 'h' || k === 'H') setShowHidden(!S.showHidden);
    else if (k === 'e') act('archive');
    else if (k === 'f') act('fork');
    else if (k === 'n') openPicker();
    // any other key is swallowed too: nothing typed in Organize mode may reach a terminal
    e.preventDefault(); e.stopPropagation();
    return;
  }
  if (S.mode === 'review' && reviewKey(e)) { e.preventDefault(); e.stopPropagation(); return; } // review.js
  if (!e.metaKey) return;
  const k = e.key.toLowerCase();
  // in a code editor, the editor keeps Cmd+D (next occurrence) and Cmd+Up/Down (start/end)
  if (e.target.closest && e.target.closest('.cm-editor') && ['d', 'arrowup', 'arrowdown'].includes(k)) return;
  // a Finder tab keeps Cmd+Up (enclosing folder) and Cmd+Down (open), as Finder (finder.js)
  if (e.target.closest && e.target.closest('.finder') && ['arrowup', 'arrowdown'].includes(k)) return;
  if (k === 'g' && e.shiftKey) setMode(S.mode === 'review' ? R.from : 'review'); // Review mode, and back
  else if (k === 'b') setMode(S.mode === 'organize' ? 'dev' : 'organize');
  else if (/^[1-9]$/.test(k) && S.mode === 'dev') showTabAt(Number(k)); // grid.js
  else if (k === 'arrowdown' && S.mode === 'dev') move(1);
  else if (k === 'arrowup' && S.mode === 'dev') move(-1);
  else if (k === 't') openPicker(); // a new task (Cmd+N creates in a pane since 2026-11-10: grid.js)
  else if (k === 'p') openPicker('open');
  else if (k === 'd' && e.shiftKey) act('fork');
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
  const cardEl = e.target.closest('.card');
  if (cardEl && e.detail === 2) return openInDev(cardEl.dataset.card);
  if (cardEl) { S.boardSel = cardEl.dataset.card; $('#board').focus(); return render(); }
  const rowEl = e.target.closest('.list .row');
  if (rowEl) return select(rowEl.dataset.id);
  const newIn = e.target.closest('[data-new-in]');
  if (newIn) return newTask(newIn.dataset.newIn);
  const btn = e.target.closest('[data-act]');
  if (btn && !btn.disabled) return act(btn.dataset.act);
  const removeBtn = e.target.closest('#picker [data-remove]');
  if (removeBtn) return removeProject(Number(removeBtn.dataset.remove));
  const item = e.target.closest('#picker .item');
  if (item) return pick(Number(item.dataset.i));
  if (e.target.id === 'picker') return closePicker();
});

$('#new-btn').addEventListener('click', () => openPicker());
$('#park-btn').addEventListener('click', () => { parkOthers(); $('#board').focus(); }); // the board keys keep working

document.addEventListener('change', (e) => {
  if (e.target.id === 'show-hidden') { setShowHidden(e.target.checked); $('#board').focus(); }
  if (e.target.id === 'auto-advance') {
    S.autoAdvance = e.target.checked;
    store('autoAdvance', S.autoAdvance);
    if (!S.autoAdvance) cancelAdvance();
    uiLog(`auto-advance switched ${S.autoAdvance ? 'on' : 'off'}`);
    const t = S.mode === 'dev' && S.selected && S.terms.get(S.selected);
    if (t) t.term.focus(); // the keys go back to the selected task's terminal
  }
  if (e.target.id === 'project-select') {
    e.target.blur(); // the selected task's terminal takes the focus
    if (e.target.value) openProject(e.target.value);
  }
});

// drag a card to another column: same as Shift+Left/Right
document.addEventListener('dragstart', (e) => {
  const c = e.target.closest && e.target.closest('.card');
  if (!c) return;
  e.dataTransfer.setData('text/plain', c.dataset.card);
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
  if (!col || !S.state.tasks[id]) return render();
  e.preventDefault();
  S.state.tasks[id].prio = col.dataset.prio;
  S.boardSel = id;
  send('prio', { id, prio: col.dataset.prio });
  render();
});

connectCtl();
