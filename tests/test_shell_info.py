"""The last command and the folder of a terminal tab (shell_init.bash, ptys.scan_info), for the
list of Cmd+N O."""

import os
import select
import time
from pathlib import Path

import pytest

from brain_fry.app import ptys
from brain_fry.app.core import SHELL_INIT


def seq(kind: str, value: str, st: bytes = b"\x07") -> bytes:
    return b"\x1b]7771;" + kind.encode() + b";" + value.encode() + st


def test_scan_finds_sequences_among_output():
    data = b"hello " + seq("cmd", "ls -la") + b"\r\nfile\r\n" + seq("cwd", "/tmp/a b", b"\x1b\\") + b"$ "
    found, rest = ptys.scan_info(data)
    assert found == [("cmd", "ls -la"), ("cwd", "/tmp/a b")]
    assert rest == b""


def test_scan_keeps_a_sequence_cut_between_two_reads():
    whole = b"out" + seq("cmd", "git diff --stat") + b"more"
    for cut in range(1, len(whole)):
        found, rest = ptys.scan_info(whole[:cut])
        found2, rest2 = ptys.scan_info(rest + whole[cut:])
        assert found + found2 == [("cmd", "git diff --stat")], cut
        assert rest2 == b""


def test_scan_ignores_other_escape_sequences():
    data = b"\x1b]0;title\x07\x1b]8;;http://x\x07link\x1b]8;;\x07\x1b[31mred"
    assert ptys.scan_info(data) == ([], b"")


def test_scan_decodes_utf8():
    assert ptys.scan_info(seq("cwd", "/Users/x/Économie"))[0] == [("cwd", "/Users/x/Économie")]


def run_bash(home: Path, lines: list[str], wait_for: bytes, timeout: float = 15.0) -> bytes:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "xterm", "BF_SHELL_LOGIN": "1"}
    proc, fd = ptys.spawn(["/bin/bash", "--init-file", str(SHELL_INIT)], str(home), env, 24, 100)
    out = b""

    def read_until(marker: bytes, start: int) -> None:
        nonlocal out
        end = time.time() + timeout
        while marker not in out[start:] and time.time() < end:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
        assert marker in out[start:], out.decode(errors="replace")

    try:
        read_until(b"PROMPT>", 0)  # the first prompt
        for line in lines:
            start = len(out)
            os.write(fd, line.encode() + b"\n")
            read_until(b"PROMPT>", start)
        start = len(out)
        os.write(fd, b"echo " + wait_for + b"\n")
        read_until(wait_for + b"\r\n", start)
        read_until(b"PROMPT>", start)
    finally:
        proc.kill()
        proc.wait()
        os.close(fd)
    return out


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_bash_tells_its_commands_and_its_folder(tmp_path):
    (tmp_path / "sub").mkdir()
    # a profile with its own PROMPT_COMMAND (kept) and HISTCONTROL (a line with a space is not sent)
    (tmp_path / ".bash_profile").write_text(
        "PS1='PROMPT> '\nHISTCONTROL=ignorespace\nPROMPT_COMMAND='echo PC-RAN'\nexport FROM_PROFILE=yes\n"
    )
    out = run_bash(tmp_path, ["cd sub", "echo hi | cat; true", "", " echo hidden", "echo $FROM_PROFILE"], b"DONE")
    found, _ = ptys.scan_info(out)
    cmds = [v for k, v in found if k == "cmd"]
    assert cmds == ["cd sub", "echo hi | cat; true", "echo $FROM_PROFILE", "echo DONE"]
    assert [v for k, v in found if k == "cwd"][-1] == str(tmp_path / "sub")
    assert b"PC-RAN" in out  # the profile's PROMPT_COMMAND still runs
    assert b"yes\r\n" in out  # the login files were read


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_bash_without_a_profile_reads_profile_or_nothing(tmp_path):
    (tmp_path / ".profile").write_text("PS1='PROMPT> '\n")
    out = run_bash(tmp_path, ["pwd"], b"END")
    found, _ = ptys.scan_info(out)
    assert [v for k, v in found if k == "cmd"] == ["pwd", "echo END"]
    assert ("cwd", str(tmp_path)) in found


def test_a_session_keeps_the_last_command_and_folder():
    s = ptys.PtySession.__new__(ptys.PtySession)  # no process: only the reader of the sequences
    s.scan, s.command, s.folder, s._tail, s.prompts = True, "", "/start", b"", 0
    seen = []
    s.on_info = lambda x: seen.append((x.command, x.folder))
    whole = seq("cmd", "make test") + b"output" + seq("cwd", "/start/sub")
    s._read_info(whole[:9])
    s._read_info(whole[9:])
    assert (s.command, s.folder) == ("make test", "/start/sub")
    s._read_info(seq("cmd", "make test"))  # the same again: no news
    s._read_info(seq("cwd", "/elsewhere"))
    assert seen == [("make test", "/start/sub"), ("make test", "/elsewhere")]
    assert s.prompts == 2  # a `cwd` before each prompt (the play button of a Python file waits for the first)


