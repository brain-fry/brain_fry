'use strict';
// Column 3: a grid of up to four panes, numbered as the rest of the layout (DESIGN.md): 1 top
// left (the main pane), 2 top right, 3 bottom left, 4 bottom right. Each pane has its own tabs,
// and a tab is a file, a thread, a browser page, a terminal or a Finder tab (finder.js). A pane is on screen only while it
// has tabs; the others give it their space. Everything belongs to the selected task (work(tid),
// files.js). See DESIGN_DETAILS.md, "2a, 3a, 3b".
// - Cmd+N, then T (terminal), B (browser), F (new file) or E (Finder), then a digit 1-4: created in that
//   pane. No digit within 1 s: pane 1. Nothing is created and then moved. Cmd+N O: the list
//   that copies a tab of another task into the pane that I use (copytab.js).
// - Cmd+J (jump), then a digit 1-4: the keyboard goes to that pane; C: to Claude Code (4a); E: to
//   the explorer, whose rows on screen show a key each, 1-9 then a-z (explorer.js). No key within 2 s, or
//   another key: nothing.
// - Cmd+1 to Cmd+9: the n-th tab of the pane that I use (showTabAt).
// - Drag a tab onto another pane: it moves there. Onto the edge of a pane whose neighbour is
//   empty: the neighbour opens with it (to the right of a single column: two columns).
// - The × at the right end of a pane's tab bar: all its tabs close, after one question.
// Uses S, $, store, stored, toast (app.js) and work,
// renderFiles, renderExplorer, focusTab, saveOpenFiles, newUntitled, closeFile, tabName (files.js), openWebTab,
// saveWebTabs (browser.js), newShell (shells.js), at call time.

const PANES = [1, 2, 3, 4];
const CELL = { 1: [0, 0], 2: [0, 1], 3: [1, 0], 4: [1, 1] }; // [row, column]
const CHORD_KIND_MS = 2000; // after Cmd+N or Cmd+J: the next key
const CHORD_PANE_MS = 1000; // after Cmd+N's letter: the digit (Renaud: no hint on screen, just wait)
const EDGE = 0.33; // a drop this close to a pane's edge (share of its size) opens the neighbour
const G = { els: {}, chord: null, press: null, drag: null, untitled: 0 };

function buildGrid() {
  const pane = (n) => `<div class="pane" data-pane="${n}"><div class="tabs pane-tabs"></div><div class="pane-bar"></div>`
    + '<div class="pane-body"></div></div>'; // an empty pane 1 shows nothing: no hint (Renaud, 2026-11-11: "disturbing")
  const mid = $('#middle');
  mid.innerHTML = `<div class="grid-row" data-row="0">${pane(1)}<div class="split v" data-split="gridTop"></div>${pane(2)}</div>`
    + '<div class="split h" data-split="gridRows"></div>'
    + `<div class="grid-row" data-row="1">${pane(3)}<div class="split v" data-split="gridBottom"></div>${pane(4)}</div>`;
  for (const n of PANES) {
    const el = mid.querySelector(`.pane[data-pane="${n}"]`);
    G.els[n] = { pane: el, tabs: el.querySelector('.pane-tabs'), bar: el.querySelector('.pane-bar'), body: el.querySelector('.pane-body') };
    new ResizeObserver(() => fitPaneShell(n)).observe(G.els[n].body);
  }
  G.els.rows = [...mid.querySelectorAll('.grid-row')];
  G.els.splits = { gridTop: mid.querySelector('[data-split="gridTop"]'), gridBottom: mid.querySelector('[data-split="gridBottom"]'),
    gridRows: mid.querySelector('[data-split="gridRows"]') };
}

// ------------------------------------------------------------------ panes of a task

const paneTabs = (w, n) => w.tabs.filter((t) => t.pane === n && !t.closing);
const paneActive = (w, n) => paneTabs(w, n).find((t) => t.path === w.act[n]) || null;

function shownPanes(w) {
  const shown = w ? PANES.filter((n) => paneTabs(w, n).length) : [];
  return shown.length ? shown : [1];
}

function addTab(w, tab, n) {
  tab.pane = n;
  G.els[n].body.appendChild(tab.el);
  w.tabs.push(tab);
}

function usePane(w, n) { // the pane that I used last: Cmd+L and Cmd+W act in it, the explorer opens files in it
  w.pane = n;
  w.mru = [n, ...w.mru.filter((x) => x !== n)];
}

