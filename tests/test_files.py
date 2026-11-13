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


def test_images_and_pdfs_go_to_a_viewer_whatever_their_size(repo):
    (repo / "plot.PNG").write_bytes(b"\x89PNG" + b"\x00" * (files.MAX_BYTES + 1))
    (repo / "paper.pdf").write_bytes(b"%PDF-1.7\n")
    r = files.read_file(repo, "plot.PNG")
    assert (r["kind"], r["size"]) == ("image", files.MAX_BYTES + 5) and r["version"] and "text" not in r
    assert files.read_file(repo, "paper.pdf")["kind"] == "pdf"
    (repo / "logo.svg").write_text("<svg/>")
    assert files.read_file(repo, "logo.svg")["text"] == "<svg/>"  # SVG is text: the editor


def test_viewable_serves_only_images_and_pdfs_inside_the_project(repo):
    (repo / "paper.pdf").write_bytes(b"%PDF-1.7\n")
    assert files.viewable(repo, "paper.pdf") == ((repo / "paper.pdf").resolve(), "application/pdf")
    for rel in ("README.md", "missing.png", "src"):
        with pytest.raises(FileNotFoundError):
            files.viewable(repo, rel)
    with pytest.raises(PermissionError):
        files.viewable(repo, "../x.png")


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


# ---------------------------------------------------------------- the right-click menu of 2a


def test_rename_in_place_refuses_a_taken_name_and_bad_names(repo):
    assert files.rename(repo, "src/a.py", "b.py") == "src/b.py"
    assert (repo / "src" / "b.py").read_text() == "x = 1\n" and not (repo / "src" / "a.py").exists()
    assert files.rename(repo, "src", "lib") == "lib"
    assert (repo / "lib" / "b.py").exists()
    assert files.rename(repo, "lib/b.py", "b.py") == "lib/b.py"  # same name: nothing to do
    (repo / "lib" / "c.py").write_text("")
    with pytest.raises(FileExistsError):
        files.rename(repo, "lib/c.py", "b.py")
    for bad in ("", ".", "..", "x/y.py", "a\0b"):
        with pytest.raises(ValueError):
            files.rename(repo, "lib/c.py", bad)
    assert files.rename(repo, "lib/c.py", "C.py") == "lib/C.py"  # a change of case only (same entry on macOS)
    assert "C.py" in os.listdir(repo / "lib")
    with pytest.raises(PermissionError):
        files.rename(repo, "", "x")  # the project folder itself


def test_new_file_and_folder_never_replace_and_make_the_folders_on_the_way(repo, tmp_path):
    assert files.create(repo, "src", "b.py", is_dir=False) == "src/b.py"
    assert (repo / "src" / "b.py").read_text() == ""
    assert files.create(repo, "", "docs", is_dir=True) == "docs"
    assert (repo / "docs").is_dir()
    assert files.create(repo, "", " lib/x/c.py ", is_dir=False) == "lib/x/c.py"  # as VS Code
    assert (repo / "lib" / "x" / "c.py").is_file()
    assert files.create(repo, "lib", "x/y/", is_dir=True) == "lib/x/y"
    for name in ("src/a.py", "README.md", "src"):
        with pytest.raises(FileExistsError):
            files.create(repo, "", name, is_dir=False)
    assert (repo / "src" / "a.py").read_text() == "x = 1\n"
    with pytest.raises(NotADirectoryError):
        files.create(repo, "", "README.md/x.py", is_dir=False)
    with pytest.raises(NotADirectoryError):
        files.create(repo, "README.md", "x.py", is_dir=False)
    for bad in ("", " ", "/", ".", "..", "a/../b", "a\0b"):
        with pytest.raises(ValueError):
            files.create(repo, "", bad, is_dir=False)
    with pytest.raises(PermissionError):
        files.create(repo, "..", "x.py", is_dir=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / "out")
    with pytest.raises(PermissionError):
        files.create(repo, "", "out/x.py", is_dir=False)  # a link on the way leads out of the project
    assert not (outside / "x.py").exists()


def test_a_symlink_is_renamed_and_trashed_as_a_link_never_its_target(repo, monkeypatch):
    os.symlink("README.md", repo / "link.md")
    assert files.rename(repo, "link.md", "other.md") == "other.md"
    assert os.path.islink(repo / "other.md") and (repo / "README.md").read_text() == "hi\n"
    trashed = []
    monkeypatch.setattr(files, "_move_to_trash", trashed.append)
    files.trash(repo, "other.md")
    assert trashed == [files.resolve(repo, "") / "other.md"]
    with pytest.raises(FileNotFoundError):
        files.trash(repo, "missing.md")
    with pytest.raises(PermissionError):
        files.trash(repo, "../repo")


