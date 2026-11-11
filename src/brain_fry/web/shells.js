'use strict';
// Terminal tabs: tabs of the panes of column 3 (grid.js), like files and pages. Each one is a
// shell in a PTY of the backend (key "shell:<task>:<n>"), shown in its own xterm.js terminal.
// They belong to the task, and the backend's state lists them (task.shells): a tab appears when
// its shell is in the state and goes when it is not any more (`exit`, or × in its tab).
// No terminal at start: Cmd+N T (grid.js) opens one, in the pane that I say.
// Uses S, $, request, toast, ensureTerm, connect, fitSafely (app.js), work (files.js) and the
// panes of grid.js.

// The size of a terminal about to start in pane n: the pane's own size when it is on screen,
// else an estimate from column 3 (the terminal fits itself once it shows).
function shellBoxSize(n = 1) {
  const body = G.els[n] && G.els[n].body.getBoundingClientRect();
  let { width, height } = body && body.width > 50 ? body : $('#middle').getBoundingClientRect();
  if (!(body && body.width > 50)) {
    if (n !== 1) width /= 2;
    if (n > 2) height /= 2;
  }
  return { rows: Math.max(4, Math.floor((height - 6) / 17)), cols: Math.max(40, Math.floor((width - 8) / 7.8)) }; // Menlo 13px
}

// path: a folder of the project or a file (its folder), from "Open a terminal here" in 2a;
// folder: the same from a Finder tab, absolute, anywhere on the disk (finder.js); without them,
// the shell starts in the task's cwd. python: a .py file of the project, which the shell runs from
// the project's root (runPython). focus false: the tab shows, the keyboard stays where it is.
async function newShell(tid, n = 1, path = null, folder = null, { python = null, focus = true } = {}) {
  const w = work(tid);
  const next = { pane: n, focus, show: true };
  w.nextShells.push(next); // the state may list the shell before the reply arrives
  try {
    await request('shell.new', { id: tid, ...shellBoxSize(n), ...(path === null ? {} : { path }), ...(folder === null ? {} : { folder }),
      ...(python === null ? {} : { python }) });
  } catch (e) {
    const i = w.nextShells.indexOf(next);
    if (i >= 0) w.nextShells.splice(i, 1);
    toast(e.message);
  }
}

// The play button of a Python file (files.js), as VS Code's "Run Python File": the file is saved
// first, then a new terminal in pane 3 (bottom left, which opens if it was empty), at the root of
// the project, runs `python <file>` (the backend picks the project's .venv when it has one). The
// keyboard stays in the file, as in VS Code.
async function runPython(tab) {
  if (!S.selected) { toast('Start a task first: terminals and browser tabs belong to a task.'); return; } // as Cmd+N T
  if (tab.view) tab.view.focus();
  if (tab.dirty) {
    await saveTab(tab);
    if (tab.dirty) return; // not saved (changed on disk, an error): saveTab said why
  }
  uiLog('create: terminal in pane 3, runs a Python file');
  newShell(tab.tid, 3, null, null, { python: tab.path, focus: false });
}

// The terminal tabs of every task follow the state. A new shell goes to the pane that I asked
// for (newShell), or to the one that the window remembers for its position (grid.js), or to 1.
function renderShells() {
  const st = S.state;
  let changed = false;
  let focus = null;
  if (S.selected && st.tasks[S.selected]) work(S.selected); // its shells may arrive before its first restore
  for (const [tid, w] of Object.entries(S.work || {})) {
    const task = st.tasks[tid];
    const shells = task ? task.shells : [];
    const keys = new Set(shells.map((s) => s.key));
    for (const tab of w.tabs.filter((t) => t.shell && !keys.has(t.key))) {
      dropTab(w, tab);
      changed = true;
      saveGrid(tid); // the positions of the others changed
    }
    shells.forEach((s, i) => {
      let tab = w.tabs.find((t) => t.shell && t.key === s.key);
      if (!tab) {
        let n;
        let show = false;
        const next = w.nextShells.shift(); // asked for (newShell, Cmd+N O): in that order
        if (next) { n = next.pane; show = next.focus || next.show; if (next.focus) focus = s.key; } else {
          w.grid = w.grid || loadGrid(tid);
          n = w.grid.at[`shell#${i}`] || 1;
        }
        const t = ensureTerm(s.key, G.els[n].body, true);
        tab = { tid, key: s.key, path: `shell:${s.key}`, el: t.el, term: t, kind: 'shell', shell: true, name: '',
          dirty: false, diskChanged: false, gone: false };
        addTab(w, tab, n);
        if (show) { w.act[n] = tab.path; delete w.want[n]; if (focus === s.key) usePane(w, n); saveGrid(tid); }
        changed = true;
      }
      tab.name = `${s.title} ${i + 1}`;
      connect(tab.term, s.gen);
    });
  }
  // the xterm of a shell that is gone
  const alive = new Set(Object.values(st.tasks).flatMap((t) => t.shells.map((s) => s.key)));
  for (const [key, t] of S.terms) {
    if (t.shell && !alive.has(key)) { if (t.ws) { t.ws.onclose = null; t.ws.close(); } t.term.dispose(); t.el.remove(); S.terms.delete(key); }
  }
  if (changed) renderFiles();
  const t = focus && S.terms.get(focus);
  if (t && S.selected && work(S.selected).tabs.some((x) => x.key === focus)) {
    requestAnimationFrame(() => { fitSafely(t); t.term.focus(); });
  }
}