// Where a file, a thread or a page opens: the pane that I used last, unless it shows a terminal
// or a Finder tab (a click in a terminal must not make the next file cover it; a file opened from
// a Finder tab opens next to it).
function openPane(w) {
  const shown = shownPanes(w);
  for (const n of [...w.mru, ...PANES]) {
    if (!shown.includes(n)) continue;
    const a = paneActive(w, n);
    if (!a || !(a.shell || a.finder)) return n;
  }
  return 1;
}

// The tab on screen in its pane, and its pane the one that I use.
function showTab(tab, focus = false) {
  const w = work(tab.tid);
  w.act[tab.pane] = tab.path;
  delete w.want[tab.pane];
  usePane(w, tab.pane);
  saveGrid(tab.tid);
  if (tab.tid !== wsId()) return;
  renderFiles();
  renderExplorer();
  if (focus) focusTab(tab);
}

// Out of its pane (closed, or moved): the tab next to it takes its place on screen.
function leavePane(w, tab) {
  const list = paneTabs(w, tab.pane);
  const i = list.indexOf(tab);
  const rest = list.filter((t) => t !== tab);
  if (w.act[tab.pane] === tab.path) w.act[tab.pane] = rest.length ? rest[Math.min(i, rest.length - 1)].path : null;
}

function dropTab(w, tab) {
  leavePane(w, tab);
  const i = w.tabs.indexOf(tab);
  if (i >= 0) w.tabs.splice(i, 1);
  tab.el.remove();
  if (!paneTabs(w, w.pane).length) usePane(w, w.mru.find((n) => paneTabs(w, n).length) || 1);
}

// To pane n, before the tab `before` (a drop on a tab), or at the end.
function moveTab(tab, n, before = null) {
  const w = work(tab.tid);
  if (tab.pane !== n) {
    leavePane(w, tab);
    tab.pane = n;
    G.els[n].body.appendChild(tab.el);
  }
  w.tabs.splice(w.tabs.indexOf(tab), 1);
  const i = before ? w.tabs.indexOf(before) : -1;
  if (i >= 0) w.tabs.splice(i, 0, tab); else w.tabs.push(tab);
  if (tab.web) saveWebTabs(tab.tid); else if (tab.finder) saveFinderTabs(tab.tid); else if (isFileTab(tab)) saveOpenFiles(tab.tid); // the order is kept
  showTab(tab, true);
}

// The × at the right end of a pane's tab bar: all its tabs close, after one native question
// (Renaud, 2026-11-11; VS Code's "Close All" of an editor group). The question names what is
// lost: unsaved changes (no second question per tab) and terminals, whose shells end.
function closePane(n) {
  const tid = wsId();
  const tabs = tid ? paneTabs(work(tid), n) : [];
  if (!tabs.length) return;
  const lost = [];
  const dirty = tabs.filter(losesEdits).map(tabName); // edits that a tab of another task holds too are kept (files.js)
  if (dirty.length) lost.push(`Unsaved changes to ${dirty.join(', ')} will be lost.`);
  const shells = tabs.filter((t) => t.shell).length;
  if (shells) lost.push(shells === 1 ? 'Its terminal will end.' : `Its ${shells} terminals will end.`);
  const q = tabs.length === 1 ? 'Close the tab of this pane?' : `Close the ${tabs.length} tabs of this pane?`;
  if (!confirm(lost.length ? `${q}\n\n${lost.join(' ')}` : q)) return;
  uiLog(`close pane ${n}: ${tabs.length} tab(s)`);
  for (const t of tabs) closeFile(tid, t.path, false);
}

// Which panes and rows show, and their sizes (fractions of column 3, remembered by the window).
function layoutPanes(w) {
  const shown = shownPanes(w);
  const on = (n) => shown.includes(n);
  const L = S.layout;
  const rows = [on(1) || on(2), on(3) || on(4)];
  G.els.rows[0].hidden = !rows[0];
  G.els.rows[1].hidden = !rows[1];
  const both = rows[0] && rows[1];
  G.els.splits.gridRows.hidden = !both;
  // alone, a pane or a row takes all: a flex-grow below 1 would leave the rest of the space empty
  G.els.rows[0].style.flexGrow = both ? L.gridRows : 1;
  G.els.rows[1].style.flexGrow = both ? 1 - L.gridRows : 1;
  const sides = [[1, 2, 'gridTop'], [3, 4, 'gridBottom']];
  for (const [a, b, key] of sides) {
    const pair = on(a) && on(b);
    G.els[a].pane.hidden = !on(a);
    G.els[b].pane.hidden = !on(b);
    G.els.splits[key].hidden = !pair;
    G.els[a].pane.style.flexGrow = pair ? L[key] : 1;
    G.els[b].pane.style.flexGrow = pair ? 1 - L[key] : 1;
  }
  $('#middle').classList.toggle('multi', shown.length > 1);
  for (const n of PANES) G.els[n].pane.classList.toggle('used', Boolean(w) && w.pane === n);
}

