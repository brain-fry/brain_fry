'use strict';
// Review mode (DESIGN_DETAILS.md, "Review mode"): a git client in the style of SourceTree, for
// the repository of the selected task's folder. 1: worktrees and branches (read-only); 2a: the
// log and its graph, "Uncommitted changes" first; 2b: the staged and unstaged files, or the files
// of a commit; 3b: the diff, read-only, with buttons on each hunk, and single lines that can be
// selected. Commit opens a dialog. 4a comes in and out with Cmd+4. The backend is git.py.

const RV_POLL_MS = 2000;
const RV_ROW = 22; // px: a row of the log
const RV_LANE = 12; // px between two lanes of the graph
const RV_COLORS = ['#6aa1ff', '#a78bfa', '#2bb5a8', '#e8912d', '#e07ab8', '#9bd36a', '#f2c94c', '#5ac8fa'];
const RV_PAGE = 1000;

const R = {
  key: null, // the task whose repository is shown
  tree: null, // the worktree chosen in panel 1; null: the one that holds the task's folder
  from: 'dev', // the mode that Cmd+Shift+G goes back to
  st: null, // the reply of git.state
  error: null,
  log: null, // the reply of git.log
  logKey: null, // what the log was loaded for: refs, worktree, options
  logVer: 0,
  count: RV_PAGE,
  all: stored('rvAll', true), // all branches, or the current branch only
  sel: 'wip', // the row of 2a: 'wip' or a commit
  info: null, // git.commit_info of the selected commit
  file: null, // {side, path, orig, st, sha}: the file of 2b whose diff 3b shows
  fileIndex: 0, // its place in its list, for the next file when it leaves the list
  diff: null,
  diffKey: null,
  diffVer: 0,
  full: false, // "Show all" for this file
  cursor: 0, // the current hunk (N / P, Space)
  lines: null, // {hunk, set}: the lines selected in one hunk
  anchor: null, // the line of the last plain click, for Shift+click
  focus: 'files', // log, files, diff
  drafts: {}, // worktree path -> the draft of the commit message
  dialog: false,
  busy: false,
  again: false,
  timer: null,
  sigs: '', // the updates of the project's tasks: a hook event refreshes at once
  drawn: {}, // panel -> the HTML on screen
};

S.reviewAgent = stored('reviewAgent', false); // 4a on screen in Review mode (Cmd+4)

// ------------------------------------------------------------------ entering, leaving, 4a

function enterReview(from) {
  R.from = from === 'organize' ? 'organize' : 'dev';
  if (document.activeElement) document.activeElement.blur(); // out of the terminal
  clearInterval(R.timer);
  R.timer = setInterval(() => { if (S.mode === 'review' && document.visibilityState === 'visible') refreshReview(); }, RV_POLL_MS);
  refreshReview();
  requestAnimationFrame(() => focusPanel(R.focus));
}

function leaveReview() {
  clearInterval(R.timer);
  R.timer = null;
  closeCommitDialog();
}

// classes on #dev: Review mode replaces 1, 2a and the middle column; 4a keeps the width that it
// has in Dev mode, so that its PTY keeps its size and Claude Code does not re-render
function applyReviewLayout() {
  const dev = $('#dev');
  dev.classList.toggle('review', S.mode === 'review');
  dev.classList.toggle('rv-agent', S.mode === 'review' && S.reviewAgent);
  document.documentElement.style.setProperty('--w-agent-review', `${S.agentWidth || 480}px`);
}

function toggleReviewAgent() {
  S.reviewAgent = !S.reviewAgent;
  store('reviewAgent', S.reviewAgent);
  applyReviewLayout();
  const t = S.selected && S.terms.get(S.selected);
  if (S.reviewAgent && t) requestAnimationFrame(() => { fitSafely(t); t.term.focus(); });
  else focusPanel(R.focus);
  render();
}

// ------------------------------------------------------------------ data

const rvReq = (cmd, extra = {}) => request(cmd, { id: R.key, tree: R.tree, ...extra });
const fileKey = (f) => (f ? `${f.side}|${f.path}|${f.sha || ''}|${R.st ? R.st.tree : ''}` : '');
const errMsg = (e) => String(e && e.message ? e.message : e).replace(/^\w+Error: /, '');

function resetReview(key) {
  Object.assign(R, { key, tree: null, st: null, error: null, log: null, logKey: null, count: RV_PAGE, sel: 'wip', info: null,
    file: null, diff: null, diffKey: null, lines: null, anchor: null, cursor: 0, full: false, sigs: '' });
  R.logVer++;
  R.diffVer++;
}

async function refreshReview() {
  if (!R.key) return;
  if (R.busy) { R.again = true; return; }
  const key = R.key;
  R.busy = true;
  try {
    const st = await rvReq('git.state');
    if (key !== R.key) return;
    R.st = st;
    R.error = null;
    if (st.repo) {
      const logKey = `${st.refsSig}|${st.tree}|${R.all}|${R.count}`;
      if (logKey !== R.logKey) {
        const log = await rvReq('git.log', { all: R.all, count: R.count });
        if (key !== R.key) return;
        R.log = log;
        R.logKey = logKey;
        R.logVer++;
      }
      if (R.sel !== 'wip' && !R.log.rows.some((r) => r.h === R.sel)) selectRow('wip', false);
      if (R.sel === 'wip') keepFileSelected();
      await loadDiff();
    }
  } catch (e) {
    if (key === R.key) R.error = errMsg(e);
  } finally {
    R.busy = false;
    R.loadingMore = false;
    drawReview();
    if (R.again) { R.again = false; refreshReview(); }
  }
}