def test_paste_copies_under_a_free_name_and_moves_without_replacing(repo, tmp_path):
    assert files.paste(repo, ["README.md"], repo, "", cut=False) == [["README.md", "README copy.md"]]
    assert files.paste(repo, ["README.md"], repo, "", cut=False) == [["README.md", "README copy 2.md"]]
    assert files.paste(repo, ["src"], repo, "", cut=False) == [["src", "src copy"]]
    assert (repo / "src copy" / "a.py").read_text() == "x = 1\n"
    assert files.paste(repo, ["README.md"], repo, "src", cut=False) == [["README.md", "src/README.md"]]
    # move
    assert files.paste(repo, ["README copy.md"], repo, "src copy", cut=True) == [["README copy.md", "src copy/README copy.md"]]
    assert not (repo / "README copy.md").exists()
    with pytest.raises(FileExistsError):
        files.paste(repo, ["README.md"], repo, "src", cut=True)  # src/README.md exists
    assert files.paste(repo, ["src/a.py"], repo, "src", cut=True) == []  # already there
    for cut in (False, True):
        with pytest.raises(ValueError):
            files.paste(repo, ["src"], repo, "src", cut=cut)  # into itself
    with pytest.raises(NotADirectoryError):
        files.paste(repo, ["src/a.py"], repo, "README.md", cut=False)
    # to another project
    other = tmp_path / "other"
    other.mkdir()
    assert files.paste(repo, ["src/a.py"], other, "", cut=True) == [["src/a.py", "a.py"]]
    assert (other / "a.py").exists() and not (repo / "src" / "a.py").exists()


def test_path_text_is_the_path_shown_by_the_explorer(repo):
    assert files.path_text(repo, "src/a.py", relative=True) == "src/a.py"
    assert files.path_text(repo, "src/a.py", relative=False) == str(repo / "src" / "a.py")
    with pytest.raises(PermissionError):
        files.path_text(repo, "../x", relative=True)


def test_link_target_resolves_a_terminal_link_against_the_cwd(repo):
    """The targets of the OSC 8 links that Claude Code writes for Markdown links (4a)."""
    (repo / "src" / "my notes.md").write_text("a\nb\n")
    cwd = str(repo / "src")
    assert files.link_target(repo, cwd, "a.py") == {"path": "src/a.py", "line": None}
    assert files.link_target(repo, cwd, "a.py#L2") == {"path": "src/a.py", "line": 2}
    assert files.link_target(repo, cwd, "a.py#L2-5")["line"] == 2
    assert files.link_target(repo, cwd, "a.py#L2-L5")["line"] == 2
    assert files.link_target(repo, cwd, "a.py#intro")["line"] is None
    assert files.link_target(repo, cwd, "../README.md")["path"] == "README.md"
    assert files.link_target(repo, cwd, "my%20notes.md")["path"] == "src/my notes.md"
    assert files.link_target(repo, cwd, str(repo / "README.md"))["path"] == "README.md"
    assert files.link_target(repo, cwd, f"file://{repo}/src/my%20notes.md#L1") == {"path": "src/my notes.md", "line": 1}
    assert files.link_target(repo, cwd, f"file://localhost{repo}/README.md")["path"] == "README.md"
    # a cwd that is gone: the project
    assert files.link_target(repo, str(repo / "gone"), "README.md")["path"] == "README.md"


def test_link_target_refuses_what_3a_cannot_open(repo):
    with pytest.raises(PermissionError):
        files.link_target(repo, str(repo), "../outside.txt")
    with pytest.raises(PermissionError):
        files.link_target(repo, str(repo), "/etc/hosts")
    with pytest.raises(FileNotFoundError):
        files.link_target(repo, str(repo), "missing.py#L3")
    with pytest.raises(ValueError):
        files.link_target(repo, str(repo), "src")
    with pytest.raises(ValueError):
        files.link_target(repo, str(repo), "#L3")


def test_relative_in_takes_the_save_panels_choice_inside_the_project_only(repo):
    assert files.relative_in(repo, repo / "src" / "new.md") == "src/new.md"  # need not exist yet
    for outside in (repo.parent / "elsewhere.md", repo):
        with pytest.raises(PermissionError):
            files.relative_in(repo, outside)
