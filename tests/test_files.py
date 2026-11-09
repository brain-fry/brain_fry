import os
import subprocess

import pytest

from brain_fry import files


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("x = 1\n")
    (r / "README.md").write_text("hi\n")
    (r / "node_modules").mkdir()
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    return r


def test_list_dir_hides_noise_and_puts_folders_first(repo):
    names = [e["name"] for e in files.list_dir(repo)]
    assert names == ["src", "README.md"]
    assert files.list_dir(repo, "src") == [{"name": "a.py", "path": "src/a.py", "dir": False}]


def test_paths_must_stay_in_the_project(repo):
    with pytest.raises(PermissionError):
        files.read_file(repo, "../outside.txt")
    os.symlink("/etc", repo / "etc_link")
    with pytest.raises(PermissionError):
        files.list_dir(repo, "etc_link")


def test_read_gives_text_version_and_head(repo):
    (repo / "src" / "a.py").write_text("x = 2\n")
    r = files.read_file(repo, "src/a.py")
    assert (r["text"], r["head"]) == ("x = 2\n", "x = 1\n") and r["version"]
    (repo / "new.txt").write_text("new\n")
    assert files.read_file(repo, "new.txt")["head"] is None


def test_binary_and_large_files_are_not_opened(repo):
    (repo / "b.bin").write_bytes(b"\x00\x01\x02")
    assert files.read_file(repo, "b.bin")["binary"]
    (repo / "big.txt").write_bytes(b"a" * (files.MAX_BYTES + 1))
    assert files.read_file(repo, "big.txt")["tooLarge"]


def test_write_detects_a_change_on_disk_and_keeps_the_mode(repo):
    p = repo / "run.sh"
    p.write_text("echo 1\n")
    p.chmod(0o755)
    base = files.read_file(repo, "run.sh")["version"]
    out = files.write_file(repo, "run.sh", "echo 2\n", base)
    assert p.read_text() == "echo 2\n" and p.stat().st_mode & 0o777 == 0o755
    with pytest.raises(files.Conflict):  # stale base: someone wrote in between
        files.write_file(repo, "run.sh", "echo 3\n", base)
    files.write_file(repo, "run.sh", "echo 3\n", out["version"])
    files.write_file(repo, "run.sh", "echo 4\n", "stale", force=True)
    assert p.read_text() == "echo 4\n"


def test_versions(repo):
    v = files.versions(repo, ["README.md", "gone.txt", "../x"])
    assert v["README.md"] and v["gone.txt"] is None and v["../x"] is None