# ---- the backend: the state carries them, and shell.copy starts a shell where the other one is

@pytest.fixture
def backend(project, tmp_path, monkeypatch):
    from brain_fry.app.core import Core
    from brain_fry.app.server import start_backend

    fake = tmp_path / "fake_claude"
    fake.write_text("#!/bin/bash\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("BRAIN_FRY_CLAUDE", str(fake))
    monkeypatch.setenv("SHELL", "/bin/bash")  # with shell_init.bash; $HOME is the fake one (conftest)
    core = Core()
    port = start_backend(core)
    yield core, port
    core.shutdown()


def _states(ws, until, timeout=10.0):
    import json

    end = time.time() + timeout
    while time.time() < end:
        m = json.loads(ws.recv(timeout=end - time.time()))
        assert m["type"] != "error", m
        if m["type"] == "state" and until(m):
            return m
    raise AssertionError("timeout")


def _request(ctl, req, **msg):
    import json

    ctl.send(json.dumps({"req": req, **msg}))
    end = time.time() + 10
    while time.time() < end:
        m = json.loads(ctl.recv(timeout=end - time.time()))
        if m["type"] == "reply" and m["req"] == req:
            return m
    raise AssertionError("no reply")


def _read(ws, needle, timeout=10.0):
    out, end = b"", time.time() + timeout
    while needle not in out and time.time() < end:
        out += ws.recv(timeout=end - time.time())
    return out


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_copy_a_terminal_starts_a_shell_in_its_folder(backend, project, home, tmp_path):
    import json

    from websockets.sync.client import connect

    from brain_fry import actions, projects

    core, port = backend
    (home / ".bash_profile").write_text("PS1='PROMPT> '\n")
    (project / "sub").mkdir()
    a, b = actions.new_task(project), actions.new_task(project)
    other = tmp_path / "other"
    other.mkdir()
    projects.init(other)
    c = actions.new_task(other)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        key = _request(ctl, 1, cmd="shell.new", id=a.id, rows=10, cols=80)["data"]["key"]
        with connect(f"ws://127.0.0.1:{port}/pty/{key}") as pty:
            _read(pty, b"PROMPT>")
            pty.send(json.dumps({"t": "in", "d": "cd sub && echo moved\r"}))
            st = _states(ctl, lambda m: (m["tasks"][a.id]["shells"] or [{}])[0].get("cwd") == str(project / "sub"))
        assert st["tasks"][a.id]["shells"][0]["cmd"] == "cd sub && echo moved"
        new = _request(ctl, 2, cmd="shell.copy", id=b.id, **{"from": key}, rows=10, cols=80)["data"]["key"]
        with connect(f"ws://127.0.0.1:{port}/pty/{new}") as pty:
            _read(pty, b"PROMPT>")
            pty.send(json.dumps({"t": "in", "d": "pwd -P\r"}))
            where = str((project / "sub").resolve()).encode() + b"\r\n"
            assert where in _read(pty, where)
        st = _states(ctl, lambda m: (m["tasks"][b.id]["shells"] or [{}])[0].get("cmd"))
        assert st["tasks"][b.id]["shells"][0]["cmd"] == "pwd -P"  # its own last command
        assert actions.load(b.id).terminal_tabs == ["bash"]
        # a terminal of another project, or one that has ended: refused
        refused = _request(ctl, 3, cmd="shell.copy", id=c.id, **{"from": key}, rows=10, cols=80)
        assert not refused["ok"] and "another project" in refused["error"]
        assert not _request(ctl, 4, cmd="shell.copy", id=b.id, **{"from": "shell:nope:1"}, rows=10, cols=80)["ok"]


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_run_python_file_in_bash(backend, project, home):
    """bash tells its first prompt: the line is typed after it, and shows once."""
    from websockets.sync.client import connect

    from brain_fry import actions

    core, port = backend
    (home / ".bash_profile").write_text("PS1='PROMPT> '\n")
    (project / "sub").mkdir()
    (project / "sub" / "a.py").write_text("x = 1\n")
    (project / ".venv" / "bin").mkdir(parents=True)
    fake_python = project / ".venv" / "bin" / "python"
    fake_python.write_text('#!/bin/bash\necho "RAN $* in $(pwd -P)"\n')
    fake_python.chmod(0o755)
    t = actions.new_task(project)
    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        key = _request(ctl, 1, cmd="shell.new", id=t.id, rows=10, cols=80, python="sub/a.py")["data"]["key"]
        with connect(f"ws://127.0.0.1:{port}/pty/{key}") as pty:
            done = f"RAN sub/a.py in {project.resolve()}".encode()
            out = _read(pty, done)
    assert done in out
    before = ptys.INFO.sub(b"", out.split(done, 1)[0])  # what the terminal shows
    assert before.count(b".venv/bin/python sub/a.py") == 1  # typed after the prompt, not echoed before it
    assert before.index(b"PROMPT> ") < before.index(b".venv/bin/python sub/a.py")