// the lists of 2b in their order on screen: conflicts, staged, unstaged
function wipFiles() {
  const st = R.st;
  if (!st || !st.repo) return [];
  return [
    ...st.conflicts.map((f) => ({ ...f, side: 'conflict' })),
    ...st.staged.map((f) => ({ ...f, side: 'staged' })),
    ...st.unstaged.map((f) => ({ ...f, side: f.st === '?' ? 'untracked' : 'unstaged' })),
  ];
}

const sideList = (side) => (side === 'untracked' ? 'unstaged' : side);

// after a change, the selection stays on the file, or moves to the next file of the same list
// when the file left it (or to the first file when nothing is selected)
function keepFileSelected() {
  const all = wipFiles();
  const f = R.file;
  if (f && f.side !== 'commit') {
    const same = all.find((x) => x.path === f.path && sideList(x.side) === sideList(f.side));
    if (same) { R.file = same; return; }
    const list = all.filter((x) => sideList(x.side) === sideList(f.side));
    const next = list[Math.min(R.fileIndex, list.length - 1)];
    return setFile(next || all.find((x) => x.side !== 'conflict') || all[0] || null);
  }
  if (!f) setFile(all.find((x) => x.side === 'unstaged' || x.side === 'untracked') || all[0] || null);
}

function setFile(f) {
  if (fileKey(f) === fileKey(R.file)) { R.file = f; return; }
  R.file = f;
  R.diff = null;
  R.diffKey = null;
  R.lines = null;
  R.anchor = null;
  R.cursor = 0;
  R.full = false;
  if (f && f.side !== 'commit') {
    const list = wipFiles().filter((x) => sideList(x.side) === sideList(f.side));
    R.fileIndex = Math.max(0, list.findIndex((x) => x.path === f.path));
  }
}

async function loadDiff() {
  const f = R.file;
  if (!f) { R.diff = null; R.diffKey = null; return; }
  const key = fileKey(f);
  const d = await rvReq('git.diff', { side: f.side, path: f.path, orig: f.orig || null, sha: f.sha || null, full: R.full });
  if (fileKey(R.file) !== key) return; // another file was selected meanwhile
  if (R.diffKey === key && R.diff && R.diff.sig === d.sig) return;
  const prev = R.diffKey === key ? R.diff : null;
  R.diff = d;
  R.diffKey = key;
  R.diffVer++;
  // the selected lines stay only while their hunk is the same
  if (R.lines && !(prev && prev.hunks[R.lines.hunk] && d.hunks[R.lines.hunk]
      && prev.hunks[R.lines.hunk].digest === d.hunks[R.lines.hunk].digest)) R.lines = null;
  R.cursor = Math.max(0, Math.min(R.cursor, d.hunks.length - 1));
}

async function selectRow(sha, load = true) {
  if (R.sel === sha && load) return;
  R.sel = sha;
  R.info = null;
  setFile(null);
  if (sha === 'wip') {
    keepFileSelected();
  }
  if (!load) return;
  drawReview();
  try {
    if (sha !== 'wip') {
      const info = await rvReq('git.commit_info', { sha });
      if (R.sel !== sha) return;
      R.info = info;
      const f = info.files[0];
      setFile(f ? { ...f, side: 'commit', sha } : null);
    }
    await loadDiff();
  } catch (e) {
    toast(errMsg(e));
  }
  drawReview();
}

async function selectFile(f) {
  setFile(f);
  drawReview();
  try { await loadDiff(); } catch (e) { toast(errMsg(e)); }
  drawReview();
}

// ------------------------------------------------------------------ actions

async function rvAction(cmd, extra, done = null) {
  try {
    const r = await rvReq(cmd, extra);
    if (done) done(r || {});
  } catch (e) {
    toast(errMsg(e));
  }
  await refreshReview();
}

function stageFile(f) {
  if (!f || f.side === 'commit') return;
  if (f.side === 'staged') return rvAction('git.unstage', { paths: f.orig ? [f.orig, f.path] : [f.path] });
  rvAction('git.stage', { paths: [f.path] });
}

function stageAll(unstage) {
  const st = R.st;
  if (!st || !st.repo) return;
  if (unstage) rvAction('git.unstage', { paths: st.staged.flatMap((f) => (f.orig ? [f.orig, f.path] : [f.path])) });
  else rvAction('git.stage', { paths: st.unstaged.map((f) => f.path) });
}

const nameOfPath = (p) => p.replace(/\/$/, '').split('/').pop();

function undoToast(what, r) {
  if (r.trashed) return toast(`${what} moved to the Trash.`);
  if (r.undo) toast(`Discarded ${what}.`, { label: 'Undo', run: () => rvAction('git.undo_discard', { name: r.undo }) });
}

function discardFile(f) {
  if (!f || (f.side !== 'unstaged' && f.side !== 'untracked')) return;
  const name = nameOfPath(f.path);
  const q = f.side === 'untracked'
    ? `Move the untracked ${f.path.endsWith('/') ? 'folder' : 'file'} ${name} to the Trash?`
    : `Discard the changes of ${name} that are not staged? Undo stays possible for a few seconds, and a copy is kept in .brain_fry/discarded/.`;
  if (!confirm(q)) return;
  rvAction('git.discard', { path: f.path, untracked: f.side === 'untracked' }, (r) => undoToast(name, r));
}

