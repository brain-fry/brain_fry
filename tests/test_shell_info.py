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
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "xterm"}
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
    assert [v for k, v in found if k == "ran"] == ["echo hidden"]  # not in history: its first command (empty line: nothing)
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
    s.scan, s.command, s.folder, s._tail, s.prompts, s.writes, s.fs_gen = True, "", "/start", b"", 0, False, 0
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


# ---- a command line that may change files reloads the explorer at the next prompt

@pytest.mark.parametrize(("line", "writes"), [
    ("ls -la", False), ("cd sub && ls | head -3; pwd", False), ("git status", False), ("git -c x=y log --oneline", False),
    ("grep -rn foo . 2>&1 | less", False), ("cat a 2> /dev/null", False), ("echo err >&2", False), ("a=$(ls)", False),
    ("FOO=1 sudo -E ls", False), ("", False),
    ("rm a", True), ("mv a b", True), ("touch a", True), ("\\rm a", True), ("/bin/rm a", True),
    ("python make_plots.py", True), ("uv run pytest", True), ("make", True), ("git checkout main", True),
    ("git -C sub status", True),  # -C's folder taken for the subcommand: a false positive
    ("echo x > new.txt", True), ("ls >> list.txt", True), ("ls && rm a", True), ("ls | xargs rm", True),
    ("ls; (cd sub && rm a)", True), ("echo `rm a`", True), ("cat a | tee b", True), ("FOO=1 rm a", True),
])
def test_changes_files(line, writes):
    assert ptys.changes_files(line) is writes


def test_a_session_counts_the_lines_that_may_change_files_at_the_prompt():
    s = ptys.PtySession.__new__(ptys.PtySession)
    s.scan, s.command, s.folder, s._tail, s.prompts, s.writes, s.fs_gen = True, "", "/p", b"", 0, False, 0
    seen = []
    s.on_info, s.on_open = lambda x: seen.append(x.fs_gen), None
    s._read_info(seq("cmd", "python run.py"))
    assert s.fs_gen == 0 and s.writes  # before it runs: its files are not there yet
    s._read_info(seq("cwd", "/p"))  # the prompt: it has ended (the same folder: news all the same)
    assert s.fs_gen == 1 and seen[-1] == 1 and not s.writes
    s._read_info(seq("ran", "python run.py") + seq("cwd", "/p"))  # the same line again, not in history
    assert s.fs_gen == 2
    s._read_info(seq("cmd", "ls -la") + seq("cwd", "/p"))
    s._read_info(seq("ran", "ls") + seq("cwd", "/p"))  # `ls -la` again
    s._read_info(seq("cwd", "/p"))  # an empty line
    assert s.fs_gen == 2
    s._read_info(seq("ran", "rm a") + seq("cwd", "/p"))  # a line with a leading space: its first command
    assert s.fs_gen == 3 and s.command == "ls -la"  # the name of the terminal stays


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_bash_tells_a_line_again_that_history_drops(tmp_path):
    # Renaud's ~/.bash_profile has HISTIGNORE="&": the same line twice in a row is recorded once
    # and a PROMPT_COMMAND that shows the status of the last line: __bf_prompt, before it, keeps $?
    (tmp_path / ".bash_profile").write_text("PS1='PROMPT> '\nHISTIGNORE='&'\nPROMPT_COMMAND='echo ST=$?'\n")
    out = run_bash(tmp_path, ["touch a", "touch a", "", "ls -d a", "ls -d a | cat", "false"], b"END")
    found, _ = ptys.scan_info(out)
    assert [(k, v) for k, v in found if k in ("cmd", "ran")] == [("cmd", "touch a"), ("ran", "touch a"), ("cmd", "ls -d a"),
                                                                ("cmd", "ls -d a | cat"), ("cmd", "false"), ("cmd", "echo END")]  # fmt: skip
    assert b"ST=1" in out and b"ST=0" in out


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
            assert st["tasks"][a.id]["shells"][0]["fsgen"] == 0  # cd, echo: the explorer does not reload
            pty.send(json.dumps({"t": "in", "d": "touch made\r"}))  # it may: at the next prompt
            st = _states(ctl, lambda m: m["tasks"][a.id]["shells"][0]["fsgen"] == 1)
            assert (project / "sub" / "made").exists()
        assert st["tasks"][a.id]["shells"][0]["cmd"] == "touch made"
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


# ---- `bf <file>`: the bash of a terminal tab sends the path (OSC 7771 `open`), the app opens it

def test_scan_finds_a_file_to_open():
    assert ptys.scan_info(seq("open", "/p/a b.py:3"))[0] == [("open", "/p/a b.py:3")]


