// Markdown --> HTML for the preview tabs of column 3 (web/mdpreview.js). The parser is the one
// that the editor uses for markdown (@lezer/markdown, already in the CodeMirror bundle), with
// GitHub's additions: tables, task lists, strikethrough, bare links. No new library: vendor.sh
// could not download one (2026-11-14), and one grammar for the editor and its preview agree.
// The raw HTML of the file is passed through as it is: the page sanitizes the result
// (mdpreview.js) before it shows it. Headings get `data-anchor` (their GitHub slug), not `id`:
// an id in the page could take the place of one of the app's own elements.

import { parser, GFM } from '@lezer/markdown';

const mdParser = parser.configure(GFM);

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
export const escHTML = (s) => s.replace(/[&<>"]/g, (c) => ESC[c]);

const unescapeMd = (s) => s.replace(/\\([!-/:-@[-`{-~])/g, '$1'); // \( --> (, in a URL or a title
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
function decodeEntity(s) { // for plain text only (alt, slugs); the HTML keeps the entity as it is
  const m = /^&(?:#(\d+)|#[xX]([\da-fA-F]+)|(\w+));$/.exec(s);
  if (!m) return s;
  if (m[1] || m[2]) {
    const n = m[1] ? Number(m[1]) : parseInt(m[2], 16);
    return n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : '�';
  }
  return ENTITIES[m[3]] ?? s;
}

const normLabel = (s) => s.trim().replace(/\s+/g, ' ').toLowerCase();
const cleanUrl = (s) => unescapeMd(s.trim().replace(/^<(.*)>$/s, '$1'));
const cleanTitle = (s) => unescapeMd(s.slice(1, -1));
const blankBetween = (text, a, b) => /\n[ \t>]*\n/.test(text.slice(a, b)); // a blank line (also inside a quote)

// GitHub's slug of a heading: lower case, punctuation removed, spaces to hyphens
const slugOf = (s) => s.trim().toLowerCase().replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, '').replace(/\s/g, '-');

function kids(n) {
  const out = [];
  for (let c = n.firstChild; c; c = c.nextSibling) out.push(c);
  return out;
}

const MARKS = new Set(['QuoteMark', 'ListMark', 'HeaderMark', 'EmphasisMark', 'CodeMark', 'LinkMark',
  'StrikethroughMark', 'TableDelimiter', 'TaskMarker']);

// highlight(code, lang): the HTML of a fenced block's text colored, or null (escaped as it is)
export function markdownToHTML(text, highlight = null) {
  const tree = mdParser.parse(text);
  const src = (n) => text.slice(n.from, n.to);

  const refs = new Map(); // the link reference definitions: [label]: url "title"
  tree.iterate({ enter(n) {
    if (n.name !== 'LinkReference') return n.name === 'Document' || n.name === 'Blockquote' || n.name.endsWith('List') || n.name === 'ListItem';
    const node = n.node;
    const label = node.getChild('LinkLabel');
    const url = node.getChild('URL');
    const title = node.getChild('LinkTitle');
    const key = label && normLabel(src(label).slice(1, -1));
    if (key && !refs.has(key)) refs.set(key, { href: url ? cleanUrl(src(url)) : '', title: title ? cleanTitle(src(title)) : '' });
    return false;
  } });

  const slugs = new Map(); // a slug seen n times: the next one is slug-n, as GitHub
  const anchor = (plain) => {
    const base = slugOf(plain);
    const n = slugs.get(base) || 0;
    slugs.set(base, n + 1);
    return n ? `${base}-${n}` : base;
  };

  // ---- inline content: the children of `parent` between `from` and `to`, and the text between them

  function inline(parent, from, to, plain = false) {
    let out = '';
    let pos = from;
    let afterQuote = false;
    const gap = (a, b) => {
      if (b <= a) return;
      let s = text.slice(a, b).replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]*/g, '\n'); // the indentation of continuation lines
      if (afterQuote) s = s.replace(/^[ \t]+/, '');
      out += plain ? s : escHTML(s);
    };
    for (let c = parent.firstChild; c; c = c.nextSibling) {
      if (c.to <= from || c.from >= to) continue;
      gap(pos, c.from);
      out += inlineNode(c, plain);
      afterQuote = c.name === 'QuoteMark';
      pos = c.to;
    }
    gap(pos, to);
    return out;
  }

  function wrapped(n, tag, plain) { // emphasis, strong, strikethrough: the content between the marks
    const ks = kids(n);
    const inner = inline(n, ks.length ? ks[0].to : n.from, ks.length > 1 ? ks[ks.length - 1].from : n.to, plain);
    return plain ? inner : `<${tag}>${inner}</${tag}>`;
  }

  function linkHref(url) { // a bare link (GFM) or <...>
    if (/^[a-z][a-z\d+.-]*:/i.test(url)) return url;
    if (/^www\./i.test(url)) return `http://${url}`;
    return url.includes('@') ? `mailto:${url}` : url;
  }

  function link(n, plain) {
    const ks = kids(n);
    const marks = ks.filter((k) => k.name === 'LinkMark');
    const open = marks[0];
    const close = marks.find((k) => src(k) === ']');
    if (!open || !close) return plain ? src(n) : escHTML(src(n));
    const isImage = n.name === 'Image';
    const inner = inline(n, open.to, close.from, plain || isImage);
    let href = '';
    let title = '';
    const url = n.getChild('URL');
    if (url || marks.some((k) => src(k) === '(')) { // [text](url "title")
      href = url ? cleanUrl(src(url)) : '';
      const t = n.getChild('LinkTitle');
      title = t ? cleanTitle(src(t)) : '';
    } else { // [text][label], [text][], [text]
      const label = n.getChild('LinkLabel');
      const ref = refs.get(normLabel(label && src(label) !== '[]' ? src(label).slice(1, -1) : text.slice(open.to, close.from)));
      if (!ref) { // no definition: not a link, its text as written
        const rest = text.slice(close.from, n.to);
        return plain ? `${src(open)}${inner}${rest}` : `${escHTML(src(open))}${inner}${escHTML(rest)}`;
      }
      ({ href, title } = ref);
    }
    if (plain) return inner;
    const t = title ? ` title="${escHTML(title)}"` : '';
    if (isImage) return `<img src="${escHTML(href)}" alt="${escHTML(inner)}"${t}>`;
    return `<a href="${escHTML(href)}"${t}>${inner}</a>`;
  }

  function inlineNode(c, plain) {
    if (MARKS.has(c.name)) return '';
    switch (c.name) {
      case 'Emphasis': return wrapped(c, 'em', plain);
      case 'StrongEmphasis': return wrapped(c, 'strong', plain);
      case 'Strikethrough': return wrapped(c, 'del', plain);
      case 'InlineCode': {
        const ks = kids(c);
        let code = text.slice(ks.length ? ks[0].to : c.from, ks.length > 1 ? ks[ks.length - 1].from : c.to).replace(/\n/g, ' ');
        if (/^ .* $/.test(code) && code.trim()) code = code.slice(1, -1); // one space each side is dropped (CommonMark)
        return plain ? code : `<code>${escHTML(code)}</code>`;
      }
      case 'Escape': return plain ? src(c).slice(1) : escHTML(src(c).slice(1));
      case 'Entity': return plain ? decodeEntity(src(c)) : src(c);
      case 'HardBreak': return plain ? ' ' : '<br>\n';
      case 'URL': { // a bare link (GFM)
        const u = src(c);
        return plain ? u : `<a href="${escHTML(linkHref(u))}">${escHTML(u)}</a>`;
      }
      case 'Autolink': {
        const u = c.getChild('URL');
        const s = u ? src(u) : '';
        return plain ? s : `<a href="${escHTML(linkHref(s))}">${escHTML(s)}</a>`;
      }
      case 'Link': case 'Image': return link(c, plain);
      case 'HTMLTag': return plain ? '' : src(c);
      case 'Comment': case 'ProcessingInstruction': return '';
      default: return inline(c, c.from, c.to, plain);
    }
  }

  // ---- blocks

  function blocks(n, tight = false) {
    let out = '';
    for (let c = n.firstChild; c; c = c.nextSibling) if (!MARKS.has(c.name)) out += block(c, tight);
    return out;
  }

  function heading(level, n, from, to) {
    const html = inline(n, from, to).trim();
    return `<h${level} data-anchor="${escHTML(anchor(inline(n, from, to, true)))}">${html}</h${level}>\n`;
  }

  function code(n) {
    const info = n.getChild('CodeInfo');
    const lang = info ? src(info).trim().split(/\s+/)[0].toLowerCase() : '';
    const body = n.getChildren('CodeText').map(src).join('');
    const colored = lang && highlight ? highlight(body, lang) : null;
    return `<pre><code${lang ? ` class="language-${escHTML(lang)}"` : ''}>${colored ?? escHTML(body)}</code></pre>\n`;
  }

  function list(n) {
    const items = kids(n).filter((k) => k.name === 'ListItem');
    const parts = (it) => kids(it).filter((k) => k.name !== 'ListMark');
    const loose = items.some((it, i) => (i > 0 && blankBetween(text, items[i - 1].to, it.from))
      || parts(it).some((b, j, bs) => j > 0 && blankBetween(text, bs[j - 1].to, b.from)));
    const ordered = n.name === 'OrderedList';
    const first = items.length && items[0].getChild('ListMark');
    const start = ordered && first ? parseInt(src(first), 10) : 1;
    const lis = items.map((it) => {
      const ps = parts(it);
      const task = ps.length && ps[0].name === 'Task';
      const inner = ps.map((b) => (b.name === 'Task' ? taskItem(b, !loose) : block(b, !loose))).join('');
      return `<li${task ? ' class="task"' : ''}>${inner}</li>\n`;
    }).join('');
    const tag = ordered ? 'ol' : 'ul';
    return `<${tag}${ordered && start !== 1 ? ` start="${start}"` : ''}>\n${lis}</${tag}>\n`;
  }

  function taskItem(n, tight) {
    const marker = n.getChild('TaskMarker');
    const checked = marker && /x/i.test(src(marker));
    const html = `<input type="checkbox" disabled${checked ? ' checked' : ''}> ${inline(n, marker ? marker.to : n.from, n.to).trim()}`;
    return tight ? html : `<p>${html}</p>\n`;
  }

  function table(n) {
    const ks = kids(n);
    const header = ks.find((k) => k.name === 'TableHeader');
    const delim = ks.find((k) => k.name === 'TableDelimiter');
    const aligns = delim ? src(delim).trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((s) => {
      s = s.trim();
      return s.startsWith(':') && s.endsWith(':') ? 'center' : s.endsWith(':') ? 'right' : s.startsWith(':') ? 'left' : '';
    }) : [];
    const cells = (row) => { // the text between the pipes; a pipe at either end opens or closes the row
      const ds = kids(row).filter((k) => k.name === 'TableDelimiter');
      const bounds = [row.from, ...ds.flatMap((d) => [d.from, d.to]), row.to];
      const segs = [];
      for (let i = 0; i < bounds.length; i += 2) segs.push([bounds[i], bounds[i + 1]]);
      if (segs.length > 1 && !text.slice(...segs[0]).trim()) segs.shift();
      if (segs.length > 1 && !text.slice(...segs[segs.length - 1]).trim()) segs.pop();
      return segs.map(([a, b]) => inline(row, a, b).trim());
    };
    const width = header ? cells(header).length : 0;
    const tr = (row, tag) => {
      const cs = cells(row);
      return `<tr>${Array.from({ length: width }, (_, i) => {
        const al = aligns[i] ? ` align="${aligns[i]}"` : '';
        return `<${tag}${al}>${cs[i] ?? ''}</${tag}>`;
      }).join('')}</tr>\n`;
    };
    const body = ks.filter((k) => k.name === 'TableRow').map((r) => tr(r, 'td')).join('');
    return `<table>\n<thead>\n${header ? tr(header, 'th') : ''}</thead>\n${body ? `<tbody>\n${body}</tbody>\n` : ''}</table>\n`;
  }

  function block(n, tight) {
    const h = /^ATXHeading(\d)$/.exec(n.name);
    if (h) {
      const marks = n.getChildren('HeaderMark');
      return heading(Number(h[1]), n, marks.length ? marks[0].to : n.from, marks.length > 1 ? marks[marks.length - 1].from : n.to);
    }
    const s = /^SetextHeading(\d)$/.exec(n.name);
    if (s) {
      const mark = n.getChild('HeaderMark');
      return heading(Number(s[1]), n, n.from, mark ? mark.from : n.to);
    }
    switch (n.name) {
      case 'Paragraph': {
        const html = inline(n, n.from, n.to).trim();
        return tight ? html : `<p>${html}</p>\n`;
      }
      case 'Blockquote': return `<blockquote>\n${blocks(n)}</blockquote>\n`;
      case 'BulletList': case 'OrderedList': return list(n);
      case 'FencedCode': case 'CodeBlock': return code(n);
      case 'HorizontalRule': return '<hr>\n';
      case 'Table': return table(n);
      case 'Task': return taskItem(n, tight);
      case 'HTMLBlock': { // as written, without the > of an enclosing quote
        let out = '';
        let pos = n.from;
        for (const q of n.getChildren('QuoteMark')) { out += text.slice(pos, q.from); pos = q.to; }
        return `${out}${text.slice(pos, n.to)}\n`;
      }
      case 'LinkReference': case 'CommentBlock': case 'ProcessingInstructionBlock': return '';
      default: return `<p>${escHTML(src(n))}</p>\n`;
    }
  }

  return blocks(tree.topNode);
}
