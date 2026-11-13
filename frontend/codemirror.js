// Entry point of the CodeMirror bundle. vendor.sh turns it into
// src/brain_fry/web/vendor/codemirror.js, which exposes everything below as the global `CM`.

import { basicSetup } from 'codemirror';
import { EditorView, keymap, ViewPlugin, Decoration } from '@codemirror/view';
import { EditorState, EditorSelection, Compartment, Prec } from '@codemirror/state';
import { indentWithTab } from '@codemirror/commands';
import { StreamLanguage, syntaxTree } from '@codemirror/language';
import { openSearchPanel, searchPanelOpen, selectMatches, SearchQuery } from '@codemirror/search';
import { unifiedMergeView } from '@codemirror/merge';
import { oneDark } from '@codemirror/theme-one-dark';
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
const inlineCodeMark = Decoration.mark({ class: 'cm-inline-code' });
const backtickMark = Decoration.mark({ class: 'cm-inline-code-mark' });
function inlineCodeMarks(view) {
  const marks = [];
  const tree = syntaxTree(view.state);
  for (const { from, to } of view.visibleRanges) {
    tree.iterate({ from, to, enter(node) {
      if (node.name !== 'InlineCode') return;
      marks.push(inlineCodeMark.range(node.from, node.to));
      for (let c = node.node.firstChild; c; c = c.nextSibling) {
        if (c.name === 'CodeMark') marks.push(backtickMark.range(c.from, c.to));
      }
      return false;
    } });
  }
  return Decoration.set(marks, true);
}
const inlineCode = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = inlineCodeMarks(view); }
  update(u) {
    if (u.docChanged || u.viewportChanged || syntaxTree(u.startState) !== syntaxTree(u.state)) this.decorations = inlineCodeMarks(u.view);
  }
}, { decorations: (v) => v.decorations });
const md = () => [markdown(), inlineCode];

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
