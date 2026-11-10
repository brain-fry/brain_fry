"""A local test site for the browser of 3a (no network): `python site.py PORT LOG`.

/page sets a persistent cookie and a session cookie, and asks for three paths that uBlock
Origin Lite's generic rules block (/webtracking.min.js, /ad.html?x=1; /ad-server.js is not
blocked as a script) next to a control (/ok.js). Every request that arrives is logged with
its Cookie header: a blocked request never arrives. /audio plays a silent sound in a loop.
"""

import http.server
import io
import sys
import wave

PORT, LOG = int(sys.argv[1]), sys.argv[2]

PAGE = b"""<!doctype html><html><head><title>BF browser test</title></head><body style="font: 16px sans-serif">
<h1>Brain Fry browser test</h1>
<p id=c>cookies seen by the page: <span id=cookies></span></p>
<p><a href="/page2" target="_blank">opens a new tab</a> | <a href="/page2">same tab</a></p>
<script src="/ok.js"></script>
<script src="/webtracking.min.js"></script>
<script src="/ad-server.js"></script>
<iframe src="/ad.html?x=1" width=200 height=40></iframe>
<script>document.getElementById('cookies').textContent = document.cookie || '(none, or HttpOnly)';</script>
</body></html>"""


def silence() -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
        w.writeframes(bytes(16000))  # 1 s
    return b.getvalue()


WAV = silence()
AUDIO = b'<title>audio</title><audio id=a src="/silence.wav" autoplay loop></audio>'


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        with open(LOG, "a") as f:
            f.write(f"{self.path}\tcookie={self.headers.get('Cookie', '')}\n")
        self.send_response(200)
        if self.path == "/download":
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", 'attachment; filename="bf-test.txt"')
            self.end_headers()
            self.wfile.write(b"downloaded by the Brain Fry browser\n")
            return
        if self.path in ("/audio", "/silence.wav"):
            audio = self.path == "/audio"
            self.send_header("Content-Type", "text/html" if audio else "audio/wav")
            self.send_header("Content-Length", str(len(AUDIO if audio else WAV)))
            self.end_headers()
            self.wfile.write(AUDIO if audio else WAV)
            return
        if self.path.startswith("/page"):
            self.send_header("Content-Type", "text/html")
            if "persist=" not in self.headers.get("Cookie", ""):
                self.send_header("Set-Cookie", "persist=1; Max-Age=86400; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Set-Cookie", "session=1; Path=/; SameSite=Lax")
            self.end_headers()
            self.wfile.write(PAGE if self.path.startswith("/page?") or self.path == "/page" else b"<title>page 2</title>page 2")
        else:
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(b"/* ok */")


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
