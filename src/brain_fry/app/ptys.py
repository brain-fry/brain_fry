"""PTY children: Claude Code sessions (4a) and terminal tabs (3b).

Spawning: the app has threads (Cocoa main thread, asyncio thread, digest worker), so
`pty.fork()` (Python code between fork and exec) is unsafe. subprocess.Popen forks and
execs in C. A 3-line Python wrapper then makes the PTY its controlling terminal
(TIOCSCTTY, so that Ctrl-C and SIGWINCH reach the child) and execs the command.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import shlex
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable

REPLAY_BYTES = 4 * 1024 * 1024

# What the bash of a terminal tab says about itself (shell_init.bash): `cmd` (the command line that
# starts) or `cwd` (the folder, at each prompt), as OSC 7771, which the terminal ignores.
INFO = re.compile(rb"\x1b\]7771;(cmd|cwd);([^\x07\x1b]{0,4096})(?:\x07|\x1b\\)")
INFO_START = b"\x1b]7771;"
INFO_MAX = 1000  # characters kept of a command line

log = logging.getLogger(__name__)


def scan_info(data: bytes) -> tuple[list[tuple[str, str]], bytes]:
    """The (kind, value) pairs in `data`, and its end when it may be the start of a sequence
    that the next read completes (to put before the next read)."""
    found = [(m.group(1).decode(), m.group(2).decode("utf-8", "replace")[:INFO_MAX]) for m in INFO.finditer(data)]
    i = data.rfind(INFO_START)
    if i >= 0 and not INFO.match(data, i) and len(data) - i < 4200:
        return found, data[i:]
    for k in range(len(INFO_START) - 1, 0, -1):  # "\x1b]77" at the very end
        if data.endswith(INFO_START[:k]):
            return found, data[-k:]
    return found, b""
WRAPPER = "import fcntl,os,sys,termios\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\nos.execvp(sys.argv[1], sys.argv[1:])\n"


def set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 2), max(cols, 10), 0, 0))


def spawn(argv: list[str], cwd: str, env: dict[str, str], rows: int, cols: int) -> tuple[subprocess.Popen, int]:
    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        # IUTF8 (Backspace erases a whole UTF-8 character): Python's termios on macOS lacks it, so its value
        attrs[0] |= getattr(termios, "IUTF8", 0x00004000)
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
        self.key, self.gen = key, gen
        self.kind, self.task, self.title = "claude", key, ""  # set by the caller for shells
        self.proc, self.fd = spawn(argv, cwd, env, rows, cols)
        self.started = time.time()
        self.bytes_out = 0
        log.info("pty %s: pid %d started, %s (cwd %s, %dx%d)", key, self.proc.pid, shlex.join(argv), cwd, cols, rows)
        self.on_exit = on_exit
        self.replay = bytearray()
        self.queues: set[asyncio.Queue] = set()
        self.alive = True
        self.last_output = time.time()
        # a terminal tab whose bash runs shell_init.bash: the last command line and the folder
        self.scan = False  # set by the caller before attach()
        self.command, self.folder = "", cwd
        self.tells = False  # set by the caller: its bash runs shell_init.bash, so `prompts` counts
        self.prompts = 0  # the prompts shown so far (a `cwd` comes before each one)
        self.on_info: Callable[[PtySession], None] | None = None
        self._tail = b""

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
        if self.scan:
            self._read_info(data)
        self._emit(data)

    def _read_info(self, data: bytes) -> None:
        found, self._tail = scan_info(self._tail + data)
        changed = False
        for kind, value in found:
            self.prompts += kind == "cwd"
            if kind == "cmd" and value.strip() and value != self.command:
                self.command, changed = value.strip(), True
            elif kind == "cwd" and value and value != self.folder:
                self.folder, changed = value, True
        if changed and self.on_info:
            self.on_info(self)

    def _emit(self, data: bytes) -> None:
        self.bytes_out += len(data)
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
            code = self.proc.wait(timeout=2)
            how = f"exit code {code}" if code >= 0 else f"signal {signal.Signals(-code).name}"
        except subprocess.TimeoutExpired:
            how = "output closed, process still running"
        except ValueError:
            how = f"exit code {self.proc.returncode}"
        log.info("pty %s: pid %d ended (%s) after %.0f s, %d bytes of output", self.key, self.proc.pid, how,
                 time.time() - self.started, self.bytes_out)  # fmt: skip
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

    def clear_replay(self) -> None:
        """Cmd+K in a terminal tab: forget the output, but keep the line of the cursor (the
        prompt), as xterm.js's clear() does. A reloaded view then replays only that line."""
        del self.replay[: self.replay.rfind(b"\n") + 1]

    def resize(self, rows: int, cols: int) -> None:
        if self.alive:
            try:
                set_size(self.fd, rows, cols)
            except OSError:
                pass

    def hangup(self) -> None:
        """SIGHUP to the child's process group, like closing a terminal window."""
        log.info("pty %s: SIGHUP to pid %d", self.key, self.proc.pid)
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
