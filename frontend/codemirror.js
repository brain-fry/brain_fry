// Entry point of the CodeMirror bundle. vendor.sh turns it into
// src/brain_fry/web/vendor/codemirror.js, which exposes everything below as the global `CM`.

import { basicSetup } from 'codemirror';
import { EditorView, keymap, ViewPlugin, Decoration } from '@codemirror/view';
import { EditorState, EditorSelection, Compartment, Prec } from '@codemirror/state';
import { indentWithTab } from '@codemirror/commands';
import { StreamLanguage, syntaxTree, HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { openSearchPanel, searchPanelOpen, selectMatches, SearchQuery } from '@codemirror/search';
import { unifiedMergeView } from '@codemirror/merge';
import { oneDark, oneDarkHighlightStyle } from '@codemirror/theme-one-dark';
import { highlightCode } from '@lezer/highlight';
import { StyleModule } from 'style-mod';
import { markdownToHTML, escHTML } from './markdown_html.js';
import { python } from '@codemirror/lang-python';
import { javascript } from '@codemirror/lang-javascript';
import { json } from '@codemirror/lang-json';
import { markdown } from '@codemirror/lang-markdown';
import { html } from '@codemirror/lang-html';
import { css } from '@codemirror/lang-css';
import { yaml } from '@codemirror/lang-yaml';
import { sql } from '@codemirror/lang-sql';
import { shell } from '@codemirror/legacy-modes/mode/shell';
import { toml } from '@codemirror/legacy-modes/mode/toml';

// Inline code in markdown (asked by Renaud on 2026-11-13: it did not stand out; One Dark colors
// only its backticks). Marks each inline code, backticks included (`cm-inline-code`), and its
// backticks (`cm-inline-code-mark`); app.css draws them. A decoration, not a highlight style:
// the grammar gives the same tag (monospace) to the text of a fenced block without a language,
// and a tag's style would not cover the backticks, which are child nodes.
// Bold (asked by Renaud on 2026-11-15, option 1B of a mockup): `cm-bold` on the text between
// the `**` (or `__`) of each bold, marks excluded; app.css gives it a faint yellow background.
// A bold inside a bold is not marked again (the yellow would double).
const inlineCodeMark = Decoration.mark({ class: 'cm-inline-code' });
const backtickMark = Decoration.mark({ class: 'cm-inline-code-mark' });
const boldMark = Decoration.mark({ class: 'cm-bold' });
function inlineMarks(view) {
  const marks = [];
  const tree = syntaxTree(view.state);
  let inBold = 0;
  for (const { from, to } of view.visibleRanges) {
    tree.iterate({ from, to, enter(node) {
      if (node.name === 'StrongEmphasis') {
        if (!inBold++) {
          const first = node.node.firstChild, last = node.node.lastChild;
          const a = first && first.name === 'EmphasisMark' ? first.to : node.from;
          const b = last && last !== first && last.name === 'EmphasisMark' ? last.from : node.to;
          if (b > a) marks.push(boldMark.range(a, b));
        }
        return;
      }
      if (node.name !== 'InlineCode') return;
      marks.push(inlineCodeMark.range(node.from, node.to));
      for (let c = node.node.firstChild; c; c = c.nextSibling) {
        if (c.name === 'CodeMark') marks.push(backtickMark.range(c.from, c.to));
      }
      return false;
    }, leave(node) {
      if (node.name === 'StrongEmphasis') inBold--;
    } });
  }
  return Decoration.set(marks, true);
}
const inlineCode = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = inlineMarks(view); }
  update(u) {
    if (u.docChanged || u.viewportChanged || syntaxTree(u.startState) !== syntaxTree(u.state)) this.decorations = inlineMarks(u.view);
  }
}, { decorations: (v) => v.decorations });
// Colors of markdown on top of One Dark (asked by Renaud on 2026-11-15). Prec.high mounts this
// CSS after One Dark's, so its colors win where both style a token.
// Headings: One Dark's blue (One Dark draws them coral, the `#` green), on the whole heading
// line, `#` included, as VS Code. heading1-6 only, not `heading`: the header row of a GFM table
// also has `heading`.
// HTML in markdown (option 1 of a mockup): the brackets `<` `>` `</` `/>` in One Dark's yellow,
// the tag names, attributes, `=` and values in its gray (One Dark: tag names yellow, brackets
// plain, attributes coral, values green, `=` cyan). The text between the tags is unchanged.
const mdStyle = Prec.high(syntaxHighlighting(HighlightStyle.define([
  { tag: [tags.heading1, tags.heading2, tags.heading3, tags.heading4, tags.heading5, tags.heading6], color: '#61afef' },
  { tag: tags.angleBracket, color: '#e5c07b' },
  { tag: [tags.tagName, tags.attributeName, tags.attributeValue, tags.definitionOperator], color: '#7d8799' },
])));
const md = () => [markdown(), inlineCode, mdStyle];

