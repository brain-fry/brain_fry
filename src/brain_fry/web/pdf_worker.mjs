// The PDF.js worker, after the polyfill that it needs in WebKit. Imports run in order.
import './pdf_polyfill.mjs';
import './vendor/pdfjs/pdf.worker.min.mjs';
