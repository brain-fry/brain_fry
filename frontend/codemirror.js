// Entry point of the CodeMirror bundle. vendor.sh turns it into
// src/brain_fry/web/vendor/codemirror.js, which exposes everything below as the global `CM`.

import { basicSetup } from 'codemirror';
import { EditorView, keymap } from '@codemirror/view';
import { EditorState, Compartment } from '@codemirror/state';
import { indentWithTab } from '@codemirror/commands';
import { StreamLanguage } from '@codemirror/language';
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

const sh = () => StreamLanguage.define(shell);
const BY_EXT = {
  py: python, pyi: python,
  js: javascript, mjs: javascript, cjs: javascript,
  jsx: () => javascript({ jsx: true }),
  ts: () => javascript({ typescript: true }),
  tsx: () => javascript({ jsx: true, typescript: true }),
  json: json, jsonl: json,
  md: markdown, markdown: markdown,
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

export { basicSetup, EditorView, EditorState, Compartment, keymap, indentWithTab, unifiedMergeView, oneDark };
