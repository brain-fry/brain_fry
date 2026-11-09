"""HTTP (the static front end) and two kinds of websocket on one port:
/ctl (state pushed to the front end, commands back) and /pty/<task id> (terminal bytes)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from importlib import resources

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from .core import Core

log = logging.getLogger("brain_fry")

WEB = resources.files("brain_fry") / "web"
TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "png": "image/png",
}
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
    def process_request(connection, request):
        if request.path == "/ctl" or request.path.startswith("/pty/"):
            return None  # websocket handshake
        return _static(request.path) or connection.respond(404, "not found\n")

    async def ctl(ws) -> None:
        """Commands come in as JSON. A command with a `req` number gets a reply with the
        same number (the front end awaits it); one without gets an error message on failure."""
        core.clients.add(ws)
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
                    if not isinstance(e, (FileNotFoundError, PermissionError, ValueError)):
                        log.exception("command %s", raw[:200])
                    error = f"{type(e).__name__}: {e}"
                    if req is not None:
                        await ws.send(json.dumps({"type": "reply", "req": req, "ok": False, "error": error}))
                    else:
                        await ws.send(json.dumps({"type": "error", "message": error}))
        finally:
            core.clients.discard(ws)

    async def pty(ws, key: str) -> None:
        s = core.ptys.get(key)
        if s is None:
            await ws.close(4404, "no session")
            return
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