function selectedLines(h) {
  return R.lines && R.lines.hunk === h && R.lines.set.size ? [...R.lines.set].sort((a, b) => a - b) : null;
}

function hunkAction(action, h) {
  const d = R.diff, f = R.file;
  if (!d || !f || !d.hunkable || !d.hunks[h]) return;
  if (action === 'discard' && f.side !== 'unstaged') return;
  const lines = selectedLines(h);
  const what = lines ? `${lines.length} line${lines.length === 1 ? '' : 's'}` : 'this hunk';
  if (action === 'discard' && !confirm(`Discard ${what} of ${nameOfPath(f.path)}? Undo stays possible for a few seconds, and a copy is kept in .brain_fry/discarded/.`)) return;
  R.cursor = h;
  R.lines = null;
  rvAction(`git.${action}`, { path: f.path, digest: d.hunks[h].digest, lines },
    (r) => { if (action === 'discard') undoToast(`${what} of ${nameOfPath(f.path)}`, r); });
}

// Space: the selected file (2b), or the hunk at the cursor or its selected lines (3b)
function spaceAction() {
  const f = R.file;
  if (!f) return;
  if (R.focus === 'diff' && R.diff && R.diff.hunkable) return hunkAction(f.side === 'staged' ? 'unstage' : 'stage', R.lines ? R.lines.hunk : R.cursor);
  if (R.focus !== 'log') stageFile(f);
}

function discardFocused() {
  const f = R.file;
  if (!f) return;
  if (R.focus === 'diff' && R.diff && R.diff.hunkable && f.side === 'unstaged') return hunkAction('discard', R.lines ? R.lines.hunk : R.cursor);
  discardFile(f);
}

// ------------------------------------------------------------------ the commit dialog

function currentTree() {
  return R.st && R.st.repo ? R.st.trees.find((t) => t.path === R.st.tree) : null;
}

function openCommitDialog() {
  const st = R.st;
  if (!st || !st.repo) return;
  if (!st.staged.length) return toast('Nothing is staged: stage files, hunks or lines first.');
  const t = currentTree();
  const n = st.staged.length;
  const el = $('#rv-dialog');
  el.innerHTML = `<div class="box"><h3>Commit ${n} staged file${n === 1 ? '' : 's'}
      <span class="hint">on ${esc(st.branch || 'a detached HEAD')} · worktree ${esc(t ? t.name : '')} · the repository's hooks run</span></h3>
    <div class="rv-dfiles">${st.staged.map((f) => `<div><span class="st st-${stClass(f.st)}">${esc(f.st)}</span>  ${esc(f.orig ? `${f.orig} --> ${f.path}` : f.path)}</div>`).join('')}</div>
    <textarea class="plan-notes" rows="7" spellcheck="false" placeholder="Message: a subject line, then a blank line and the details">${esc(R.drafts[st.tree] || '')}</textarea>
    <pre class="rv-err" hidden></pre>
    <div class="rv-dkeys"><span>Cmd+Enter: commit · Esc: close (the message is kept)</span>
      <button data-rvd="cancel">Cancel</button><button class="primary" data-rvd="commit">Commit</button></div></div>`;
  el.hidden = false;
  R.dialog = true;
  const ta = el.querySelector('textarea');
  ta.addEventListener('input', () => { R.drafts[st.tree] = ta.value; });
  ta.addEventListener('keydown', (e) => {
    if (e.isComposing) return;
    if (e.key === 'Enter' && e.metaKey) { e.preventDefault(); doCommit(); }
    else if (e.key === 'Escape') { e.preventDefault(); closeCommitDialog(); }
  });
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

function closeCommitDialog() {
  const el = $('#rv-dialog');
  if (el.hidden) return;
  el.hidden = true;
  el.innerHTML = '';
  R.dialog = false;
  if (S.mode === 'review') focusPanel(R.focus);
}

async function doCommit() {
  const el = $('#rv-dialog');
  const ta = el.querySelector('textarea');
  if (!ta || el.dataset.busy) return;
  const tree = R.st.tree;
  const message = ta.value;
  if (!message.trim()) return ta.focus();
  const err = el.querySelector('.rv-err');
  const btn = el.querySelector('[data-rvd="commit"]');
  el.dataset.busy = '1';
  btn.disabled = true;
  btn.textContent = 'Committing...';
  try {
    const r = await rvReq('git.commit', { message });
    R.drafts[tree] = '';
    closeCommitDialog();
    toast(`Committed ${r.sha}.`);
  } catch (e) {
    err.textContent = errMsg(e); // the output of the hooks, among others
    err.hidden = false;
    btn.disabled = false;
    btn.textContent = 'Commit';
  } finally {
    delete el.dataset.busy;
  }
  refreshReview();
}

$('#rv-dialog').addEventListener('click', (e) => {
  const b = e.target.closest('[data-rvd]');
  if (b && b.dataset.rvd === 'commit') return doCommit();
  if (b || e.target.id === 'rv-dialog') closeCommitDialog(); // Cancel, or the dimmed background
});
$('#rv-dialog').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeCommitDialog(); }
});

