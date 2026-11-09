import os
import select
import sys
import time

from brain_fry.app.ptys import set_size, spawn

CHILD = r"""
import os, signal, sys, time
signal.signal(signal.SIGWINCH, lambda *a: print("WINCH", os.get_terminal_size(), flush=True))
print("SIZE", os.get_terminal_size(), "CTTY", os.ttyname(0) == os.ttyname(sys.stdout.fileno()),
      "LEADER", os.getsid(0) == os.getpid(), flush=True)
print("READ", input(), flush=True)
time.sleep(5)
"""


def read_until(fd, needle: bytes, timeout=5.0) -> bytes:
    out, end = b"", time.time() + timeout
    while needle not in out and time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                out += os.read(fd, 4096)
            except BlockingIOError:
                pass
    return out


def test_spawn_gives_a_controlling_terminal_size_and_sigwinch():
    proc, fd = spawn([sys.executable, "-c", CHILD], cwd="/", env=dict(os.environ), rows=30, cols=100)
    try:
        out = read_until(fd, b"LEADER")
        assert b"columns=100, lines=30" in out and b"CTTY True" in out and b"LEADER True" in out
        os.write(fd, b"hello\r")
        assert b"READ hello" in read_until(fd, b"READ")
        set_size(fd, 40, 120)
        assert b"WINCH os.terminal_size(columns=120, lines=40)" in read_until(fd, b"WINCH")
    finally:
        proc.kill()
        proc.wait()
        os.close(fd)


def test_ctrl_c_reaches_the_child():
    proc, fd = spawn([sys.executable, "-c", "import time\nprint('ready', flush=True)\ntime.sleep(10)"],
                     cwd="/", env=dict(os.environ), rows=24, cols=80)  # fmt: skip
    try:
        read_until(fd, b"ready")
        os.write(fd, b"\x03")
        assert proc.wait(timeout=5) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(fd)