function fitPaneShell(n) {
  const w = wsId() && S.work && S.work[wsId()];
  const t = w && paneActive(w, n);
  if (t && t.shell) requestAnimationFrame(() => fitSafely(t.term));
}

// ------------------------------------------------------------------ the grid of a task, remembered
// By the window (localStorage `grid:<task id>`), as the sizes of the panels: which pane each tab
// is in, the tab on screen in each pane, the pane that I used last. A tab is named by its path; a
// browser tab by its position among the saved ones (`web#0`), a terminal by its position among
// the task's (`shell#0`), a Finder tab by its position among the task's (`finder#0`): their keys
// are new at each start. Threads and untitled files are not kept.

const savedWeb = (t) => t.web && t.url && t.url !== 'about:blank'; // as saveWebTabs (browser.js)

function tabId(t) {
  if (t.shell) {
    const task = S.state && S.state.tasks[t.tid];
    const i = task ? task.shells.findIndex((s) => s.key === t.key) : -1;
    return i >= 0 ? `shell#${i}` : null;
  }
  if (t.web) {
    const i = work(t.tid).tabs.filter(savedWeb).indexOf(t);
    return i >= 0 ? `web#${i}` : null;
  }
  if (t.finder) {
    const i = work(t.tid).tabs.filter((x) => x.finder && !x.idle).indexOf(t); // as saveFinderTabs (finder.js)
    return i >= 0 ? `finder#${i}` : null;
  }
  return t.thread || t.untitled ? null : t.path;
}

function gridRecord(w) {
  const at = {}, act = {};
  for (const t of w.tabs) {
    const id = !t.closing && t.pane !== 1 && tabId(t);
    if (id) at[id] = t.pane;
  }
  for (const n of PANES) {
    const t = paneActive(w, n);
    const id = (t && tabId(t)) || w.want[n];
    if (id) act[n] = id;
  }
  return { pane: w.pane, act, at };
}

function saveGrid(tid) {
  const w = S.work && S.work[tid];
  if (w && w.restored && !w.restoring) store(`grid:${tid}`, gridRecord(w));
}

function loadGrid(tid) {
  const g = stored(`grid:${tid}`, null);
  if (g && g.act && g.at) return g;
  return { pane: 1, act: {}, at: {} };
}

// A new task or a fork starts with the grid of the task whose tabs it copied (copyView, files.js).
function copyGrid(from, to) {
  const src = S.work && S.work[from];
  store(`grid:${to}`, src && src.restored ? gridRecord(src) : loadGrid(from));
}

// The tab on screen in each pane, as remembered, once that tab exists (a terminal comes later).
function applyWant(w) {
  for (const n of Object.keys(w.want)) {
    const t = paneTabs(w, Number(n)).find((x) => tabId(x) === w.want[n]);
    if (t) { w.act[n] = t.path; delete w.want[n]; }
  }
}

// ------------------------------------------------------------------ Cmd+N: create where I say; Cmd+J: jump
// Called first by app.js's key handler; true: the key is used. While the chord waits, every key
// is taken, so that none reaches a terminal or Claude Code.

