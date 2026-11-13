'use strict';
// Dev mode columns: 1 (side) | 2a (explorer) | the grid of column 3 (middle, grid.js) | 4a (agent).
// Review mode (review.js): 1 (rvSide) | 2a (the log, rvLog high) over 2b (rvFiles) and 3b; Cmd+1 hides 1.
// Drag the splitters to resize. Cmd+Option+1 to 4 show or hide column 1, 2a, column 3 and 4a
// in Dev mode (Renaud, 2026-11-12); each column takes its splitter with it.
// The panes of the grid share column 3 by fractions (layoutPanes, grid.js). Rows first:
// gridRows (the top row's share of the height), gridTop and gridBottom (the left pane's share of
// its row). Columns first: gridCols (the left column's share of the width), gridLeft and
// gridRight (the top pane's share of its column). Sizes and the shown columns are remembered by
// the window (localStorage), but at start 4a takes a third of the window (startWidths).

const LAYOUT_DEFAULT = { side: 270, explorer: 200, middle: 480, gridRows: 0.5, gridTop: 0.5, gridBottom: 0.5,
  gridCols: 0.5, gridLeft: 0.5, gridRight: 0.5, rvSide: 230, rvLog: 300, rvFiles: 360, rvSide_on: true,
  side_on: true, explorer_on: true, middle_on: true, agent_on: true };
const COLUMNS = { 1: 'side', 2: 'explorer', 3: 'middle', 4: 'agent' }; // Cmd+Option+digit --> the column's id
const LAYOUT_MIN = { side: 220, explorer: 140, middle: 0, rvSide: 150, rvLog: 100, rvFiles: 200 };
const GRID_FRACTION = [0.15, 0.85]; // a pane keeps at least this share of its row or column

function applyLayout() {
  const L = S.layout;
  const root = document.documentElement.style;
  root.setProperty('--w-side', `${L.side}px`);
  root.setProperty('--w-explorer', `${L.explorer}px`);
  root.setProperty('--w-middle', `${L.middle}px`);
  root.setProperty('--w-rv-side', `${L.rvSide}px`);
  root.setProperty('--h-rv-log', `${L.rvLog}px`);
  root.setProperty('--w-rv-files', `${L.rvFiles}px`);
  $('#rv-side').hidden = !L.rvSide_on;
  $('[data-split="rvSide"]').hidden = !L.rvSide_on;
  for (const c of Object.values(COLUMNS)) $('#dev').classList.toggle(`no-${c}`, !L[`${c}_on`]); // app.css
}

// Cmd+Option+1 to 4: the digit of the column, else 0. By e.code, since Option changes e.key
// ('¡'); a key forwarded by a browser page (forwardKey in browser.js) has no code, only its digit.
function columnKey(e) {
  if (!e.metaKey || !e.altKey || e.ctrlKey || e.shiftKey) return 0;
  const m = /^Digit([1-4])$/.exec(e.code || '') || /^([1-4])$/.exec(e.key || '');
  return m ? Number(m[1]) : 0;
}

// Dev mode. A column that comes back takes the keyboard when it has a place for it (2a: the
// tree, column 3: the tab on screen in the pane that I use, 4a: Claude Code; not column 1).
// Hiding the column that has the keyboard gives it to 4a, or to column 3 when 4a is hidden.
function toggleColumn(n) {
  const c = COLUMNS[n];
  const had = $(`#${c}`).contains(document.activeElement);
  S.layout[`${c}_on`] = !S.layout[`${c}_on`];
  store('layout', S.layout);
  applyLayout();
  if (S.layout[`${c}_on`]) focusColumn(c);
  else if (had) { // WebKit keeps the focus in a hidden element: a hidden terminal would still get the keys
    document.activeElement.blur();
    focusColumn(S.layout.agent_on ? 'agent' : 'middle');
  }
}

// Cmd+J C, Cmd+J E, and a tab of column 3 that takes the keyboard show their column first;
// a new or forked task shows 4a (onState, app.js).
function revealColumn(c) {
  if (S.layout[`${c}_on`]) return;
  S.layout[`${c}_on`] = true;
  store('layout', S.layout);
  applyLayout();
}

function focusColumn(c) {
  if (c === 'explorer') { $('#tree').focus(); return; }
  if (c === 'agent') {
    const t = S.selected && S.terms.get(S.selected);
    if (t) requestAnimationFrame(() => { fitSafely(t); t.term.focus(); });
    return;
  }
  if (c === 'middle') {
    const w = wsId() && work(wsId());
    const tab = w && paneActive(w, w.pane);
    if (tab) requestAnimationFrame(() => focusTab(tab));
  }
}

function toggleLayout(what) {
  const L = S.layout;
  if (what === 'rvSide') L.rvSide_on = !L.rvSide_on;
  store('layout', L);
  applyLayout();
  if (typeof render === 'function') render();
}

// A splitter of the grid: the fraction follows the mouse inside column 3. Every split spans
// column 3 along its own axis (a row spans its width, a column its height): one box for all.
function dragGridSplit(e, key) {
  const box = $('#middle').getBoundingClientRect();
  const vertical = ['gridRows', 'gridLeft', 'gridRight'].includes(key); // a horizontal bar, moved up and down
  document.body.classList.add(vertical ? 'resizing-v' : 'resizing-h');
  const move = (ev) => {
    const f = vertical ? (ev.clientY - box.top) / box.height : (ev.clientX - box.left) / box.width;
    S.layout[key] = Math.min(GRID_FRACTION[1], Math.max(GRID_FRACTION[0], f));
    layoutPanes(wsId() ? work(wsId()) : null);
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    document.body.classList.remove('resizing-v', 'resizing-h');
    store('layout', S.layout);
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

document.addEventListener('mousedown', (e) => {
  const split = e.target.closest('.split');
  if (!split) return;
  e.preventDefault();
  const key = split.dataset.split; // side, explorer, middle; gridRows, gridTop, gridBottom, gridCols, gridLeft, gridRight; rvSide, rvLog, rvFiles
  if (key.startsWith('grid')) { dragGridSplit(e, key); return; }
  const vertical = key === 'rvLog';
  const start = vertical ? e.clientY : e.clientX;
  // The middle column shrinks below its saved width in a narrow window (a laptop after a large
  // screen): the drag starts from the width on screen, else nothing moves until the mouse has crossed the gap.
  const size0 = key === 'middle' ? Math.round($('#middle').getBoundingClientRect().width) : S.layout[key];
  document.body.classList.add(vertical ? 'resizing-v' : 'resizing-h');
  const move = (ev) => {
    const d = (vertical ? ev.clientY : ev.clientX) - start;
    S.layout[key] = Math.max(LAYOUT_MIN[key], size0 + d);
    applyLayout();
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    document.body.classList.remove('resizing-v', 'resizing-h');
    store('layout', S.layout);
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
});

// At start, 4a takes a third of the window (Renaud, 2026-11-13): 4a takes what the middle column
// leaves, so the middle column gets the rest. Its saved width is replaced; the other columns keep theirs.
function startWidths() {
  const L = S.layout;
  if (!L.middle_on || !L.agent_on) return; // a hidden middle column: 4a takes everything anyway
  const split = 4; // .split.v
  const left = (L.side_on ? L.side + split : 0) + (L.explorer_on ? L.explorer + split : 0);
  L.middle = Math.max(LAYOUT_MIN.middle, Math.round(innerWidth - left - split - innerWidth / 3));
}

S.layout = { ...LAYOUT_DEFAULT, ...stored('layout', {}) };
startWidths();
applyLayout();
