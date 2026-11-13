'use strict';
// The browser of 3a. A browser tab is a tab of 3a (kind 'web') whose element is only a
// placeholder: the page itself is a native WKWebView (app/browser.py) that the backend lays
// over the placeholder's rectangle. Native views always paint above this page, so while an
// HTML dialog covers the panel (the project picker), the view is hidden and a snapshot of it
// takes its place. See DESIGN_DETAILS.md, "Browser (3a)".
// Uses S, $, request, send, toast (app.js) and work, renderFiles,
// renderFileTabs, activeTab, closeFile (files.js).

const B = {
  seq: 0,
  tabs: new Map(), // key -> tab (every task's)
  placed: new Map(), // key -> the last placement sent: {visible, x, y, w, h}
  saveTimers: new Map(), // task id -> timer: the URLs of its tabs go to its task file
  unavailable: null, // why the backend has no browser (bf --browser): told once
};
const SEARCH = 'https://www.google.com/search?q=';

function webTabName(t) {
  if (t.title) return t.title;
  try { return new URL(t.url).host || 'New tab'; } catch { return 'New tab'; }
}

// What I type in the address field: a URL, a host, or words to search.
function normalizeUrl(text) {
  const s = text.trim();
  if (!s) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(s) && !/^[a-z0-9.-]+:\d+(\/|$)/i.test(s)) return s; // http:, about:, file:... (not host:port)
  if (/^(localhost|\d{1,3}(\.\d{1,3}){3})(:\d+)?(\/|$)/i.test(s)) return `http://${s}`;
  if (!/\s/.test(s) && /^[^/\s]+\.[a-z]{2,}(:\d+)?(\/\S*)?$/i.test(s)) return `https://${s}`;
  return SEARCH + encodeURIComponent(s);
}

function makeWebTab(tid, url, key, pane) {
  const el = document.createElement('div');
  el.className = 'editor web-page';
  key = key || `b${++B.seq}-${Math.random().toString(36).slice(2, 8)}`;
  const tab = { tid, key, path: `web:${key}`, el, kind: 'web', web: true, url: url || '', title: '', loading: false,
    back: false, forward: false, error: null, dirty: false, diskChanged: false, gone: false, wantFocus: false };
  tab.tools = makeTools(tab);
  tab.close = () => forgetWebTab(tab);
  tab.focus = () => focusPage(tab);
  B.tabs.set(key, tab);
  addTab(work(tid), tab, pane || openPane(viewWork(tid))); // grid.js; tid: its owner, a task or a project's tabs
  return tab;
}

// The toolbar, in the bar of 3a while the tab is shown (renderFiles keeps the element).
function makeTools(tab) {
  const t = document.createElement('span');
  t.className = 'web-tools';
  t.innerHTML = `<button data-web="back" title="Back (Cmd+[)" disabled>‹</button>
    <button data-web="forward" title="Forward (Cmd+])" disabled>›</button>
    <button data-web="reload" title="Reload (Cmd+R)">↻</button>
    <input class="web-url" spellcheck="false" autocomplete="off" placeholder="Address, or words to search (Cmd+L)">
    <span class="web-find-box" hidden><input class="web-find" placeholder="Find in page" title="Enter: next · Shift+Enter: previous · Esc: close"><span class="web-count"></span></span>
    <button data-web="ublock" title="uBlock Origin Lite: what it blocks on this page, and its settings">uBlock</button>`;
  const url = t.querySelector('.web-url');
  const find = t.querySelector('.web-find');
  url.value = tab.url === 'about:blank' ? '' : tab.url;
  url.addEventListener('focus', () => url.select());
  url.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const u = normalizeUrl(url.value);
      if (u) go(tab, u);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      url.value = tab.url === 'about:blank' ? '' : tab.url;
      focusPage(tab);
    }
  });
  let findTimer = null;
  find.addEventListener('input', () => { clearTimeout(findTimer); findTimer = setTimeout(() => findInPage(tab, false), 150); });
  find.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); findInPage(tab, e.shiftKey); }
    if (e.key === 'Escape') { e.preventDefault(); t.querySelector('.web-find-box').hidden = true; focusPage(tab); }
  });
  t.addEventListener('click', (e) => {
    const b = e.target.closest('[data-web]');
    if (!b || b.disabled) return;
    const a = b.dataset.web;
    if (a === 'reload') send('browser.nav', { tab: tab.key, action: tab.loading ? 'stop' : 'reload' });
    else if (a === 'ublock') openUblock(tab, b);
    else send('browser.nav', { tab: tab.key, action: a });
  });
  return t;
}