def test_a_session_passes_a_file_to_open_on():
    s = ptys.PtySession.__new__(ptys.PtySession)
    s.scan, s.command, s.folder, s._tail, s.prompts, s.writes, s.fs_gen = True, "", "/start", b"", 0, False, 0
    s.on_info, opened = None, []
    s.on_open = lambda x, path: opened.append(path)
    s._read_info(seq("open", "/p/a.py") + seq("open", "/p/b.py"))
    assert opened == ["/p/a.py", "/p/b.py"]
    assert s.prompts == 0 and s.folder == "/start"


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_bash_bf_sends_absolute_paths_and_leaves_bfs_commands_alone(tmp_path):
    (tmp_path / ".bash_profile").write_text("PS1='PROMPT> '\n")
    (tmp_path / "sub").mkdir()
    lines = ["cd sub", "bf notes.txt /abs/x.py ../up.md:4", "bf r.txt > /dev/null", "bf ./init",
             "bf", "bf init", "bf --debug", "type -t bf"]  # fmt: skip
    out = run_bash(tmp_path, lines, b"DONE")
    found, _ = ptys.scan_info(out)
    sub = tmp_path / "sub"
    assert [v for k, v in found if k == "open"] == [f"{sub}/notes.txt", "/abs/x.py", f"{sub}/../up.md:4", f"{sub}/r.txt",
                                                     f"{sub}/./init"]  # fmt: skip
    assert out.count(b"bf: command not found") == 3  # `bf`, `bf init`, `bf --debug`: the real bf (not on this PATH)
    assert b"function\r\n" in out


def test_bf_commands_are_the_same_in_the_cli_and_in_bash():
    import re

    from brain_fry import cli

    src = Path(cli.__file__).read_text()
    assert set(re.findall(r'add_parser\("([a-z]+)"', src)) == set(cli.COMMANDS)
    case = re.search(r"''\|-\*\|([a-z|]+)\)", SHELL_INIT.read_text()).group(1)
    assert set(case.split("|")) == set(cli.COMMANDS)


def test_bf_file_outside_brain_fry_says_where_it_works(capsys):
    from brain_fry import cli

    assert cli.main(["notes.txt"]) == 2
    assert "terminal tabs of Brain Fry" in capsys.readouterr().err


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_bf_in_a_terminal_tab_tells_the_front_end(backend, project, home, tmp_path):
    import json

    from websockets.sync.client import connect

    from brain_fry import actions

    core, port = backend
    (home / ".bash_profile").write_text("PS1='PROMPT> '\n")
    (project / "sub").mkdir()
    (project / "sub" / "a.py").write_text("x = 1\n")
    t = actions.new_task(project)

    def opened(ctl):
        end = time.time() + 10
        while time.time() < end:
            m = json.loads(ctl.recv(timeout=end - time.time()))
            if m["type"] == "open":
                return m
        raise AssertionError("no open message")

    with connect(f"ws://127.0.0.1:{port}/ctl") as ctl:
        ctl.recv(timeout=5)
        key = _request(ctl, 1, cmd="shell.new", id=t.id, rows=10, cols=80)["data"]["key"]
        with connect(f"ws://127.0.0.1:{port}/pty/{key}") as pty:
            _read(pty, b"PROMPT>")
            pty.send(json.dumps({"t": "in", "d": "cd sub && bf a.py:1\r"}))
            assert opened(ctl) == {"type": "open", "tid": t.id, "path": "sub/a.py", "line": 1, "new": False}
            pty.send(json.dumps({"t": "in", "d": "bf new.md\r"}))
            assert opened(ctl) == {"type": "open", "tid": t.id, "path": "sub/new.md", "line": None, "new": True}
            far = (tmp_path / "far.txt").resolve()  # outside the project: its absolute path
            far.write_text("far\n")
            pty.send(json.dumps({"t": "in", "d": f"bf {far}\r"}))
            assert opened(ctl) == {"type": "open", "tid": t.id, "path": str(far), "line": None, "new": False}
            # several files: the messages keep their order, whatever each check takes
            pty.send(json.dumps({"t": "in", "d": f"bf {far} a.py .. gone/x.md new.md\r"}))
            got = [opened(ctl) for _ in range(5)]
            assert [got[0].get("path"), got[1].get("path"), got[2].get("error"), got[3].get("error"), got[4].get("path")] == [
                str(far), "sub/a.py", "proj is a folder, not a file.", "no such folder: sub/gone", "sub/new.md"]  # fmt: skip
