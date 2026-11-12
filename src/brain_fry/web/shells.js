'use strict';
// Terminal tabs: tabs of the panes of column 3 (grid.js), like files and pages. Each one is a
// shell in a PTY of the backend (key "shell:<owner>:<n>"), shown in its own xterm.js terminal.
// They belong to a task or to its project (project tabs), and the backend's state lists them
// (task.shells, project.tabs.shells: shellsOf): a tab appears when its shell is in the state and
// goes when it is not any more (`exit`, or × in its tab). A double-click moves a shell to the
// other owner in the backend (shell.move); the state then moves its tab (reown).
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
// the shell starts in the task's cwd (a project's tabs: the project folder). tid: the owner, a task
// or `project:<path>` (ownerFor). python: a .py file of the project, which the shell runs from
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
  if (tab.view) tab.view.focus();
  if (tab.dirty) {
    await saveTab(tab);
    if (tab.dirty) return; // not saved (changed on disk, an error): saveTab said why
  }
  uiLog('create: terminal in pane 3, runs a Python file');
  newShell(ownerFor(wsId()), 3, null, null, { python: tab.path, focus: false }); // as Cmd+N T
}

// The terminal tabs of every task and project follow the state. A new shell goes to the pane that
// I asked for (newShell), or to the one that the window remembers for its position (grid.js), or to 1.
function renderShells() {
  const st = S.state;
  let changed = false;
  let focus = null;
  for (const id of viewIds()) if (ownerData(id)) work(id); // their shells may arrive before their first restore
  const owners = new Map(); // shell key -> its owner now (a double-click moves it: shell.move)
  for (const tid of Object.keys(S.work || {})) for (const s of shellsOf(tid)) owners.set(s.key, tid);
  for (const w of Object.values(S.work || {})) {
    for (const tab of w.tabs.filter((t) => t.shell && owners.has(t.key) && owners.get(t.key) !== w.id)) {
      const from = w.id;
      reown(tab, owners.get(tab.key)); // files.js: same tab, same pane, same terminal
      saveGrid(from);
      saveGrid(tab.tid);
      changed = true;
    }
  }
  for (const [tid, w] of Object.entries(S.work || {})) {
    const shells = shellsOf(tid);
    const keys = new Set(shells.map((s) => s.key));
    // a task's terminals are numbered after its project's, which show before them in each pane
    const first = isProjectWs(tid) || !wsProject(tid) ? 1 : shellsOf(projectWs(wsProject(tid))).length + 1;
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
          n = w.grid.at[gridKey(tid, `shell#${i}`)] || 1;
        }
        const t = ensureTerm(s.key, G.els[n].body, true);
        tab = { tid, key: s.key, path: `shell:${s.key}`, el: t.el, term: t, kind: 'shell', shell: true, name: '',
          dirty: false, diskChanged: false, gone: false };
        addTab(w, tab, n);
        if (show) { // in the view that asked for it (a project tab: the idle screen's task, or the project view)
          const v = viewWork(tid);
          v.act[n] = tab.path;
          delete v.want[n];
          if (focus === s.key) usePane(v, n);
          saveGrid(v.id);
        }
        changed = true;
      }
      tab.name = `${s.title} ${first + i}`;
      connect(tab.term, s.gen);
    });
  }
  // the xterm of a shell that is gone
  const alive = new Set([...Object.values(st.tasks), ...st.projects.map((p) => p.tabs || {})].flatMap((t) => (t.shells || []).map((s) => s.key)));
  for (const [key, t] of S.terms) {
    if (t.shell && !alive.has(key)) { if (t.ws) { t.ws.onclose = null; t.ws.close(); } t.term.dispose(); t.el.remove(); S.terms.delete(key); }
  }
  if (changed) renderFiles();
  const t = focus && S.terms.get(focus);
  if (t && wsId() && viewTabs(work(wsId())).some((x) => x.key === focus)) {
    requestAnimationFrame(() => { fitSafely(t); t.term.focus(); });
  }
}
