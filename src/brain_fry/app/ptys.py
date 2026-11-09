"""PTY children: Claude Code sessions (4a) and, later, terminal tabs (3b).

Spawning: the app has threads (Cocoa main thread, asyncio thread, digest worker), so
`pty.fork()` (Python code between fork and exec) is unsafe. subprocess.Popen forks and
execs in C. A 3-line Python wrapper then makes the PTY its controlling terminal
(TIOCSCTTY, so that Ctrl-C and SIGWINCH reach the child) and execs the command.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable

REPLAY_BYTES = 4 * 1024 * 1024

WRAPPER = "import fcntl,os,sys,termios\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\nos.execvp(sys.argv[1], sys.argv[1:])\n"


def set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 2), max(cols, 10), 0, 0))


def spawn(argv: list[str], cwd: str, env: dict[str, str], rows: int, cols: int) -> tuple[subprocess.Popen, int]:
    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        if hasattr(termios, "IUTF8"):
            attrs[0] |= termios.IUTF8  # backspace removes a whole UTF-8 character
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
        set_size(master, rows, cols)
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", WRAPPER, *argv],
            stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    return proc, master


class PtySession:
    """One child in a PTY, read by the asyncio loop. Keeps the last few MB of output, so
    that a terminal view that (re)connects can replay it."""

    def __init__(self, key: str, gen: int, argv: list[str], cwd: str, env: dict[str, str],
                 rows: int, cols: int, on_exit: Callable[[PtySession], None]):  # fmt: skip
        self.key, self.gen, self.argv, self.cwd = key, gen, argv, cwd
        self.kind, self.task, self.title = "claude", key, ""  # set by the caller for shells
        self.proc, self.fd = spawn(argv, cwd, env, rows, cols)
        self.on_exit = on_exit
        self.replay = bytearray()
        self.queues: set[asyncio.Queue] = set()
        self.alive = True
        self.last_output = time.time()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.add_reader(self.fd, self._readable, loop)

    def _readable(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the child side is closed
            data = b""
        if not data:
            loop.remove_reader(self.fd)
            self._exited()
            return
        self.last_output = time.time()
        self._emit(data)

    def _emit(self, data: bytes) -> None:
        self.replay += data
        if len(self.replay) > REPLAY_BYTES:  # may cut an escape sequence at the very start
            del self.replay[: len(self.replay) - REPLAY_BYTES]
        for q in self.queues:
            q.put_nowait(data)

    def _exited(self) -> None:
        if not self.alive:
            return
        self.alive = False
        self._emit(b"\r\n\x1b[2m[session ended]\x1b[0m\r\n")
        for q in self.queues:
            q.put_nowait(None)  # tells the websocket to close
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self.on_exit(self)

    def write(self, data: bytes) -> None:
        if not self.alive:
            return
        while data:
            try:
                n = os.write(self.fd, data)
            except BlockingIOError:
                time.sleep(0.01)  # the child is not reading (a huge paste); rare
                continue
            except OSError:
                return
            data = data[n:]

    def resize(self, rows: int, cols: int) -> None:
        if self.alive:
            try:
                set_size(self.fd, rows, cols)
            except OSError:
                pass

    def hangup(self) -> None:
        """SIGHUP to the child's process group, like closing a terminal window."""
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
        except OSError:
            pass

    def kill(self, timeout: float = 3.0) -> None:
        """Blocking: SIGHUP, then SIGTERM, then SIGKILL. For shutdown."""
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except OSError:  # gone, or only a zombie left (macOS then says EPERM)
                pass
            try:
                self.proc.wait(timeout=timeout / 3)
                return
            except subprocess.TimeoutExpired:
                continue