function chordKey(e) {
  const k = (e.key || '').toLowerCase();
  if (!G.chord) {
    if ((k !== 'n' && k !== 'j') || !e.metaKey || e.shiftKey || e.altKey || e.ctrlKey || S.picker || S.mode !== 'dev') return false;
    G.chord = k === 'n' ? { kind: null, timer: setTimeout(() => endChord(), CHORD_KIND_MS) } : { kind: 'jump', timer: jumpTimer() };
    return true;
  }
  if (['meta', 'shift', 'alt', 'control', 'capslock'].includes(k)) return false; // a modifier alone: still waiting
  const c = G.chord;
  if (k === 'escape') { endChord(c.kind === 'jump' || c.kind === 'tree' ? 'jump: Esc' : ''); return true; }
  if (c.kind === 'jump') { // no default, unlike Cmd+N (Renaud)
    if (k === 'j' && e.metaKey) { clearTimeout(c.timer); c.timer = jumpTimer(); } // pressed again: wait again
    else if (/^[1-4]$/.test(k)) jumpTo(Number(k));
    else if (k === 'c') jumpToClaude();
    else if (k === 'e') startTreeHints(); // explorer.js
    else endChord(`jump: cancelled by the key ${k}`);
    return true;
  }
  if (c.kind === 'tree') { treeHintKey(k); return true; } // explorer.js
  if (!c.kind) {
    if (k === 't' || k === 'b' || k === 'f' || k === 'e') {
      clearTimeout(c.timer);
      c.kind = k;
      c.timer = setTimeout(() => create(k, 1), CHORD_PANE_MS);
    } else if (k === 'o') { // a tab of another task, into the pane that I use: no digit (copytab.js)
      endChord();
      openCopyList();
    } else endChord();
    return true;
  }
  create(c.kind, /^[1-4]$/.test(k) ? Number(k) : 1);
  return true;
}

// why: a line for bf.log, so that a Cmd+J that seemed lost can be traced (Renaud, 2026-11-11)
function endChord(why = '') {
  const c = G.chord;
  G.chord = null;
  if (c) clearTimeout(c.timer);
  if (c && c.kind === 'tree') clearTreeHints(); // explorer.js
  if (why) uiLog(why);
}

function jumpTimer() { return setTimeout(() => endChord(`jump: no key within ${CHORD_KIND_MS / 1000} s`), CHORD_KIND_MS); }

// The keyboard goes to the tab on screen in pane n (Cmd+J n). An empty pane: nothing happens.
function jumpTo(n) {
  const w = wsId() && S.mode === 'dev' && work(wsId());
  const tab = w && paneActive(w, n);
  endChord(`jump: pane ${n}${tab ? '' : ' (empty)'}`);
  if (tab) showTab(tab, true);
}

// Cmd+1 to Cmd+9: the n-th tab from the left of the pane that I use goes on screen and takes the
// keyboard, wherever the keyboard was (as Cmd+W). No such tab: nothing happens.
function showTabAt(n) {
  const w = wsId() && S.mode === 'dev' && work(wsId());
  const tab = w && paneTabs(w, w.pane)[n - 1];
  if (tab) showTab(tab, true);
}

// Cmd+J C: the keyboard goes to the selected task's Claude Code (4a). No task: nothing happens.
function jumpToClaude() {
  const t = S.mode === 'dev' && S.selected && S.terms.get(S.selected);
  endChord(`jump: Claude Code${t ? '' : ' (no task)'}`);
  if (t) t.term.focus();
}

function create(kind, n) {
  endChord();
  const tid = wsId(); // a project without a task (wsId) has file tabs only: terminals and pages belong to a task
  if (!tid || S.mode !== 'dev') { toast('Select a task first: tabs belong to a task.'); return; }
  if (!S.selected && kind !== 'f' && kind !== 'e') { toast('Start a task first: terminals and browser tabs belong to a task.'); return; }
  uiLog(`create: ${{ t: 'terminal', b: 'browser', f: 'new file', e: 'Finder' }[kind]} in pane ${n}`);
  if (kind === 't') newShell(tid, n);
  else if (kind === 'b') openWebTab(tid, '', { pane: n });
  else if (kind === 'e') newFinder(tid, n); // finder.js
  else newUntitled(tid, n);
}

// ------------------------------------------------------------------ the pane that I use
// A click or the keyboard in a pane makes it the one that I use. A browser page is a native
// view: its clicks never reach this page, but the keys that it forwards say which tab it is (browser.js).

function paneOf(el) {
  const p = el && el.closest && el.closest('#middle .pane');
  return p ? Number(p.dataset.pane) : 0;
}

function noteUse(e) {
  const n = paneOf(e.target);
  const w = n && wsId() && S.work && S.work[wsId()];
  if (!w || w.pane === n || !paneTabs(w, n).length) return;
  usePane(w, n);
  saveGrid(wsId());
  layoutPanes(w);
  renderExplorer();
}
document.addEventListener('mousedown', noteUse, true);
document.addEventListener('focusin', noteUse, true);

// ------------------------------------------------------------------ drag a tab to another pane
// Mouse events, not HTML drag and drop: while the button is down, AppKit sends every move to
// this page, also over a browser page (a native view on top), which would take a drop for
// itself. The pages show snapshots meanwhile (covered(), browser.js), so the marks are visible.

