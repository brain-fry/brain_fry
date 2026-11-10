// The system WebKit (macOS 26) cannot iterate a ReadableStream with `for await`, and PDF.js 6
// does it, also in its legacy build: to read the text of a page (find), and to decompress some
// images in the worker. Loaded by files.js before PDF.js, and by pdf_worker.mjs in the worker.
// The standard behaviour: read until the end, then release the reader; leaving the loop early
// cancels the stream, unless preventCancel.
if (typeof ReadableStream !== 'undefined' && !ReadableStream.prototype[Symbol.asyncIterator]) {
  ReadableStream.prototype.values = async function* values({ preventCancel = false } = {}) {
    const reader = this.getReader();
    let done = false;
    try {
      while (!done) {
        const r = await reader.read();
        done = r.done;
        if (!done) yield r.value;
      }
    } finally {
      if (!done && !preventCancel) reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  };
  ReadableStream.prototype[Symbol.asyncIterator] = ReadableStream.prototype.values;
}
