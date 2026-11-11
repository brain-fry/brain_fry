'use strict';
// Dev mode columns: 1 (side) | 2a (explorer) | the grid of column 3 (middle, grid.js) | 4a (agent).
// Review mode (review.js): 1 (rvSide) | 2a (the log, rvLog high) over 2b (rvFiles) and 3b; Cmd+1 hides 1.
// Drag the splitters to resize. 2a and the middle column are always shown (Renaud, 2026-11-11).
// The panes of the grid share column 3 by fractions: gridRows (the top row's share of the
// height), gridTop and gridBottom (the left pane's share of its row). Sizes are remembered by
// the window (localStorage).

const LAYOUT_DEFAULT = { side: 270, explorer: 200, middle: 480,
  gridRows: 0.5, gridTop: 0.5, gridBottom: 0.5, rvSide: 230, rvLog: 300, rvFiles: 360, rvSide_on: true };
const LAYOUT_MIN = { side: 220, explorer: 140, middle: 300, rvSide: 150, rvLog: 100, rvFiles: 200 };
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
}

function toggleLayout(what) {
  const L = S.layout;
  if (what === 'rvSide') L.rvSide_on = !L.rvSide_on;
  store('layout', L);
  applyLayout();
  if (typeof render === 'function') render();
}

// A splitter of the grid: the fraction follows the mouse inside the row (gridTop, gridBottom)
// or inside column 3 (gridRows).
function dragGridSplit(e, key) {
  const box = (key === 'gridRows' ? $('#middle') : e.target.parentElement).getBoundingClientRect();
  const vertical = key === 'gridRows';
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
  const key = split.dataset.split; // side, explorer, middle; gridRows, gridTop, gridBottom; rvSide, rvLog, rvFiles
  if (key.startsWith('grid')) { dragGridSplit(e, key); return; }
  const vertical = key === 'rvLog';
  const start = vertical ? e.clientY : e.clientX;
  const size0 = S.layout[key];
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

S.layout = { ...LAYOUT_DEFAULT, ...stored('layout', {}) };
applyLayout();