function updateTools(tab) {
  const t = tab.tools;
  t.querySelector('[data-web="back"]').disabled = !tab.back;
  t.querySelector('[data-web="forward"]').disabled = !tab.forward;
  const r = t.querySelector('[data-web="reload"]');
  r.textContent = tab.loading ? '✕' : '↻';
  r.title = tab.loading ? 'Stop' : 'Reload (Cmd+R)';
  const url = t.querySelector('.web-url');
  if (document.activeElement !== url) url.value = tab.url === 'about:blank' ? '' : tab.url;
  url.title = tab.error ? `Could not load: ${tab.error}` : tab.url;
  url.classList.toggle('error', Boolean(tab.error));
}

// pane: where it opens (Cmd+N B 2); by default the pane that I used last (openPane, grid.js)
async function openWebTab(tid, url = '', { activate = true, lazy = false, key = null, native = false, pane = null } = {}) {
  if (!tid) { toast('Pick a project first: tabs belong to a task or to its project.'); return null; }
  if (B.unavailable) { toast(B.unavailable); return null; }
  const tab = makeWebTab(tid, url, key, pane);
  if (!native) {
    try {
      await request('browser.open', { tab: tab.key, id: tid, url: url || 'about:blank', lazy });
    } catch (e) {
      if (/only in the Brain Fry window/.test(e.message)) B.unavailable = e.message;
      toast(e.message);
      dropWebTab(tab);
      if (inView(tid)) renderFiles();
      return null;
    }
  }
  if (activate) activateWebTab(tab, !url);
  else if (inView(tid)) renderFileTabs();
  if (!lazy) saveWebTabs(tid);
  return tab;
}

function activateWebTab(tab, focusAddress) {
  showTab(tab); // grid.js
  if (focusAddress) setTimeout(() => focusAddressField(tab), 0);
  else focusPage(tab);
}

function go(tab, url) {
  tab.url = url;
  tab.error = null;
  updateTools(tab);
  send('browser.go', { tab: tab.key, url });
  focusPage(tab);
  saveWebTabs(tab.tid);
}

// The keyboard goes to the native page at its next placement (it must be on screen first).
function focusPage(tab) {
  tab.wantFocus = true;
  if (document.activeElement && tab.tools.contains(document.activeElement)) document.activeElement.blur();
}

function focusAddressField(tab) {
  const url = tab.tools.querySelector('.web-url');
  url.focus();
  url.select();
}

function openFind(tab) {
  const box = tab.tools.querySelector('.web-find-box');
  box.hidden = false;
  const f = box.querySelector('.web-find');
  f.focus();
  f.select();
}

async function findInPage(tab, backwards) {
  const f = tab.tools.querySelector('.web-find');
  const count = tab.tools.querySelector('.web-count');
  if (!f.value) { count.textContent = ''; return; }
  try {
    const r = await request('browser.find', { tab: tab.key, query: f.value, backwards });
    count.textContent = r && r.found ? '' : 'not found';
  } catch (e) { toast(e.message); }
}

async function openUblock(tab, button) {
  const r = button.getBoundingClientRect();
  try {
    const res = await request('browser.ublock', { tab: tab.key, x: r.left, y: r.top, w: r.width, h: r.height });
    if (res && !res.installed) toast('uBlock Origin Lite is not installed: run bf ublock in a terminal, then restart Brain Fry.');
  } catch (e) { toast(e.message); }
}

// files.js calls tab.close() when the tab is closed (× or Cmd+W)
function forgetWebTab(tab) {
  send('browser.close', { tab: tab.key });
  B.tabs.delete(tab.key);
  B.placed.delete(tab.key);
  setTimeout(() => saveWebTabs(tab.tid), 0); // after files.js removed it from the list
}

