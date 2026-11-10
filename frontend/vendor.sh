#!/bin/bash
# Builds src/brain_fry/web/vendor/ (xterm.js, a CodeMirror bundle and PDF.js). Run it after a change
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

# PDF.js: the library, its worker and its page viewer component (for the PDF tabs of 3a), plus
# what it loads on demand: character maps and standard fonts (PDFs that do not embed their
# fonts), the wasm decoders of JPEG 2000 and JBIG2 images, a color profile. Not copied: the
# full viewer app, the decoders without wasm, the JS engine for PDF forms (scripting is off).
# The legacy build, with polyfills: the modern one needs features that the system WebKit lacks
# (macOS 26: no async iteration of a ReadableStream, so getTextContent and find fail).
pdf="$build/node_modules/pdfjs-dist"
rm -rf "$out/pdfjs"
mkdir -p "$out/pdfjs/wasm"
cp "$pdf/legacy/build/pdf.min.mjs" "$pdf/legacy/build/pdf.worker.min.mjs" "$pdf/legacy/web/pdf_viewer.css" "$pdf/LICENSE" "$out/pdfjs/"
(cd "$build" && npx esbuild "$pdf/legacy/web/pdf_viewer.mjs" --minify --format=esm --legal-comments=none \
  --outfile="$out/pdfjs/pdf_viewer.min.mjs")
cp -R "$pdf/legacy/web/images" "$pdf/cmaps" "$pdf/standard_fonts" "$pdf/iccs" "$out/pdfjs/"
cp "$pdf/wasm/jbig2.wasm" "$pdf/wasm/openjpeg.wasm" "$pdf/wasm/qcms_bg.wasm" "$pdf"/wasm/LICENSE_* "$out/pdfjs/wasm/"
du -sh "$out"/*