// ------------------------------------------------------------------ drawing

function setHtml(id, html) { // only when it changed: the panels redraw at every state push
  if (R.drawn[id] === html) return false;
  R.drawn[id] = html;
  $(`#${id}`).innerHTML = html;
  if (id === 'rv-rows') R.drawnLog = null; // drawLog and drawDiff write these two themselves
  if (id === 'rv-diffbody') delete $('#rv-diffbody').dataset.key;
  return true;
}

function renderReview(st) {
  const task = st.tasks[S.selected];
  const key = task ? task.id : null;
  if (key !== R.key) {
    resetReview(key);
    if (key) refreshReview();
  }
  if (task) { // a hook event of a task of this project: the agent may have changed files
    const sig = Object.values(st.tasks).filter((t) => t.projectPath === task.projectPath).map((t) => t.id + t.updated).join();
    if (sig !== R.sigs) { const first = !R.sigs; R.sigs = sig; if (!first) refreshReview(); }
  }
  drawReview();
}

function drawReview() {
  if (S.mode !== 'review') return;
  const st = R.st;
  if (!R.key || !st || !st.repo || R.error) {
    const msg = !R.key ? ''
      : R.error ? `Git: ${esc(R.error)}`
      : !st ? ''
      : `${esc((S.state.tasks[R.key] || {}).project || 'This project')} is not in a git repository.`;
    setHtml('rv-side', '');
    setHtml('rv-loghead', '');
    setHtml('rv-rows', msg ? `<div class="rv-note">${msg}</div>` : '');
    setHtml('rv-files', '');
    setHtml('rv-diffbar', '');
    setHtml('rv-diffbody', '');
    R.drawnLog = null;
    return;
  }
  drawSide();
  drawLog();
  drawFiles();
  drawDiff();
}

function drawSide() {
  const st = R.st;
  const trees = st.trees.map((t) => {
    const meta = [t.detached ? `detached at ${(t.head || '').slice(0, 7)}` : (t.branch || 'no commit yet'),
      t.prunable ? 'folder missing' : t.count === null || t.count === undefined ? '' : t.count ? `${t.count} changed` : 'clean'].filter(Boolean).join(' · ');
    const dots = t.tasks.map((x) => `<span class="dot ${x.status}" data-task="${x.id}" title="${esc(x.title)} (${x.status}): select it"></span>`).join('');
    return `<div class="rv-tree${t.path === st.tree ? ' sel' : ''}${t.prunable ? ' gone' : ''}" data-tree="${esc(t.path)}" title="${esc(t.path)}">
      <div class="main"><div class="name">${esc(t.name)}</div><div class="meta">${esc(meta)}</div></div><div class="dots">${dots}</div></div>`;
  }).join('');
  const branches = st.branches.map((b) => {
    const wt = b.worktree ? st.trees.find((t) => t.path === b.worktree) : null;
    const track = b.track.replace(/ahead (\d+)/, '$1 ahead').replace(/behind (\d+)/, '$1 behind');
    const right = [track, wt ? `wt ${wt.name}` : ''].filter(Boolean).join(' · ');
    return `<div class="rv-branch${b.name === st.branch ? ' cur' : ''}" data-branch="${esc(b.name)}"><span class="name">${esc(b.name)}</span><span class="right">${esc(right)}</span></div>`;
  }).join('');
  setHtml('rv-side', `<h2>Worktrees</h2>${trees}<h2>Branches</h2>${branches}`);
}

function fmtDate(t) {
  const d = new Date(t * 1000);
  const m = d.toLocaleString('en-GB', { month: 'short' });
  const time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return d.getFullYear() === new Date().getFullYear() ? `${d.getDate()} ${m} ${time}` : `${d.getDate()} ${m} ${d.getFullYear()}`;
}

function graphSvg(r, gw) {
  const ys = [0, RV_ROW / 2, RV_ROW];
  const x = (l) => 7 + l * RV_LANE;
  const color = (c) => RV_COLORS[c % RV_COLORS.length];
  let s = '';
  for (const [x1, y1, x2, y2, c, dash] of r.segs) {
    const X1 = x(x1), X2 = x(x2), Y1 = ys[y1], Y2 = ys[y2], ym = (Y1 + Y2) / 2;
    const d = X1 === X2 ? `M${X1} ${Y1}V${Y2}` : `M${X1} ${Y1}C${X1} ${ym} ${X2} ${ym} ${X2} ${Y2}`;
    s += `<path d="${d}" stroke="${color(c)}"${dash ? ' stroke-dasharray="2 3" stroke-width="1.5"' : ''}/>`;
  }
  const cx = x(r.lane), cy = RV_ROW / 2;
  s += r.h === 'wip'
    ? `<circle cx="${cx}" cy="${cy}" r="3.5" fill="#1b1c1f" stroke="#9a9ca6" stroke-width="1.5"/>`
    : `<circle cx="${cx}" cy="${cy}" r="4" fill="${color(r.color)}" stroke="#1b1c1f" stroke-width="1.5"/>`;
  return `<svg width="${gw}" height="${RV_ROW}" fill="none" stroke-width="2">${s}</svg>`;
}