function dropWebTab(tab) { // a tab whose page could not open
  dropTab(work(tab.tid), tab); // grid.js
  B.tabs.delete(tab.key);
}

function saveWebTabs(tid) {
  clearTimeout(B.saveTimers.get(tid));
  B.saveTimers.set(tid, setTimeout(() => {
    const urls = work(tid).tabs.filter((t) => t.web && t.url && t.url !== 'about:blank').map((t) => t.url);
    send('browser.tabs.set', { id: tid, urls });
  }, 500));
}

// files.js, the first time the task is selected after a start: its tabs come back, each in its
// pane (grid: the remembered grid, grid.js), and each loads its page when it is first shown
async function restoreBrowserTabs(tid, grid) { // tid: a task, or a project's tabs
  const urls = (ownerData(tid) || {}).browserTabs || [];
  for (const [i, url] of urls.entries()) {
    if (B.unavailable) return;
    await openWebTab(tid, url, { activate: false, lazy: true, pane: grid.at[gridKey(tid, `web#${i}`)] || 1 });
  }
}

// ------------------------------------------------------------------ messages from the backend

function onBrowser(m) {
  if (m.ev === 'key') { forwardKey(m); return; }
  if (m.ev === 'cookies') { // the copy of my Firefox cookies at the first start
    toast(m.error ? `Firefox cookies not copied: ${m.error}` : `Copied ${cookieCount(m)} from Firefox into the browser.`);
    return;
  }
  if (m.ev === 'download') {
    if (m.ok) toast(`Downloaded ${m.name} to ~/Downloads.`, { label: 'Show in Finder', run: () => send('browser.reveal', { path: m.path }) });
    else toast(`Download failed${m.name ? `: ${m.name}` : ''} (${m.error}).`);
    return;
  }
  if (m.ev === 'opened') { // a page opened a window (window.open, target=_blank, Cmd+click), or uBlock a tab
    if (B.tabs.has(m.tab)) return;
    const opener = m.opener && B.tabs.get(m.opener);
    // the opener's owner (a project tab opens project tabs), else the backend's guess, else where I would open one
    const tid = opener && !opener.closing ? opener.tid : ownerData(m.task) ? m.task : ownerFor(wsId());
    if (!tid) { send('browser.close', { tab: m.tab }); return; }
    const pane = opener && opener.tid === tid && !opener.closing ? opener.pane : null; // beside the page that opened it
    openWebTab(tid, m.url, { native: true, key: m.tab, activate: m.activate !== false, pane });
    return;
  }
  const tab = B.tabs.get(m.tab);
  if (!tab) return;
  if (m.ev === 'closed') { closeFile(tab.tid, tab.path); return; } // the page called window.close()
  if (m.ev === 'update') {
    const urlChanged = m.url && m.url !== tab.url;
    const titleChanged = m.title !== tab.title;
    Object.assign(tab, { url: m.url || tab.url, title: m.title, loading: m.loading, back: m.back, forward: m.forward, error: m.error || null });
    updateTools(tab);
    if (titleChanged && inView(tab.tid)) renderFileTabs();
    if (urlChanged) saveWebTabs(tab.tid);
  }
}

// "Overwrite my cookies" (the burger menu): my Firefox cookies replace the browser's, site by
// site. Open pages keep what they loaded: a reload uses the new cookies.
function cookieCount(r) {
  return `${r.cookies.toLocaleString()} cookies of ${r.sites.toLocaleString()} sites`;
}

function overwriteCookies() {
  toast('Copying the cookies from Firefox...');
  request('browser.cookies.firefox').then((r) => {
    toast(`Copied ${cookieCount(r)} from Firefox (profile ${r.profile}). Reload a page to use them.`);
  }).catch((e) => toast(`Firefox cookies not copied: ${e.message}`));
}

// A Cmd key pressed while the page had the focus: the backend sends it here, and it goes to
// the same listeners as a key pressed in this page (app.js, and the one below). `tab`: the page
// that had the focus; its pane becomes the one that I use (a click in a page never reaches here).
function forwardKey(m) {
  const t = m.tab && B.tabs.get(m.tab);
  if (t && S.work && S.work[t.tid] && !t.closing) usePane(viewWork(t.tid), t.pane);
  document.body.dispatchEvent(new KeyboardEvent('keydown', {
    key: m.key, metaKey: true, shiftKey: m.shift, altKey: m.alt, ctrlKey: m.ctrl, bubbles: true, cancelable: true,
  }));
}

