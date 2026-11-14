'use strict';
// Markdown preview tabs (asked by Renaud on 2026-11-14: "right click on explorer to preview md
// files", "preview gets refreshed every time the file is updated (even if not saved)"). As VS
// Code's Open Preview: a tab "Preview README.md" in column 3 shows the file rendered, and follows
// its text: the text of an editor of the file while one is open in the project (unsaved edits
// included, at each change), else the disk's (a poll every 2 s, as the other tabs). Its path is
// `preview:<file>`, kept in the open files and the grid like any tab, so it is restored with the task.
// The HTML comes from CM.markdownHTML (frontend/markdown_html.js), and is sanitized here before
// it enters the page: a markdown file may hold any HTML. See DESIGN_DETAILS.md "2a, 3a, 3b".
// Uses S, $, esc, request, toast (app.js), PANES, paneActive, addTab, showTab, openPane (grid.js),
// work, viewTabs, peerTab, isFileTab, fileUrl, dropDuplicates, saveOpenFiles, renderBars, openFile (files.js).

const MD_PREFIX = 'preview:';
const MD_WAIT_MS = 150; // at most one render per 150 ms while I type (more for a long file: twice its last render)
const MD_SIDE = { 1: 2, 2: 1, 3: 4, 4: 3 }; // the pane beside each pane

const mdPreviewable = (path) => /\.(md|markdown)$/i.test(path);

// The pane of a new preview: beside the file when it is on screen (VS Code's Open Preview to the
// Side, so that I see both while I type), else where a file opens.
function mdPane(w, src) {
  for (const n of PANES) {
    const t = paneActive(w, n);
    if (t && isFileTab(t) && t.path === src) return MD_SIDE[n];
  }
  return openPane(w);
}

// As openFile (files.js), which hands it the paths that start with `preview:`.
async function openPreview(tid, src, focus = true, pane = null, show = true) {
  const w = work(tid);
  const path = MD_PREFIX + src;
  const find = () => viewTabs(w).find((t) => t.path === path);
  let tab = find();
  if (!tab) {
    const owner = w.restoring ? tid : ownerFor(tid);
    let r;
    try { r = await request('fs.read', { id: owner, path: src }); } catch (e) { toast(e.message); return; }
    if (r.text === undefined) { toast(`${src}: ${r.tooLarge ? 'too large for the preview' : 'not a text file'}.`); return; }
    tab = find(); // opened meanwhile by another click
    if (!tab) {
      tab = makePreviewTab(owner, src, r);
      addTab(work(owner), tab, pane || mdPane(w, src));
      if (isProjectWs(owner)) dropDuplicates(tab);
      saveOpenFiles(owner);
    }
  }
  if (show) showTab(tab, focus);
  return tab;
}

function makePreviewTab(tid, src, r) {
  const el = document.createElement('div');
  el.className = 'editor md-preview';
  el.tabIndex = -1; // focusable: arrows and Page Up/Down scroll it
  const body = document.createElement('div');
  body.className = 'md-body';
  el.appendChild(body);
  const tab = { tid, path: MD_PREFIX + src, src, el, body, kind: 'preview', preview: true, dirty: false, diskChanged: false, gone: false,
    diskText: r.text, version: r.version, shown: null, timer: null, stale: true, cost: 0 };
  // drawn when it is on screen: at once, or when it shows (another tab, a hidden pane, Organize mode)
  new ResizeObserver(() => { if (tab.stale && el.clientHeight) mdRender(tab); }).observe(el);
  el.addEventListener('click', (e) => mdClick(tab, e));
  return tab;
}

// the text that it shows: an editor's of the file (any task of the project: they share it), else the disk's
function mdSource(tab) {
  const t = peerTab(tab.tid, tab.src);
  return t ? t.view.state.doc.toString() : tab.diskText;
}

function mdRender(tab) {
  clearTimeout(tab.timer);
  tab.timer = null;
  if (!tab.el.isConnected) return; // closed
  if (!tab.el.clientHeight) { tab.stale = true; return; } // not on screen: when it shows
  tab.stale = false;
  const text = mdSource(tab);
  if (text === tab.shown) return;
  const t0 = performance.now();
  let html;
  try { html = CM.markdownHTML(text); } catch (e) {
    uiLog(`preview: the markdown of ${tab.src} did not render: ${e.message}`);
    html = `<pre>${esc(text)}</pre>`;
  }
  mdMorph(tab.body, mdSanitize(html, tab));
  tab.shown = text;
  tab.cost = performance.now() - t0;
}