function logRowHtml(r, gw) {
  const wip = r.h === 'wip';
  const refs = r.refs.map((x) => `<span class="rv-ref ${x.k}${x.head ? ' head' : ''}">${esc(x.n)}</span>`).join('');
  const subj = wip ? '<span class="s">Uncommitted changes</span><span class="n rv-wipcount"></span>' : `<span class="s" title="${esc(r.s)}">${esc(r.s)}</span>`;
  return `<div class="rv-row${wip ? ' wip' : ''}" data-sha="${r.h}"><span class="g">${graphSvg(r, gw)}</span>`
    + `<span class="d">${refs}${subj}</span><span class="h">${wip ? '' : r.h.slice(0, 7)}</span>`
    + `<span class="a">${esc(r.a)}</span><span class="t">${wip ? '' : fmtDate(r.t)}</span></div>`;
}

function drawLog() {
  const rows = R.log ? R.log.rows : [];
  const gw = Math.max(56, Math.min(8 + Math.max(1, ...rows.map((r) => r.w || 1)) * RV_LANE, 180)); // 56: the word "Graph"
  setHtml('rv-loghead', `<span class="g" style="width:${gw}px">Graph</span><span class="d">Description
    <span class="rv-toggle"><button data-rv-all="1" class="${R.all ? 'on' : ''}">All branches</button><button data-rv-all="0" class="${R.all ? '' : 'on'}">Current branch</button></span></span>
    <span class="h">Commit</span><span class="a">Author</span><span class="t">Date</span>`);
  const box = $('#rv-rows');
  if (R.drawnLog !== R.logVer) { // the rows are drawn again only when the log changed
    const top = box.scrollTop;
    R.drawn['rv-rows'] = null;
    box.innerHTML = rows.map((r) => logRowHtml(r, gw)).join('') + (R.log && R.log.more ? '<div class="rv-more">More commits load as you scroll.</div>' : '');
    box.style.setProperty('--rv-graph', `${gw}px`);
    box.scrollTop = top;
    R.drawnLog = R.logVer;
  }
  for (const el of box.querySelectorAll('.rv-row.sel')) if (el.dataset.sha !== R.sel) el.classList.remove('sel');
  const sel = box.querySelector(`.rv-row[data-sha="${R.sel}"]`);
  if (sel) sel.classList.add('sel');
  const n = new Set(wipFiles().map((f) => f.path)).size;
  const wc = box.querySelector('.rv-wipcount');
  if (wc) wc.textContent = n ? `· ${n} file${n === 1 ? '' : 's'}` : '· no changes';
}

const stClass = (s) => (s === '?' ? 'q' : s);

function fileRowHtml(f, act) {
  const sel = R.file && fileKey(R.file) === fileKey(f);
  const name = nameOfPath(f.path) + (f.path.endsWith('/') ? '/' : '');
  const dir = f.path.replace(/\/$/, '').split('/').slice(0, -1).join('/');
  const orig = f.orig ? `${f.orig} --> ` : '';
  const btn = act ? `<button class="act" data-rv-act title="${act.title}">${act.label}</button>` : '';
  // lines added and removed; a zero side is left out, and so is a binary file (no counts)
  const nums = [f.add ? `<span class="add">+${f.add}</span>` : '', f.del ? `<span class="del">-${f.del}</span>` : ''].filter(Boolean).join(' ');
  return `<div class="rv-file${sel ? ' sel' : ''}" data-side="${f.side}" data-path="${esc(f.path)}" title="${esc(orig + f.path)}">`
    + `<span class="st st-${stClass(f.st)}">${esc(f.st)}</span><span class="name">${esc(name)}</span><span class="dir">${esc(dir)}</span>`
    + `${nums ? `<span class="nums">${nums}</span>` : ''}${btn}</div>`;
}

function drawFiles() {
  const st = R.st;
  if (R.sel !== 'wip') return drawCommitFiles();
  const files = wipFiles();
  const conflicts = files.filter((f) => f.side === 'conflict');
  const staged = files.filter((f) => f.side === 'staged');
  const unstaged = files.filter((f) => f.side === 'unstaged' || f.side === 'untracked');
  const op = st.op ? `<div class="rv-op">${esc(st.op[0].toUpperCase() + st.op.slice(1))} in progress: resolve the conflicts, stage them, then commit.</div>` : '';
  const sec = (title, list, act, buttons) => `<div class="rv-sec"><h2>${title} <span class="n">${list.length}</span>${buttons}</h2>`
    + list.map((f) => fileRowHtml(f, act)).join('') + '</div>';
  const hidden = st.staged.length ? '' : 'disabled title="Nothing is staged"';
  setHtml('rv-files', op
    + (conflicts.length ? sec('Conflicts', conflicts, { label: '+', title: 'Mark resolved (stage it)' }, '') : '')
    + sec('Staged files', staged, { label: '−', title: 'Unstage (Space)' },
      `<button data-rv="unstage-all" ${st.staged.length ? '' : 'disabled'}>Unstage all</button><button class="primary" data-rv="commit" ${hidden} title="Write the message and commit (Cmd+Enter)">Commit</button>`)
    + sec('Unstaged files', unstaged, { label: '+', title: 'Stage (Space)' },
      `<button data-rv="stage-all" ${unstaged.length ? '' : 'disabled'}>Stage all</button>`));
}

