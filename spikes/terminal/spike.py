# /// script
# requires-python = ">=3.12"
# dependencies = ["pywebview>=5.3", "websockets>=14"]
# ///
"""Spike (build step 0): Claude Code in xterm.js inside pywebview (WKWebView).

One window, one PTY, one command. Answers: does Claude Code work in this stack?

  uv run spikes/terminal/spike.py                  # claude, in the current folder
  uv run spikes/terminal/spike.py -- bash -l       # any other command
  uv run spikes/terminal/spike.py --browser        # no window: prints a URL to open in a browser
  uv run spikes/terminal/spike.py --selftest 8     # types a word after 8 s, prints the screen text, quits
  uv run spikes/terminal/spike.py --debug          # right click --> Inspect Element
"""

import argparse
import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import sys
import termios
import threading
import time
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

HERE = Path(__file__).parent
REPLAY_BYTES = 4 * 1024 * 1024

# Variables that Claude Code and VS Code set for their own children. A claude started
# from inside them would think it is nested or attached to an IDE.
STRIP_PREFIXES = ("CLAUDE", "VSCODE_", "TERM_PROGRAM", "ELECTRON_")


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(STRIP_PREFIXES)}
    env.update(TERM="xterm-256color", COLORTERM="truecolor", LANG=env.get("LANG", "en_US.UTF-8"))
    return env


def set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Session:
    """One child process in a PTY, plus the output that a new client gets replayed."""

    def __init__(self, argv: list[str], cwd: str):
        env = child_env()
        pid, fd = pty.fork()  # before any thread starts, see DESIGN.md "Still open"
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        set_size(fd, 40, 120)
        self.replay = bytearray()
        self.queues: set[asyncio.Queue] = set()

    def on_readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except OSError:  # EIO once the child has exited
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.fd)
            data = b"\r\n\x1b[7m[process exited]\x1b[0m\r\n"
        self.replay += data
        if len(self.replay) > REPLAY_BYTES:  # may cut an escape sequence; fine for a spike
            del self.replay[: len(self.replay) - REPLAY_BYTES]
        for q in self.queues:
            q.put_nowait(data)

    def write(self, data: bytes) -> None:
        while data:
            n = os.write(self.fd, data)
            data = data[n:]

    def kill(self) -> None:
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except OSError:  # gone, or only a zombie left (macOS then says EPERM)
                pass
            for _ in range(10):
                if os.waitpid(self.pid, os.WNOHANG)[0]:
                    return
                time.sleep(0.1)


def make_server(session: Session):
    def process_request(connection, request):
        if request.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            headers = Headers(
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                ]
            )
            return Response(200, "OK", headers, body)
        if request.path != "/ws":
            return connection.respond(404, "not found\n")
        return None  # go on with the websocket handshake

    async def handler(ws):
        q: asyncio.Queue = asyncio.Queue()
        snapshot = bytes(session.replay)  # no await between snapshot and subscribe
        session.queues.add(q)

        async def pump():
            await ws.send(snapshot)
            while True:
                await ws.send(await q.get())

        sender = asyncio.create_task(pump())
        try:
            async for msg in ws:
                m = json.loads(msg)
                if m["t"] == "in":
                    session.write(m["d"].encode("utf-8", "replace"))
                elif m["t"] == "bin":
                    session.write(m["d"].encode("latin-1"))
                elif m["t"] == "resize":
                    set_size(session.fd, int(m["rows"]), int(m["cols"]))
        finally:
            session.queues.discard(q)
            sender.cancel()

    return handler, process_request


def start_backend(session: Session) -> int:
    ready = threading.Event()
    box: dict[str, int] = {}

    async def main():
        asyncio.get_running_loop().add_reader(session.fd, session.on_readable)
        handler, process_request = make_server(session)
        async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
            box["port"] = next(iter(server.sockets)).getsockname()[1]
            ready.set()
            await asyncio.Future()

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    ready.wait(10)
    return box["port"]


DUMP_JS = """
(() => {
  const b = term.buffer.active, lines = [];
  for (let i = 0; i < b.length; i++) lines.push(b.getLine(i).translateToString(true));
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return JSON.stringify({cols: term.cols, rows: term.rows, renderer, errors: window.__errors,
                         text: lines.join('\\n')});
})()
"""


def selftest(window, seconds: float) -> None:
    time.sleep(seconds)
    # goes through the same path as a key press: xterm.js onData --> websocket --> PTY
    window.evaluate_js("term.input('hello from the selftest', true)")
    time.sleep(2)
    result = window.evaluate_js(DUMP_JS)
    info = json.loads(result) if isinstance(result, str) else result
    print(f"size {info['cols']}x{info['rows']}, renderer {info['renderer']}, JS errors {info['errors']}")
    print("-" * 40)
    print(info["text"])
    window.destroy()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--selftest", type=float, metavar="SECONDS")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    argv = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    argv = argv or ["claude"]

    session = Session(argv, a.cwd)
    try:
        url = f"http://127.0.0.1:{start_backend(session)}/"
        if a.browser:
            print(f"open {url}   (Ctrl-C to quit)")
            threading.Event().wait()
        import webview  # after the fork, so that Cocoa is not loaded in the child

        window = webview.create_window(f"bf spike: {' '.join(argv)}", url, width=1100, height=760)
        if a.selftest is not None:
            webview.start(selftest, (window, a.selftest), debug=a.debug)
        else:
            webview.start(debug=a.debug)
    except KeyboardInterrupt:
        pass
    finally:
        session.kill()


if __name__ == "__main__":
    sys.exit(main())
