'use strict';
// 3b: terminal tabs. Each one is a shell in a PTY of the backend (key "shell:<task>:<n>"),
// shown in its own xterm.js terminal. They belong to the selected task. `exit` closes a tab.
// Uses S, $, esc, request, send, toast, ensureTerm, connect, fitSafely, work from the other files.

function shellBoxSize() {
  const box = $('#shell-terms').getBoundingClientRect(); // estimate for Menlo 13px
  return { rows: Math.max(4, Math.floor((box.height - 6) / 17)), cols: Math.max(40, Math.floor((box.width - 8) / 7.8)) };
}

async function newShell() {
  const tid = S.selected;
  if (!tid) return;
  if (!S.layout.shells) toggleLayout('shells');
  try {
    const { key } = await request('shell.new', { id: tid, ...shellBoxSize() });
    work(tid).shell = key;
    S.focusShell = key; // focus it once its terminal exists
  } catch (e) { toast(e.message); }
}

function closeShell(key) {
  send('shell.close', { key });
}

function renderShells() {
  const st = S.state;
  const tid = S.selected;
  const task = tid && st.tasks[tid];
  const shells = task ? task.shells : [];
  const w = tid && work(tid);
  if (w && (!w.shell || !shells.some((s) => s.key === w.shell))) w.shell = shells.length ? shells[shells.length - 1].key : null;

  $('#shell-tabs').innerHTML = shells.map((s, i) =>
    `<div class="tab ${w && s.key === w.shell ? 'on' : ''}" data-shell="${esc(s.key)}">${esc(s.title)} ${i + 1}`
    + `<span class="x" data-close-shell="${esc(s.key)}">×</span></div>`).join('')
    + '<div class="tab add" data-new-shell title="New terminal">+</div>';
  $('#shell-empty').hidden = shells.length > 0;

  // one xterm per running shell of any task; drop the ones whose shell is gone
  const alive = new Set(Object.values(st.tasks).flatMap((t) => t.shells.map((s) => s.key)));
  for (const [key, t] of S.terms) {
    if (t.shell && !alive.has(key)) { if (t.ws) { t.ws.onclose = null; t.ws.close(); } t.term.dispose(); t.el.remove(); S.terms.delete(key); }
  }
  for (const s of shells) connect(ensureTerm(s.key, $('#shell-terms'), true), s.gen);
  for (const t of S.terms.values()) if (t.shell) t.el.style.display = w && t.id === w.shell ? '' : 'none';

  const active = w && w.shell && S.terms.get(w.shell);
  if (active) {
    requestAnimationFrame(() => {
      fitSafely(active);
      if (S.focusShell === active.id) { S.focusShell = null; active.term.focus(); }
    });
  }
}

document.addEventListener('click', (e) => {
  const close = e.target.closest('[data-close-shell]');
  if (close) { e.stopPropagation(); closeShell(close.dataset.closeShell); return; }
  if (e.target.closest('[data-new-shell]')) { newShell(); return; }
  const tab = e.target.closest('#shell-tabs .tab[data-shell]');
  if (tab && S.selected) {
    work(S.selected).shell = tab.dataset.shell;
    S.focusShell = tab.dataset.shell;
    renderShells();
  }
}, true);

new ResizeObserver(() => {
  const w = S.selected && S.work && S.work[S.selected];
  const t = w && w.shell && S.terms.get(w.shell);
  if (t) requestAnimationFrame(() => fitSafely(t));
}).observe($('#shell-terms'));