function drawCommitFiles() {
  const i = R.info;
  if (!i) return setHtml('rv-files', '');
  const files = i.files.map((f) => ({ ...f, side: 'commit', sha: i.h }));
  const [subject, ...rest] = i.message.split('\n');
  const body = rest.join('\n').trim();
  setHtml('rv-files', `<div class="rv-commit"><div class="subj">${esc(subject)}</div>${body ? `<div class="body">${esc(body)}</div>` : ''}
    <dl><dt>Commit</dt><dd class="mono">${esc(i.h)}</dd><dt>Parents</dt><dd class="mono">${esc(i.parents.map((p) => p.slice(0, 7)).join(', ') || 'none')}</dd>
    <dt>Author</dt><dd>${esc(i.author)}</dd><dt>Date</dt><dd>${esc(new Date(i.t * 1000).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }))}</dd></dl></div>
    <div class="rv-sec"><h2>Files <span class="n">${files.length}</span></h2>${files.map((f) => fileRowHtml(f, null)).join('')}</div>`);
}

const SIDE_TEXT = {
  unstaged: ['Unstaged', 'index --> work tree'],
  untracked: ['Untracked', 'new file, staged as a whole'],
  staged: ['Staged', 'HEAD --> index'],
  conflict: ['Conflict', 'resolve it in the editor or with the agent, then stage it'],
  commit: ['Commit', ''],
};

function diffNote(d) {
  if (!d) return '';
  if (d.folder) return 'A folder of untracked files: stage it or move it to the Trash as a whole.';
  if (d.tooBig) return 'Too big to show (over 2 MB).';
  if (d.info && d.info.binary) return 'Binary file.';
  if (!d.hunks.length) {
    const modes = (d.header || []).filter((l) => /^(old|new) mode /.test(l)).join(' --> ').replace(/(old|new) mode /g, '');
    if (modes) return `Mode changed: ${modes}.`;
  }
  return '';
}

function drawDiff() {
  const f = R.file, d = R.diff;
  if (!f) {
    setHtml('rv-diffbar', '');
    setHtml('rv-diffbody', '');
    return;
  }
  const [kind, range0] = SIDE_TEXT[f.side] || ['', ''];
  const range = f.side === 'commit' && R.info ? `${(R.info.parents[0] || 'empty tree').slice(0, 7)} --> ${R.info.h.slice(0, 7)}` : range0;
  const pick = d && d.hunkable;
  const top = [];
  if (f.side === 'unstaged' || f.side === 'untracked') top.push(['discard-file', f.side === 'untracked' ? 'Move to Trash' : 'Discard file', 'discard'], ['stage-file', 'Stage file', '']);
  if (f.side === 'staged') top.push(['unstage-file', 'Unstage file', '']);
  if (f.side === 'conflict') top.push(['stage-file', 'Mark resolved', '']);
  const n = d ? d.hunks.length : 0;
  const right = pick && n ? `hunk ${R.cursor + 1} of ${n}` : d && d.truncated ? `${d.total} lines` : '';
  setHtml('rv-diffbar', `<span class="path" title="${esc(f.path)}">${esc(f.orig ? `${f.orig} --> ${f.path}` : f.path)}</span>`
    + `<span class="rv-chip">${kind}${f.side === 'commit' && R.info ? ` ${R.info.h.slice(0, 7)}` : ''}</span><span class="range">${esc(range)}</span>`
    + `<span class="right">${right}</span>${top.map(([a, label, cls]) => `<button data-rv-file="${a}" class="${cls}">${label}</button>`).join('')}`);
  const body = $('#rv-diffbody');
  const key = `${R.diffKey}|${R.diffVer}`;
  if (body.dataset.key !== key) {
    const sameFile = body.dataset.file === R.diffKey;
    const scroll = sameFile ? body.scrollTop : 0;
    const note = diffNote(d);
    body.innerHTML = !d ? '' : note && !d.hunks.length ? `<div class="rv-note">${note}</div>`
      : d.hunks.map((h, i) => hunkHtml(h, i, pick)).join('')
        + (d.truncated ? `<div class="rv-note">The diff has ${d.total} lines: the first ones are shown. <button data-rv-file="show-all">Show all</button></div>` : '');
    body.classList.toggle('rv-pick', Boolean(pick));
    body.dataset.key = key;
    body.dataset.file = R.diffKey || '';
    body.scrollTop = scroll;
    R.drawn['rv-diffbody'] = null;
  }
  drawDiffMarks();
}

function hunkHtml(h, i, pick) {
  const lines = h.lines.map((ln, j) => {
    const cls = ln[0] === '+' ? 'add' : ln[0] === '-' ? 'del' : ln[0] === '\\' ? 'meta' : 'ctx';
    return `<div class="ln ${cls}" data-l="${j}"><span class="o">${ln[1]}</span><span class="nn">${ln[2]}</span><span class="sg">${ln[0] === ' ' || ln[0] === '\\' ? '' : ln[0]}</span><span class="tx">${esc(ln[3])}</span></div>`;
  }).join('');
  return `<div class="rv-hunk" data-h="${i}"><div class="rv-hh"><span class="hdr">${esc(h.header)}</span>${pick ? '<span class="btns"></span>' : ''}</div>${lines}</div>`;
}