const sh = () => StreamLanguage.define(shell);
const BY_EXT = {
  py: python, pyi: python,
  js: javascript, mjs: javascript, cjs: javascript,
  jsx: () => javascript({ jsx: true }),
  ts: () => javascript({ typescript: true }),
  tsx: () => javascript({ jsx: true, typescript: true }),
  json: json, jsonl: json,
  md: md, markdown: md,
  html: html, htm: html,
  css: css,
  yaml: yaml, yml: yaml,
  sql: sql,
  sh: sh, bash: sh, zsh: sh,
  toml: () => StreamLanguage.define(toml),
};

// The language extension for a file name, or none.
export function languageFor(name) {
  const base = name.split('/').pop();
  const ext = base.includes('.') ? base.split('.').pop().toLowerCase() : '';
  const make = BY_EXT[ext];
  return make ? make() : [];
}

// The markdown preview (web/mdpreview.js): the HTML of a markdown text, its fenced blocks colored
// as One Dark colors them in the editor, for the languages of the editor (```py, ```python ...).
const FENCE_ALIAS = { python: 'py', javascript: 'js', typescript: 'ts', shell: 'sh', console: 'sh', jsonc: 'json' };
function highlightFence(code, lang) {
  const make = BY_EXT[FENCE_ALIAS[lang] || lang];
  if (!make) return null;
  let ext = make();
  if (Array.isArray(ext)) ext = ext[0]; // markdown: [language, the inline code marks]
  const parser = ext && (ext.language || ext).parser; // a LanguageSupport, or a StreamLanguage
  if (!parser) return null;
  if (typeof document !== 'undefined' && oneDarkHighlightStyle.module) StyleModule.mount(document, oneDarkHighlightStyle.module);
  let out = '';
  highlightCode(code, parser.parse(code), oneDarkHighlightStyle,
    (text, classes) => { out += classes ? `<span class="${classes}">${escHTML(text)}</span>` : escHTML(text); },
    () => { out += '\n'; });
  return out;
}
export const markdownHTML = (text) => markdownToHTML(text, highlightFence);

// Cmd+G (asked by Renaud on 2026-11-11): every occurrence selected, to edit them all at once, as
// VS Code's Select All Occurrences (Cmd+Shift+L). From the find field: every match of the query,
// and the keyboard back to the text. Else every occurrence of the selected text (case-sensitive).
// Nothing selected, or a read-only text (a thread tab): nothing (Renaud removed both). It always
// takes the key: CodeMirror's own Cmd+G (find next) never runs. At most 1000, as CodeMirror's commands.
export function selectAllOccurrences(view) {
  const { state } = view;
  if (state.readOnly) return true;
  const panel = view.dom.querySelector('.cm-search');
  if (searchPanelOpen(state) && panel && panel.contains(view.root.activeElement)) {
    if (selectMatches(view)) view.focus();
    return true;
  }
  const { from, to } = state.selection.main;
  if (from === to) return true;
  const query = new SearchQuery({ search: state.sliceDoc(from, to), caseSensitive: true, literal: true });
  const ranges = [];
  let mainIndex = 0;
  for (let cur = query.getCursor(state); !cur.next().done;) {
    if (ranges.length >= 1000) return true;
    if (cur.value.from === from) mainIndex = ranges.length;
    ranges.push(EditorSelection.range(cur.value.from, cur.value.to));
  }
  if (ranges.length) view.dispatch({ selection: EditorSelection.create(ranges, mainIndex), userEvent: 'select.search.matches' });
  return true;
}

export { basicSetup, EditorView, EditorState, Compartment, Prec, keymap, indentWithTab, unifiedMergeView, oneDark, openSearchPanel };