// ------------------------------------------------------------------ placement, at every frame

function covered() { // S.tabDrag: a tab is dragged (grid.js), its marks must show over the pages
  return !$('#picker').hidden || !$('#copytab').hidden || !$('#menu').hidden || !$('#keys').hidden || !$('#ctx').hidden || Boolean(document.querySelector('#splash:not(.gone)'))
    || Boolean(S.tabDrag);
}

// At every frame (smooth while a splitter is dragged), and every 250 ms: WebKit pauses frames
// while the window is covered by other windows, and the views must still follow the layout.
function placeLoop() {
  requestAnimationFrame(placeLoop);
  place();
}
setInterval(place, 250);

function place() {
  const cover = covered();
  for (const tab of B.tabs.values()) {
    let want = { visible: false };
    if (S.mode === 'dev' && inView(tab.tid) && tab.el.style.display !== 'none' && tab.el.offsetParent) {
      const r = tab.el.getBoundingClientRect();
      if (r.width > 20 && r.height > 20) {
        // alpha 0 while its task is veiled (veil.js): the page is laid out and loads, unseen
        want = { visible: true, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
          alpha: veiled(wsId()) ? 0 : 1 }; // a project tab is veiled with the task that shows it
        if (cover) want.visible = shoot(tab);
        else if (tab.shot) clearShot(tab);
      }
    }
    const last = B.placed.get(tab.key) || { visible: false };
    const same = last.visible === want.visible && (!want.visible
      || (last.x === want.x && last.y === want.y && last.w === want.w && last.h === want.h && last.alpha === want.alpha));
    const focus = want.visible && tab.wantFocus;
    if (same && !focus) continue;
    if (focus) tab.wantFocus = false;
    B.placed.set(tab.key, want);
    const fade = want.visible && last.visible && last.alpha === 0 && want.alpha === 1 ? VEIL_FADE_MS : 0; // with 2a and column 3
    send('browser.place', { tab: tab.key, ...want, focus, fade });
  }
}

// Covered: keep the view until its snapshot is in the placeholder, then hide it.
function shoot(tab) {
  if (tab.shot) return false;
  if (!tab.shooting) {
    tab.shooting = true;
    request('browser.snapshot', { tab: tab.key }).then((url) => {
      if (url) tab.el.style.backgroundImage = `url("${url}")`;
      tab.shot = true;
    }).catch(() => { tab.shot = true; }).finally(() => { tab.shooting = false; });
  }
  return true;
}

function clearShot(tab) {
  tab.shot = false;
  setTimeout(() => { if (!tab.shot) tab.el.style.backgroundImage = ''; }, 100); // the view is back on top by then
}

requestAnimationFrame(placeLoop);

// The toast sits at the bottom center of the window, where it may fall on the page: then it
// moves over 4a, which nothing covers.
new MutationObserver(() => {
  const el = $('#toast');
  el.style.left = '';
  if (el.hidden) return;
  const vis = [...B.placed.entries()].find(([, p]) => p.visible);
  if (!vis) return;
  const p = vis[1];
  const r = el.getBoundingClientRect();
  if (r.right < p.x || r.left > p.x + p.w || r.bottom < p.y || r.top > p.y + p.h) return;
  const a = $('#agent').getBoundingClientRect();
  if (a.width) el.style.left = `${a.left + a.width / 2}px`;
}).observe($('#toast'), { attributes: true, attributeFilter: ['hidden'] });

// ------------------------------------------------------------------ keys

document.addEventListener('keydown', (e) => {
  if (!e.metaKey || S.mode !== 'dev' || S.picker) return;
  const k = e.key.toLowerCase();
  const tab = activeTab();
  const web = tab && tab.web ? tab : null;
  let done = true;
  if (!web) done = false; // a new page: Cmd+N B (grid.js); Cmd+T is a new task since 2026-11-10
  else if (k === 'l') focusAddressField(web); // Cmd+W: any tab (files.js)
  else if (k === 'f') openFind(web);
  else if (k === 'r') send('browser.nav', { tab: web.key, action: 'reload' });
  else if (k === '[') send('browser.nav', { tab: web.key, action: 'back' });
  else if (k === ']') send('browser.nav', { tab: web.key, action: 'forward' });
  else done = false;
  if (done) { e.preventDefault(); e.stopPropagation(); }
}, true);