// the current hunk, the selected lines and the labels of the hunk buttons, without drawing the diff again
function drawDiffMarks() {
  const body = $('#rv-diffbody');
  const f = R.file, d = R.diff;
  if (!d || !f) return;
  for (const el of body.querySelectorAll('.rv-hunk')) {
    const h = Number(el.dataset.h);
    el.classList.toggle('cur', d.hunkable && h === R.cursor);
    const lines = selectedLines(h);
    for (const ln of el.querySelectorAll('.ln.sel')) if (!lines || !lines.includes(Number(ln.dataset.l))) ln.classList.remove('sel');
    if (lines) for (const j of lines) { const ln = el.querySelector(`.ln[data-l="${j}"]`); if (ln) ln.classList.add('sel'); }
    const btns = el.querySelector('.btns');
    if (!btns) continue;
    const what = lines ? `${lines.length} line${lines.length === 1 ? '' : 's'}` : 'hunk';
    const html = f.side === 'staged' ? `<button data-rv-hunk="unstage">Unstage ${what}</button>`
      : `<button data-rv-hunk="discard" class="discard">Discard ${what}</button><button data-rv-hunk="stage">Stage ${what}</button>`;
    if (btns.innerHTML !== html) btns.innerHTML = html;
  }
}

// ------------------------------------------------------------------ focus and keys

const PANELS = { log: '#rv-rows', files: '#rv-files', diff: '#rv-diffbody' };

function focusPanel(which) {
  R.focus = which;
  const el = $(PANELS[which]);
  if (el && S.mode === 'review') el.focus({ preventScroll: true });
}

function inReview(el) {
  return !el || el === document.body || Boolean(el.closest && el.closest('#review'));
}

function moveRow(step) {
  const rows = R.log ? R.log.rows : [];
  const i = rows.findIndex((r) => r.h === R.sel);
  const next = rows[Math.max(0, Math.min(rows.length - 1, i + step))];
  if (!next || next.h === R.sel) return;
  selectRow(next.h);
  const el = $(`#rv-rows .rv-row[data-sha="${next.h}"]`);
  if (el) el.scrollIntoView({ block: 'nearest' });
}

function moveFile(step) {
  const list = R.sel === 'wip' ? wipFiles() : (R.info ? R.info.files.map((f) => ({ ...f, side: 'commit', sha: R.info.h })) : []);
  const i = list.findIndex((f) => fileKey(f) === fileKey(R.file));
  const next = list[Math.max(0, Math.min(list.length - 1, i + step))];
  if (!next || fileKey(next) === fileKey(R.file)) return;
  selectFile(next);
  requestAnimationFrame(() => { const el = $('#rv-files .rv-file.sel'); if (el) el.scrollIntoView({ block: 'nearest' }); });
}

function moveHunk(step) {
  const d = R.diff;
  if (!d || !d.hunks.length) return;
  R.cursor = Math.max(0, Math.min(d.hunks.length - 1, R.cursor + step));
  R.lines = null;
  drawDiff();
  const el = $(`#rv-diffbody .rv-hunk[data-h="${R.cursor}"]`);
  if (el) el.scrollIntoView({ block: 'nearest' });
}

// from the keydown handler of app.js; true: handled
function reviewKey(e) {
  if (R.dialog) return false; // its own handlers
  const k = e.key;
  const here = inReview(document.activeElement);
  if (e.metaKey) {
    const lk = k.toLowerCase();
    if (lk === '1') { toggleLayout('rvSide'); return true; }
    if (lk === '4') { toggleReviewAgent(); return true; }
    if (k === 'Enter') { openCommitDialog(); return true; }
    if (k === 'Backspace' && here) { discardFocused(); return true; }
    return false;
  }
  if (e.ctrlKey || e.altKey || !here) return false; // 4a's terminal takes its keys
  if (k === 'Tab') {
    const order = ['log', 'files', 'diff'];
    focusPanel(order[(order.indexOf(R.focus) + (e.shiftKey ? 2 : 1)) % 3]);
  } else if (k === 'ArrowDown' || k === 'ArrowUp') {
    const step = k === 'ArrowDown' ? 1 : -1;
    if (R.focus === 'log') moveRow(step);
    else if (R.focus === 'files') moveFile(step);
    else return false; // the diff scrolls
  } else if (k === ' ') spaceAction();
  else if (k === 'n') moveHunk(1);
  else if (k === 'p') moveHunk(-1);
  else if (k === 'Escape') {
    if (R.lines) { R.lines = null; drawDiffMarks(); } else setMode('dev');
  } else return false;
  return true;
}

// ------------------------------------------------------------------ mouse

$('#review').addEventListener('mousedown', (e) => {
  for (const [which, sel] of Object.entries(PANELS)) {
    if (e.target.closest(sel)) { R.focus = which; return; }
  }
});

