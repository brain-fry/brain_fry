'use strict';
// Dev mode columns: 1 (side) | 2a (explorer) | 3a over 3b (middle) | 4a (agent).
// Review mode (review.js): 1 (rvSide) | 2a (the log, rvLog high) over 2b (rvFiles) and 3b; Cmd+1 hides 1.
// Drag the splitters to resize; Cmd+1 / Cmd+2 / Cmd+J show or hide 2a / the middle / 3b.
// Sizes are remembered by the window (localStorage).

const LAYOUT_DEFAULT = { side: 270, explorer: 200, middle: 480, shellsHeight: 200, explorer_on: true, middle_on: true, shells: true,
  rvSide: 230, rvLog: 300, rvFiles: 360, rvSide_on: true };
const LAYOUT_MIN = { side: 220, explorer: 140, middle: 300, shellsHeight: 80, rvSide: 150, rvLog: 100, rvFiles: 200 };

function applyLayout() {
  const L = S.layout;
  const root = document.documentElement.style;
  root.setProperty('--w-side', `${L.side}px`);
  root.setProperty('--w-explorer', `${L.explorer}px`);
  root.setProperty('--w-middle', `${L.middle}px`);
  root.setProperty('--h-shells', `${L.shellsHeight}px`);
  root.setProperty('--w-rv-side', `${L.rvSide}px`);
  root.setProperty('--h-rv-log', `${L.rvLog}px`);
  root.setProperty('--w-rv-files', `${L.rvFiles}px`);
  $('#rv-side').hidden = !L.rvSide_on;
  $('[data-split="rvSide"]').hidden = !L.rvSide_on;
  $('#explorer').hidden = !L.explorer_on;
  $('[data-split="explorer"]').hidden = !L.explorer_on;
  $('#middle').hidden = !L.middle_on;
  $('[data-split="middle"]').hidden = !L.middle_on;
  $('#shells').hidden = !L.shells;
  $('[data-split="shellsHeight"]').hidden = !L.shells;
}

function toggleLayout(what) {
  const L = S.layout;
  if (what === 'explorer') L.explorer_on = !L.explorer_on;
  if (what === 'middle') L.middle_on = !L.middle_on;
  if (what === 'shells') { L.shells = !L.shells; if (L.shells) L.middle_on = true; }
  if (what === 'rvSide') L.rvSide_on = !L.rvSide_on;
  store('layout', L);
  applyLayout();
  if (typeof render === 'function') render();
}

document.addEventListener('mousedown', (e) => {
  const split = e.target.closest('.split');
  if (!split) return;
  e.preventDefault();
  const key = split.dataset.split; // side, explorer, middle, shellsHeight; rvSide, rvLog, rvFiles
  const vertical = key === 'shellsHeight' || key === 'rvLog';
  const below = key === 'shellsHeight'; // the panel is below its splitter: dragging up makes it bigger
  const start = vertical ? e.clientY : e.clientX;
  const size0 = S.layout[key];
  document.body.classList.add(vertical ? 'resizing-v' : 'resizing-h');
  const move = (ev) => {
    const d = (vertical ? ev.clientY : ev.clientX) - start;
    S.layout[key] = Math.max(LAYOUT_MIN[key], below ? size0 - d : size0 + d);
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