// ------------------------------------------------------------------ links in the terminals

// Cmd+click on a URL in 4a or 3b opens it in a browser tab of the task, as Cmd+click opens a
// link in VS Code's terminal. A plain click selects text (or, in Claude Code's fullscreen
// mode, goes to Claude Code, which ignores it on a link). app.js calls this for
// every new terminal. Limits: a URL in the text that wraps onto the next line is cut at the
// line end (an OSC 8 link is not: its target is in the escape sequence).
// An OSC 8 link to a file (Claude Code writes one for a Markdown link, `[x](src/x.py#L2)`,
// since its PTY has FORCE_HYPERLINK) opens in 3a: openLink (files.js). Another scheme
// (`mailto:`, `vscode:`) is ignored, so that a link in a reply cannot start an app.
const URL_IN_TEXT = /https?:\/\/[^\s"'<>`]*[^\s"'<>`.,;:!?)\]}]/g;

function browserTermLinks(term) {
  const open = (e, uri) => {
    if (!e.metaKey) return;
    if (/^https?:/i.test(uri)) openWebTab(ownerFor(wsId()), uri, { activate: true }); // no task selected: a project tab
    else if (/^file:/i.test(uri) || !/^[a-z][a-z0-9+.-]*:/i.test(uri)) openLink(wsId(), uri); // also on the idle screen (showIdle)
  };
  // allowNonHttpProtocols: else xterm.js drops every OSC 8 target that is not http(s),
  // `hello.py` included (its OscLinkProvider)
  term.options.linkHandler = { activate: open, allowNonHttpProtocols: true };
  term.registerLinkProvider({
    provideLinks(y, callback) {
      const line = term.buffer.active.getLine(y - 1);
      if (!line) { callback(undefined); return; }
      const text = line.translateToString(true);
      const links = [];
      for (const m of text.matchAll(URL_IN_TEXT)) {
        links.push({ text: m[0], range: { start: { x: m.index + 1, y }, end: { x: m.index + m[0].length, y } }, activate: open });
      }
      callback(links.length ? links : undefined);
    },
  });
  // While the program tracks the mouse (Claude Code's fullscreen mode, `"tui": "fullscreen"`;
  // vim or htop in 3b), xterm.js sends it the press, the program redraws the row, xterm.js
  // builds a new link object for it, and its own check at mouseup (the same link at press and
  // release) fails: nothing opens. So a Cmd+press on a link is handled here, before xterm.js
  // sees it, and neither the press nor its release reaches the program.
  let held = false;
  const linkAt = (e) => {
    const scr = term.element.querySelector('.xterm-screen');
    if (!scr) return null;
    const r = scr.getBoundingClientRect();
    const x = Math.floor((e.clientX - r.left) / (r.width / term.cols)) + 1;
    const y = Math.floor((e.clientY - r.top) / (r.height / term.rows)) + 1 + term.buffer.active.viewportY;
    if (x < 1 || x > term.cols || y < 1) return null;
    const from = (p) => y > p.y || (y === p.y && x >= p.x), upTo = (p) => y < p.y || (y === p.y && x <= p.x);
    let hit = null;
    for (const p of term._core._linkProviderService.linkProviders) { // internal: no public lookup; both providers answer at once
      p.provideLinks(y, (links) => { for (const l of links || []) if (!hit && from(l.range.start) && upTo(l.range.end)) hit = l; });
    }
    return hit;
  };
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  term.element.addEventListener('mousedown', (e) => {
    held = false;
    if (!e.metaKey || e.button !== 0 || term.modes.mouseTrackingMode === 'none') return;
    const link = linkAt(e);
    if (!link) return;
    stop(e);
    held = true;
    link.activate(e, link.text);
  }, true);
  term.element.addEventListener('mouseup', (e) => { if (held) { held = false; stop(e); } }, true);
}