$('#review').addEventListener('click', (e) => {
  const t = e.target;
  const allBtn = t.closest('[data-rv-all]');
  if (allBtn) {
    R.all = allBtn.dataset.rvAll === '1';
    store('rvAll', R.all);
    return refreshReview();
  }
  const dot = t.closest('.rv-tree .dot[data-task]');
  if (dot) return select(dot.dataset.task);
  const tree = t.closest('.rv-tree[data-tree]');
  if (tree) {
    if (tree.classList.contains('gone') || tree.dataset.tree === (R.st && R.st.tree)) return;
    R.tree = tree.dataset.tree;
    R.sel = 'wip';
    setFile(null);
    return refreshReview();
  }
  const branch = t.closest('.rv-branch');
  if (branch) {
    const row = R.log && R.log.rows.find((r) => r.refs.some((x) => x.k === 'local' && x.n === branch.dataset.branch));
    if (!row) return toast('Its last commit is not among the commits loaded in the log.');
    selectRow(row.h);
    const el = $(`#rv-rows .rv-row[data-sha="${row.h}"]`);
    if (el) el.scrollIntoView({ block: 'center' });
    return focusPanel('log');
  }
  const rowEl = t.closest('.rv-row');
  if (rowEl) { focusPanel('log'); return selectRow(rowEl.dataset.sha); }
  const b = t.closest('[data-rv]');
  if (b && !b.disabled) {
    if (b.dataset.rv === 'commit') return openCommitDialog();
    return stageAll(b.dataset.rv === 'unstage-all');
  }
  const fileEl = t.closest('.rv-file');
  if (fileEl) {
    const f = findFile(fileEl.dataset.side, fileEl.dataset.path);
    focusPanel('files');
    if (!f) return;
    if (t.closest('[data-rv-act]')) return stageFile(f);
    return selectFile(f);
  }
  const fb = t.closest('[data-rv-file]');
  if (fb) {
    const a = fb.dataset.rvFile;
    if (a === 'show-all') { R.full = true; R.diffKey = null; return refreshReview(); }
    if (a === 'discard-file') return discardFile(R.file);
    if (a === 'stage-file' || a === 'unstage-file') return stageFile(R.file);
  }
  const hb = t.closest('[data-rv-hunk]');
  if (hb) return hunkAction(hb.dataset.rvHunk, Number(hb.closest('.rv-hunk').dataset.h));
  const ln = t.closest('.ln');
  if (ln) return clickLine(e, Number(ln.closest('.rv-hunk').dataset.h), Number(ln.dataset.l));
  const hunk = t.closest('.rv-hunk');
  if (hunk) { R.cursor = Number(hunk.dataset.h); drawDiff(); }
});

function findFile(side, path) {
  if (side === 'commit') return R.info ? R.info.files.map((f) => ({ ...f, side: 'commit', sha: R.info.h })).find((f) => f.path === path) : null;
  return wipFiles().find((f) => f.side === side && f.path === path) || null;
}

// a click selects a + or - line, Shift+click extends the selection, Cmd+click adds a line;
// the selection stays inside one hunk
function clickLine(e, h, j) {
  const d = R.diff;
  R.cursor = h;
  focusPanel('diff');
  const kind = d && d.hunks[h] && d.hunks[h].lines[j] ? d.hunks[h].lines[j][0] : ' ';
  if (!d || !d.hunkable || (kind !== '+' && kind !== '-')) return drawDiff();
  const same = R.lines && R.lines.hunk === h;
  if (e.shiftKey && same && R.anchor !== null) {
    const [a, b] = [Math.min(R.anchor, j), Math.max(R.anchor, j)];
    const set = new Set();
    for (let i = a; i <= b; i++) if ('+-'.includes(d.hunks[h].lines[i][0])) set.add(i);
    R.lines = { hunk: h, set };
  } else if (e.metaKey && same) {
    const set = new Set(R.lines.set);
    if (set.has(j)) set.delete(j); else set.add(j);
    R.lines = set.size ? { hunk: h, set } : null;
    R.anchor = j;
  } else if (same && R.lines.set.size === 1 && R.lines.set.has(j)) {
    R.lines = null; // a second click on the only selected line clears it
  } else {
    R.lines = { hunk: h, set: new Set([j]) };
    R.anchor = j;
  }
  drawDiff();
}

$('#rv-rows').addEventListener('scroll', () => {
  const box = $('#rv-rows');
  if (!R.log || !R.log.more || R.loadingMore) return;
  if (box.scrollTop + box.clientHeight > box.scrollHeight - 300) {
    R.loadingMore = true;
    R.count += RV_PAGE;
    refreshReview();
  }
});

// the right-click menu of a file of 2b (openCtx: explorer.js)
$('#review').addEventListener('contextmenu', (e) => {
  const fileEl = e.target.closest('.rv-file');
  if (!fileEl) return;
  e.preventDefault();
  const f = findFile(fileEl.dataset.side, fileEl.dataset.path);
  if (!f) return;
  selectFile(f);
  focusPanel('files');
  const task = S.state.tasks[R.key];
  const abs = `${R.st.tree}/${f.path}`.replace(/\/$/, '');
  const proj = task ? task.projectPath : '';
  const rel = proj && abs.startsWith(`${proj}/`) ? abs.slice(proj.length + 1) : null;
  const gone = f.st === 'D' || f.side === 'commit';
  openCtx(e.clientX, e.clientY, [
    { label: 'Open', disabled: !rel || gone || f.path.endsWith('/'), run: () => { setMode('dev'); openFile(R.key, rel); } },
    { label: f.side === 'untracked' ? 'Move to Trash' : 'Discard changes', key: '⌘⌫', disabled: f.side !== 'unstaged' && f.side !== 'untracked', run: () => discardFile(f) },
    '-',
    { label: 'Copy path', disabled: !rel, run: () => copyPath(R.key, rel, false) },
    { label: 'Copy relative path', disabled: !rel, run: () => copyPath(R.key, rel, true) },
  ]);
});