// The previews of `src` in the project of `tid`, in every work state.
function mdPreviewsOf(tid, src) {
  const project = wsProject(tid);
  const out = [];
  for (const w of Object.values(S.work || {})) {
    for (const t of w.tabs) if (t.preview && t.src === src && wsProject(t.tid) === project) out.push(t);
  }
  return out;
}

// An edit in an editor of the file (files.js, at each change of its text, a reload too).
function previewSoon(textTab) {
  if (textTab.untitled) return;
  for (const t of mdPreviewsOf(textTab.tid, textTab.path)) {
    if (!t.timer) t.timer = setTimeout(() => mdRender(t), Math.max(MD_WAIT_MS, 2 * t.cost));
  }
}

// An editor of the file closed (files.js, destroyTab): another editor's text, else the disk's
// (my edits may have been dropped, or saved since the preview read the file).
function previewEditorGone(textTab) {
  if (textTab.untitled) return;
  for (const t of mdPreviewsOf(textTab.tid, textTab.path)) {
    if (peerTab(t.tid, t.src)) mdRender(t); else mdFromDisk(t);
  }
}

async function mdFromDisk(tab) {
  let r;
  try { r = await request('fs.read', { id: tab.tid, path: tab.src }); } catch { return; }
  if (r.text === undefined) return;
  Object.assign(tab, { diskText: r.text, version: r.version, gone: false });
  mdRender(tab);
}

// The previews on screen that follow the disk (no editor of their file is open): checked every
// 2 s, as files.js checks the file tabs.
setInterval(async () => {
  const tid = wsId();
  const w = tid && S.mode === 'dev' && S.work && S.work[tid];
  if (!w) return;
  const tabs = PANES.map((n) => paneActive(w, n)).filter((t) => t && t.preview && !peerTab(t.tid, t.src));
  if (!tabs.length) return;
  let v;
  try { v = await request('fs.versions', { id: tid, paths: tabs.map((t) => t.src) }); } catch { return; }
  for (const t of tabs) {
    const now = v[t.src];
    if (now === undefined) continue;
    if ((now === null) !== t.gone) { t.gone = now === null; renderBars(); }
    if (now !== null && now !== t.version) mdFromDisk(t);
  }
}, POLL_MS);

// ------------------------------------------------------------------ the HTML
// Only these tags and attributes stay (GitHub's set, about). A tag that is not listed is
// replaced by its content; the tags of MD_DROP go with their content. Links: http, https, mailto
// and the paths of the project; images: http, https, a few data: types, and the project's images
// (served by /file/, server.py). An `id` or `name` becomes `data-anchor`, as the headings have.

const MD_TAGS = new Set(['a', 'abbr', 'b', 'blockquote', 'br', 'caption', 'center', 'cite', 'code', 'dd', 'del', 'details', 'div',
  'dl', 'dt', 'em', 'figcaption', 'figure', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'input', 'ins', 'kbd', 'li',
  'mark', 'ol', 'p', 'pre', 'q', 's', 'samp', 'small', 'span', 'strike', 'strong', 'sub', 'summary', 'sup', 'table', 'tbody',
  'td', 'tfoot', 'th', 'thead', 'tr', 'tt', 'u', 'ul', 'var']);
const MD_DROP = new Set(['script', 'style', 'template', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'noscript',
  'svg', 'math', 'link', 'meta', 'base', 'title', 'head', 'form', 'textarea', 'select', 'button', 'audio', 'video', 'canvas', 'dialog']);
const MD_ATTRS = new Set(['align', 'alt', 'title', 'width', 'height', 'colspan', 'rowspan', 'start', 'open', 'href', 'src',
  'checked', 'disabled', 'type', 'class', 'data-anchor']);