document.addEventListener('mousedown', (e) => {
  const el = e.button === 0 && !e.target.closest('.x') && e.target.closest('#middle .pane-tabs .tab[data-file]');
  const w = el && wsId() && work(wsId());
  const tab = w && w.tabs.find((t) => t.path === el.dataset.file);
  if (tab) G.press = { tab, x: e.clientX, y: e.clientY };
});

document.addEventListener('mousemove', (e) => {
  const p = G.press;
  if (!p) return;
  if (!G.drag) {
    if (Math.hypot(e.clientX - p.x, e.clientY - p.y) < 6) return;
    startDrag(p.tab);
  }
  e.preventDefault();
  G.drag.ghost.style.transform = `translate(${e.clientX + 12}px, ${e.clientY + 8}px)`;
  G.drag.target = dropTarget(e.clientX, e.clientY);
  markDrop(G.drag.target);
});

document.addEventListener('mouseup', () => {
  const d = G.drag;
  G.press = null;
  if (!d) return;
  endDrag();
  G.justDragged = true; // the click that follows the mouseup is not a click on a tab
  setTimeout(() => { G.justDragged = false; }, 0);
  const t = d.target;
  if (!t || (t.pane === d.tab.pane && !t.before)) return;
  if (t.before === d.tab) return;
  moveTab(d.tab, t.pane, t.before);
});

document.addEventListener('click', (e) => {
  if (G.justDragged) { e.stopPropagation(); e.preventDefault(); }
}, true);

function startDrag(tab) {
  const ghost = document.createElement('div');
  ghost.className = 'tab-ghost';
  ghost.textContent = tabName(tab);
  const mark = document.createElement('div');
  mark.className = 'drop-mark';
  mark.hidden = true;
  document.body.append(ghost, mark);
  G.drag = { tab, ghost, mark, target: null };
  S.tabDrag = true;
  document.body.classList.add('tab-dragging');
}

function endDrag() {
  G.drag.ghost.remove();
  G.drag.mark.remove();
  G.drag = null;
  S.tabDrag = false;
  document.body.classList.remove('tab-dragging');
}

// {pane, before, rect}: where the tab goes, and the rectangle to mark. Over a tab bar: into that
// pane, before the tab under the mouse. Over a pane, near an edge whose neighbour is empty: into
// the neighbour (it opens). Elsewhere in a pane: into that pane.
function dropTarget(x, y) {
  const w = work(wsId());
  for (const n of shownPanes(w)) {
    const r = G.els[n].pane.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
    const bar = G.els[n].tabs.getBoundingClientRect();
    if (y <= bar.bottom) {
      const over = [...G.els[n].tabs.querySelectorAll('.tab[data-file]')].find((el) => {
        const b = el.getBoundingClientRect();
        return x >= b.left && x <= b.right;
      });
      const before = over ? w.tabs.find((t) => t.path === over.dataset.file) : null;
      return { pane: n, before, rect: r };
    }
    const fx = (x - r.left) / r.width, fy = (y - r.top) / r.height;
    const [row, col] = CELL[n];
    const at = (rr, cc) => rr * 2 + cc + 1;
    const edges = [ // [distance to the edge, neighbour, the half of the pane to mark]
      [1 - fx, at(row, 1), { left: r.left + r.width / 2, top: r.top, width: r.width / 2, height: r.height }],
      [fx, at(row, 0), { left: r.left, top: r.top, width: r.width / 2, height: r.height }],
      [1 - fy, at(1, col), { left: r.left, top: r.top + r.height / 2, width: r.width, height: r.height / 2 }],
      [fy, at(0, col), { left: r.left, top: r.top, width: r.width, height: r.height / 2 }],
    ].filter(([d, m]) => d < EDGE && m !== n && !paneTabs(w, m).filter((t) => t !== G.drag.tab).length);
    edges.sort((a, b) => a[0] - b[0]);
    if (edges.length) return { pane: edges[0][1], before: null, rect: edges[0][2] };
    return { pane: n, before: null, rect: r };
  }
  return null;
}

function markDrop(t) {
  const m = G.drag.mark;
  m.hidden = !t || (t.pane === G.drag.tab.pane && !t.before);
  if (m.hidden) return;
  Object.assign(m.style, { left: `${t.rect.left}px`, top: `${t.rect.top}px`, width: `${t.rect.width}px`, height: `${t.rect.height}px` });
}

buildGrid();
