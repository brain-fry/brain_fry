#!/bin/bash
# Builds src/brain_fry/web/vendor/ (xterm.js and a CodeMirror bundle). Run it after a change
# to package.json or codemirror.js, and commit the result. Brain Fry needs no Node at runtime.
#
# node_modules lives in ~/.cache, outside SWITCHdrive: thousands of files that npm can
# download again, with no need for a sync or a backup.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
out="$here/../src/brain_fry/web/vendor"
build="${XDG_CACHE_HOME:-$HOME/.cache}/brain_fry/vendor-build"

mkdir -p "$build" "$out/xterm"
cp "$here/package.json" "$here/codemirror.js" "$build/"
if [ -f "$here/package-lock.json" ]; then
  cp "$here/package-lock.json" "$build/"
  (cd "$build" && npm ci --no-audit --no-fund)
else
  (cd "$build" && npm install --no-audit --no-fund)
fi
cp "$build/package-lock.json" "$here/"

(cd "$build" && npx esbuild codemirror.js --bundle --minify --format=iife --global-name=CM \
  --legal-comments=none --outfile="$out/codemirror.js")

cp "$build/node_modules/@xterm/xterm/lib/xterm.js" "$build/node_modules/@xterm/xterm/css/xterm.css" "$out/xterm/"
for addon in fit search webgl unicode11; do
  cp "$build/node_modules/@xterm/addon-$addon/lib/addon-$addon.js" "$out/xterm/"
done
ls -la "$out" "$out/xterm"