const MD_CLASS = /^(?:ͼ\w+|language-[\w+#.-]+|task)$/; // CodeMirror's colors of code, a block's language, a task item

function mdSanitize(html, tab) {
  const doc = new DOMParser().parseFromString(html, 'text/html'); // inert: no script runs, no image loads
  mdClean(doc.body, tab);
  return doc.body;
}

function mdClean(parent, tab) {
  for (const node of [...parent.childNodes]) {
    if (node.nodeType === Node.TEXT_NODE) continue;
    if (node.nodeType !== Node.ELEMENT_NODE) { node.remove(); continue; } // comments
    const tag = node.localName;
    if (MD_DROP.has(tag) || (tag === 'input' && (node.getAttribute('type') || '').toLowerCase() !== 'checkbox')) { node.remove(); continue; }
    mdClean(node, tab);
    if (!MD_TAGS.has(tag)) { node.replaceWith(...node.childNodes); continue; }
    for (const a of [...node.attributes]) {
      if ((a.name === 'id' || a.name === 'name') && !node.hasAttribute('data-anchor')) node.setAttribute('data-anchor', a.value);
      if (!MD_ATTRS.has(a.name)) node.removeAttribute(a.name);
    }
    if (node.hasAttribute('class')) {
      const keep = node.getAttribute('class').split(/\s+/).filter((c) => MD_CLASS.test(c));
      if (keep.length) node.setAttribute('class', keep.join(' ')); else node.removeAttribute('class');
    }
    if (tag === 'input') node.setAttribute('disabled', '');
    if (node.hasAttribute('href')) mdHref(node, tab);
    if (node.hasAttribute('src')) mdSrc(node, tab);
  }
}

// The URL without the characters that the URL parser of WebKit drops ("java\tscript:" is a scheme),
// and its scheme in lower case, or '' for a path.
function mdUrl(raw) {
  const url = raw.replace(/[\u0000- \u007f]/g, '');
  const m = /^([a-z][a-z\d+.-]*):/i.exec(url);
  return { url, scheme: m ? m[1].toLowerCase() : '' };
}

function mdHref(node, tab) {
  const { url, scheme } = mdUrl(node.getAttribute('href'));
  if (!scheme) { node.setAttribute('href', url); return; } // a path of the project, or #anchor: mdClick
  if (!['http', 'https', 'mailto'].includes(scheme)) { node.removeAttribute('href'); return; }
  node.setAttribute('href', url);
  node.setAttribute('target', '_blank'); // the default browser of macOS, as the links of a PDF (pywebview)
  node.setAttribute('rel', 'noopener noreferrer');
}

function mdSrc(node, tab) {
  const { url, scheme } = mdUrl(node.getAttribute('src'));
  if (scheme === 'http' || scheme === 'https' || /^data:image\/(png|gif|jpeg|webp|avif);/i.test(url)) { node.setAttribute('src', url); return; }
  const path = !scheme && url ? mdResolve(tab.src, url) : null;
  if (path) node.setAttribute('src', fileUrl(tab.tid, path, '')); else node.removeAttribute('src');
}

// A path of the project, from a link or an image of the file `src` (a leading / is the project
// folder, as GitHub's repository root); null: outside the project.
function mdResolve(src, url) {
  let path = url.split(/[?#]/)[0];
  try { path = decodeURIComponent(path); } catch { /* as written */ }
  const parts = path.startsWith('/') ? [] : src.split('/').slice(0, -1);
  for (const p of path.split('/')) {
    if (!p || p === '.') continue;
    if (p !== '..') parts.push(p);
    else if (parts.length) parts.pop();
    else return null;
  }
  return parts.join('/');
}

// Only the top-level blocks that changed are replaced: the others keep their images (no reload,
// no flash), and the scroll position and the selection stay.
function mdMorph(old, fresh) {
  const a = [...old.childNodes];
  const b = [...fresh.childNodes];
  let i = 0;
  while (i < a.length && i < b.length && a[i].isEqualNode(b[i])) i++;
  let j = 0;
  while (j < a.length - i && j < b.length - i && a[a.length - 1 - j].isEqualNode(b[b.length - 1 - j])) j++;
  for (const n of a.slice(i, a.length - j)) n.remove();
  const before = j ? a[a.length - j] : null;
  for (const n of b.slice(i, b.length - j)) old.insertBefore(n, before);
}

// ------------------------------------------------------------------ links

function mdClick(tab, e) {
  const a = e.target.closest('a[href]');
  if (!a || a.target === '_blank') return; // a web link: the default browser
  e.preventDefault();
  const href = a.getAttribute('href');
  const i = href.indexOf('#');
  const hash = i >= 0 ? href.slice(i + 1) : '';
  const before = i >= 0 ? href.slice(0, i) : href;
  if (!before) { mdScrollTo(tab, hash); return; }
  const path = mdResolve(tab.src, before);
  if (path === null) { toast(`${before}: outside the project.`); return; }
  const view = wsId();
  if (!view) return;
  if (!mdPreviewable(path)) { openFile(view, path, true); return; }
  openPreview(view, path, true, tab.pane).then((t) => { if (t && hash) mdScrollTo(t, hash); }); // as VS Code: a markdown file opens in preview
}

function mdScrollTo(tab, hash) {
  let name = hash;
  try { name = decodeURIComponent(hash); } catch { /* as written */ }
  if (tab.stale) mdRender(tab);
  const target = tab.body.querySelector(`[data-anchor="${CSS.escape(name)}"]`)
    || tab.body.querySelector(`[data-anchor="${CSS.escape(name.toLowerCase())}"]`);
  if (target) tab.el.scrollTop = target.offsetTop - 12; // the tab is the offset parent (position: absolute)
}
