"""HTTP (the static front end, and /file/<task id>/<path>: the images and PDFs of 3a) and two
kinds of websocket on one port: /ctl (state pushed to the front end, commands back) and
/pty/<task id> (terminal bytes)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from importlib import resources
from urllib.parse import unquote, urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from .. import actions, files, selection
from .core import Core

log = logging.getLogger(__name__)

WEB = resources.files("brain_fry") / "web"
TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",  # PDF.js
    "css": "text/css; charset=utf-8",
    "png": "image/png",
    "jpg": "image/jpeg",  # the photos of the empty 4a (web/calm/)
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "wasm": "application/wasm",
    "bcmap": "application/octet-stream",
    "pfb": "application/octet-stream",
    "ttf": "font/ttf",
    "icc": "application/vnd.iccprofile",
}
LOCAL_HOSTS = ("127.0.0.1", "localhost")
TASK_ID = re.compile(r"[0-9A-Za-z-]{1,64}")


def _refused(headers: Headers, port: int) -> str | None:
    """Why a request must be refused, or None. The server listens on 127.0.0.1 only, but a web
    page that I open in a browser can still reach it in two ways. A websocket: the browser
    opens one to any address, and only the Origin header tells the page apart from Brain Fry's.
    DNS rebinding: a site points its own name at 127.0.0.1, and the browser then treats the
    server as that site; the Host header still carries the site's name. Clients that are not
    browsers (the tests, a script) send no Origin, and run as me anyway."""
    host = headers.get("Host")
    if host not in {f"{h}:{port}" for h in LOCAL_HOSTS}:
        return f"Host {host!r}"
    origin = headers.get("Origin")
    if origin is not None and origin not in {f"http://{h}:{port}" for h in LOCAL_HOSTS}:
        return f"Origin {origin!r}"
    return None


def _response(status: int, body: bytes, ctype: str, extra: tuple = ()) -> Response:
    headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"), *extra])
    return Response(status, {200: "OK", 403: "Forbidden", 404: "Not Found", 413: "Too Large"}.get(status, ""), headers, body)


def _file(core: Core, path: str) -> Response:
    """GET /file/<task id>/<path in the task's project>: an image or a PDF for 3a. The page asks
    with `?v=<version>`, so that a file that changed on disk is never an old copy."""
    parts = urlsplit(path).path.split("/", 3)  # '', 'file', task id, path
    if len(parts) < 4 or not TASK_ID.fullmatch(parts[2]):
        return _response(404, b"not found\n", "text/plain")
    rel = unquote(parts[3])
    try:
        p, ctype = core.viewable(parts[2], rel)
        size = p.stat().st_size
        if size > files.MAX_VIEW_BYTES:
            return _response(413, b"too large\n", "text/plain")
        body = p.read_bytes()
    except PermissionError:
        log.warning("file %s: outside the project, refused", rel)
        return _response(403, b"forbidden\n", "text/plain")
    except (FileNotFoundError, actions.NotFound):
        return _response(404, b"not found\n", "text/plain")
    log.debug("file %s: %d bytes", rel, len(body))
    # nosniff and sandbox: the bytes are shown as what their extension says, never run as a page
    return _response(200, body, ctype, (("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "sandbox")))


def _static(path: str) -> Response | None:
    """Any file under web/ with a known extension. No '..', no hidden files."""
    path = path.split("?")[0]
    parts = [p for p in ("index.html" if path == "/" else path).split("/") if p]
    ext = parts[-1].rsplit(".", 1)[-1] if parts and "." in parts[-1] else ""
    if not parts or ext not in TYPES or any(p.startswith(".") for p in parts):
        return None
    f = WEB.joinpath(*parts)
    if not f.is_file():
        return None
    body = f.read_bytes()
    headers = Headers([
        ("Content-Type", TYPES[ext]),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])  # fmt: skip
    return Response(200, "OK", headers, body)


def make_handlers(core: Core):
    async def process_request(connection, request):
        why = _refused(request.headers, connection.local_address[1])
        if why:
            log.warning("refused %s: %s", request.path.split("?")[0][:100], why)
            return connection.respond(403, "forbidden\n")
        if request.path == "/ctl" or request.path.startswith("/pty/"):
            return None  # websocket handshake
        if request.path.startswith("/file/"):
            return await asyncio.to_thread(_file, core, request.path)
        return _static(request.path) or connection.respond(404, "not found\n")

    async def ctl(ws) -> None:
        """Commands come in as JSON. A command with a `req` number gets a reply with the
        same number (the front end awaits it); one without gets an error message on failure."""
        core.clients.add(ws)
        log.info("front end connected (%d connection(s))", len(core.clients))
        try:
            await ws.send(json.dumps(await asyncio.to_thread(core.snapshot)))
            async for raw in ws:
                msg = json.loads(raw)
                req = msg.get("req")
                try:
                    data = await core.command(msg)
                    if req is not None:
                        await ws.send(json.dumps({"type": "reply", "req": req, "ok": True, "data": data}))
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    if isinstance(e, (FileNotFoundError, FileExistsError, NotADirectoryError, PermissionError, ValueError)):
                        log.warning("command %s failed: %s", msg.get("cmd"), error)
                    else:
                        log.exception("command %s failed", raw[:200])
                    if req is not None:
                        await ws.send(json.dumps({"type": "reply", "req": req, "ok": False, "error": error}))
                    else:
                        await ws.send(json.dumps({"type": "error", "message": error}))
        finally:
            core.clients.discard(ws)
            log.info("front end disconnected (%d connection(s) left)", len(core.clients))
            if not core.clients:  # nobody sees the selection any more: it must not go with a prompt
                selection.clear()

    async def pty(ws, key: str) -> None:
        s = core.ptys.get(key)
        if s is None:
            log.info("terminal view for %s: no such session", key)
            await ws.close(4404, "no session")
            return
        log.debug("terminal view attached to %s", key)
        q: asyncio.Queue = asyncio.Queue()
        snapshot = bytes(s.replay)  # no await between snapshot and subscribe
        s.queues.add(q)

        async def pump() -> None:
            await ws.send(snapshot)
            while (data := await q.get()) is not None:
                await ws.send(data)
            await ws.close()

        sender = asyncio.create_task(pump())
        try:
            async for raw in ws:
                m = json.loads(raw)
                if m["t"] == "in":
                    s.write(m["d"].encode("utf-8", "replace"))
                elif m["t"] == "bin":
                    s.write(m["d"].encode("latin-1"))
                elif m["t"] == "resize":
                    s.resize(int(m["rows"]), int(m["cols"]))
        finally:
            s.queues.discard(q)
            sender.cancel()

    async def handler(ws) -> None:
        path = ws.request.path
        if path == "/ctl":
            await ctl(ws)
        elif path.startswith("/pty/"):
            await pty(ws, path[len("/pty/") :])

    return handler, process_request


def start_backend(core: Core, port: int = 0) -> int:
    """Runs the asyncio loop in a daemon thread. Returns the port."""
    ready = threading.Event()
    box: dict[str, int] = {}

    async def main() -> None:
        await core.start()
        handler, process_request = make_handlers(core)
        async with serve(handler, "127.0.0.1", port, process_request=process_request, max_size=16 * 2**20) as server:
            box["port"] = next(iter(server.sockets)).getsockname()[1]
            ready.set()
            await asyncio.Future()

    threading.Thread(target=lambda: asyncio.run(main()), name="backend", daemon=True).start()
    if not ready.wait(15):
        raise RuntimeError("the backend did not start")
    return box["port"]
