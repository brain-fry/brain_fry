'use strict';
// A task whose tabs are being built (its first select after a start, a new task, a fork) is
// hidden while they arrive, then 2a and column 3 fade in together. Before, each part appeared on
// its own: one file read per tab, the browser tabs, the terminals at a later state push, the open
// folders of 2a one by one (Renaud, 2026-11-10: "flashes them, which is disruptive"). The native
// browser pages ignore CSS: browser.js gives them the alpha, app/browser.py animates it.
// Uses S, $, uiLog (app.js) and work (files.js). See DESIGN_DETAILS.md, "2a, 3a, 3b".

const VEIL = { tid: null, start: 0, ready: 0, frame: null };
const VEIL_FADE_MS = 300;
const VEIL_MAX_MS = 1500; // shown by then, ready or not (a failed read, a shell that never starts)
const veilEls = () => [$('#explorer'), $('#middle')];
const veiled = (tid) => VEIL.tid !== null && VEIL.tid === tid;

// select (app.js), before its first render of the task
function veil(tid) {
  Object.assign(VEIL, { tid, start: performance.now(), ready: 0 });
  for (const el of veilEls()) { el.style.transition = 'none'; el.style.opacity = '0'; }
  if (!VEIL.frame) VEIL.frame = requestAnimationFrame(veilTick);
}

// Everything that the restore brings is in place: the files and browser tabs (restoreFiles), the
// open folders of 2a, and one tab per terminal of the task file (the backend starts them).
function veilReady(tid) {
  const task = S.state && S.state.tasks[tid];
  const w = S.work && S.work[tid];
  if (!task || !w) return true;
  if (!w.restored || w.restoring) return false;
  if (!w.kids.has('') || w.loading.size) return false;
  const shells = task.shells || [];
  if (shells.length < (task.terminalTabs || 0)) return false;
  return shells.every((s) => w.tabs.some((t) => t.key === s.key));
}

function veilTick() {
  VEIL.frame = null;
  if (VEIL.tid === null) return;
  if (VEIL.tid !== wsId() || S.mode !== 'dev') return unveil(); // the selected task, or the idle screen's (showIdle)
  if (performance.now() - VEIL.start > VEIL_MAX_MS) {
    uiLog(`veil: ${VEIL.tid.slice(0, 8)} shown after ${VEIL_MAX_MS} ms, its tabs not all in place`);
    return unveil();
  }
  // ready at two frames in a row: the terminals and the PDFs fit their panes at the next frame
  VEIL.ready = veilReady(VEIL.tid) ? VEIL.ready + 1 : 0;
  if (VEIL.ready >= 2) return unveil();
  VEIL.frame = requestAnimationFrame(veilTick);
}

function unveil() {
  VEIL.tid = null;
  if (VEIL.frame) cancelAnimationFrame(VEIL.frame);
  VEIL.frame = null;
  for (const el of veilEls()) { el.style.transition = `opacity ${VEIL_FADE_MS}ms ease-out`; el.style.opacity = ''; }
}
