import subprocess
import threading
import time

import pytest

from brain_fry import git


def sh(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def commit_all(repo, msg="c"):
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository with one commit, isolated from my git config (signing, hooks, prefixes)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{k}_NAME", "T")
        monkeypatch.setenv(f"{k}_EMAIL", "t@t")
    r = tmp_path / "repo"
    r.mkdir()
    sh(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("".join(f"line {i}\n" for i in range(1, 31)))
    (r / "b.txt").write_text("b\n")
    commit_all(r, "first")
    return r.resolve()


def three_changes(repo):
    lines = [f"line {i}\n" for i in range(1, 31)]
    for i in (1, 14, 27):
        lines[i] = f"CHANGED {i + 1}\n"
    (repo / "a.txt").write_text("".join(lines))


def hunks(repo, side="unstaged", path="a.txt"):
    return git.diff(repo, side, path)["hunks"]


# ------------------------------------------------------------------ status


def test_status_lists_staged_unstaged_untracked_and_renames(repo):
    (repo / "a.txt").write_text("new\n")
    sh(repo, "add", "a.txt")
    (repo / "a.txt").write_text("newer\n")  # partly staged: in both lists
    sh(repo, "mv", "b.txt", "c.txt")
    (repo / "new.txt").write_text("n\n")
    st = git.status(repo)
    assert [(f["path"], f["st"]) for f in st["staged"]] == [("a.txt", "M"), ("c.txt", "R")]
    assert st["staged"][1]["orig"] == "b.txt"
    assert [(f["path"], f["st"]) for f in st["unstaged"]] == [("a.txt", "M"), ("new.txt", "?")]
    assert st["branch"]["head"] == "main"


def test_status_lists_conflicts(repo):
    sh(repo, "checkout", "-qb", "other")
    (repo / "b.txt").write_text("other\n")
    commit_all(repo)
    sh(repo, "checkout", "-q", "main")
    (repo / "b.txt").write_text("main\n")
    commit_all(repo)
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "other"], capture_output=True)
    st = git.status(repo)
    assert [(f["path"], f["st"]) for f in st["conflicts"]] == [("b.txt", "U")]
    assert git.operation(repo) == "merge"


def test_many_untracked_files_are_listed_by_folder(repo, monkeypatch):
    monkeypatch.setattr(git, "MAX_UNTRACKED", 3)
    (repo / "data").mkdir()
    for i in range(5):
        (repo / "data" / f"{i}.csv").write_text("x\n")
    assert [f["path"] for f in git.status(repo)["unstaged"]] == ["data/"]


# ------------------------------------------------------------------ hunks and lines


def test_stage_one_hunk_of_three_then_unstage_it(repo):
    three_changes(repo)
    hs = hunks(repo)
    assert len(hs) == 3
    git.apply_hunk(repo, repo, "stage", "a.txt", hs[1]["digest"])
    staged = sh(repo, "diff", "--cached")
    assert "CHANGED 15" in staged and "CHANGED 2" not in staged and "CHANGED 28" not in staged
    assert len(hunks(repo)) == 2  # the other two stay unstaged
    git.apply_hunk(repo, repo, "unstage", "a.txt", hunks(repo, "staged")[0]["digest"])
    assert sh(repo, "diff", "--cached") == ""
    assert "CHANGED 15" in (repo / "a.txt").read_text()  # the work tree is never touched


def test_stage_and_unstage_single_lines(repo):
    text = (repo / "a.txt").read_text().replace("line 10\n", "line 10\nadd 1\nadd 2\nadd 3\n").replace("line 12\n", "")
    (repo / "a.txt").write_text(text)
    (h,) = hunks(repo)
    add2 = next(i for i, ln in enumerate(h["lines"]) if ln[3] == "add 2")
    git.apply_hunk(repo, repo, "stage", "a.txt", h["digest"], [add2])
    staged = sh(repo, "diff", "--cached")
    assert "+add 2" in staged and "add 1" not in staged and "add 3" not in staged and "-line 12" not in staged
    (h,) = hunks(repo)
    git.apply_hunk(repo, repo, "stage", "a.txt", h["digest"], [_index_of(h, "-")])
    assert "-line 12" in sh(repo, "diff", "--cached")
    (sh_h,) = hunks(repo, "staged")
    git.apply_hunk(repo, repo, "unstage", "a.txt", sh_h["digest"], [_index_of(sh_h, "+")])
    staged = sh(repo, "diff", "--cached")
    assert "add 2" not in staged and "-line 12" in staged
    assert (repo / "a.txt").read_text() == text


def _index_of(h, kind):
    return next(i for i, ln in enumerate(h["lines"]) if ln[0] == kind)


def test_a_hunk_that_changed_is_refused(repo):
    three_changes(repo)
    dig = hunks(repo)[1]["digest"]
    (repo / "a.txt").write_text((repo / "a.txt").read_text().replace("CHANGED 15", "AGENT 15"))
    with pytest.raises(git.Stale):
        git.apply_hunk(repo, repo, "stage", "a.txt", dig)
    assert sh(repo, "diff", "--cached") == ""


def test_a_change_elsewhere_does_not_block_the_hunk(repo):
    three_changes(repo)
    dig = hunks(repo)[2]["digest"]
    (repo / "a.txt").write_text("top\n" + (repo / "a.txt").read_text())  # moves every hunk down
    git.apply_hunk(repo, repo, "stage", "a.txt", dig)
    assert "CHANGED 28" in sh(repo, "diff", "--cached")


def test_crlf_lines_stay_crlf(repo):
    (repo / "w.txt").write_bytes(b"".join(b"r%d\r\n" % i for i in range(20)))
    commit_all(repo)
    (repo / "w.txt").write_bytes((repo / "w.txt").read_bytes().replace(b"r3\r\n", b"R3\r\n").replace(b"r16\r\n", b"R16\r\n"))
    git.apply_hunk(repo, repo, "stage", "w.txt", hunks(repo, path="w.txt")[0]["digest"])
    blob = subprocess.run(["git", "-C", str(repo), "show", ":w.txt"], capture_output=True, check=True).stdout
    assert b"R3\r\n" in blob and b"r16\r\n" in blob and b"\n" not in blob.replace(b"\r\n", b"")


# ------------------------------------------------------------------ discard


def test_discard_a_hunk_and_undo_it(repo):
    three_changes(repo)
    before = (repo / "a.txt").read_text()
    r = git.apply_hunk(repo, repo, "discard", "a.txt", hunks(repo)[0]["digest"])
    after = (repo / "a.txt").read_text()
    assert "CHANGED 2\n" not in after and "CHANGED 15" in after
    assert (git.discarded_dir(repo) / r["undo"]).exists()
    git.undo_discard(repo, repo, r["undo"])
    assert (repo / "a.txt").read_text() == before


def test_discard_lines(repo):
    (repo / "a.txt").write_text((repo / "a.txt").read_text().replace("line 10\n", "line 10\nadd 1\nadd 2\n"))
    (h,) = hunks(repo)
    i = next(i for i, ln in enumerate(h["lines"]) if ln[3] == "add 1")
    git.apply_hunk(repo, repo, "discard", "a.txt", h["digest"], [i])
    text = (repo / "a.txt").read_text()
    assert "add 1" not in text and "add 2" in text


def test_discard_a_file_and_undo_it(repo):
    (repo / "a.txt").write_text("all new\n")
    (repo / "b.txt").unlink()
    r = git.discard_file(repo, repo, "a.txt", untracked=False)
    assert (repo / "a.txt").read_text().startswith("line 1\n")
    git.undo_discard(repo, repo, r["undo"])
    assert (repo / "a.txt").read_text() == "all new\n"
    git.discard_file(repo, repo, "b.txt", untracked=False)  # a deleted file comes back
    assert (repo / "b.txt").read_text() == "b\n"


def test_discard_an_untracked_file_moves_it_to_the_trash(repo, monkeypatch):
    trashed = []
    monkeypatch.setattr("brain_fry.files._move_to_trash", trashed.append)  # a real call fills my Trash
    (repo / "new.txt").write_text("n\n")
    assert git.discard_file(repo, repo, "new.txt", untracked=True) == {"trashed": True}
    assert trashed == [repo / "new.txt"]


# ------------------------------------------------------------------ files, commit, lock


def test_stage_and_unstage_whole_files(repo):
    (repo / "a.txt").write_text("x\n")
    (repo / "b.txt").unlink()
    (repo / "n.txt").write_text("n\n")
    git.stage_files(repo, ["a.txt", "b.txt", "n.txt"])
    assert {f["path"]: f["st"] for f in git.status(repo)["staged"]} == {"a.txt": "M", "b.txt": "D", "n.txt": "A"}
    git.unstage_files(repo, ["a.txt", "b.txt", "n.txt"], has_head=True)
    assert git.status(repo)["staged"] == []


def test_commit_and_a_failing_hook(repo):
    (repo / "a.txt").write_text("x\n")
    git.stage_files(repo, ["a.txt"])
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint: 3 errors'\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(git.GitError, match="lint: 3 errors"):
        git.commit(repo, "msg")
    hook.unlink()
    r = git.commit(repo, "Commit from Review mode\n\nwith a body")
    assert sh(repo, "log", "-1", "--format=%s").strip() == "Commit from Review mode"
    assert r["sha"] == sh(repo, "rev-parse", "--short", "HEAD").strip()
    with pytest.raises(git.GitError, match="empty"):
        git.commit(repo, "  \n")


def test_waits_for_index_lock_then_gives_up(repo, monkeypatch):
    (repo / "a.txt").write_text("x\n")
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    threading.Thread(target=lambda: (time.sleep(0.3), lock.unlink()), daemon=True).start()
    git.stage_files(repo, ["a.txt"])  # retried until the lock went away
    assert git.status(repo)["staged"]
    lock.write_text("")
    monkeypatch.setattr(git, "LOCK_RETRIES", 1)
    with pytest.raises(git.GitError, match="index.lock"):
        git.unstage_files(repo, ["a.txt"], has_head=True)


def test_paths_must_stay_in_the_repository(repo):
    for bad in ("../x", "/etc/passwd", "", "a/../../x"):
        with pytest.raises(ValueError):
            git.diff(repo, "unstaged", bad)


def test_binary_files_are_whole_file_only(repo):
    (repo / "img.bin").write_bytes(bytes(range(256)))
    commit_all(repo)
    (repo / "img.bin").write_bytes(bytes(range(255, -1, -1)))
    d = git.diff(repo, "unstaged", "img.bin")
    assert d["info"]["binary"] and not d["hunkable"]


# ------------------------------------------------------------------ worktrees, log, graph


def test_worktrees_and_the_tree_of_a_task(repo):
    wt = repo / ".claude" / "worktrees" / "feat"
    sh(repo, "worktree", "add", "-q", "-b", "bf/feat", str(wt))
    (wt / "sub").mkdir()
    top, trees, cur = git.pick(wt / "sub")
    assert [t["name"] for t in trees] == ["main", "feat"]
    assert cur["name"] == "feat" and cur["branch"] == "bf/feat"
    assert git.pick(repo)[2]["name"] == "main"
    assert git.pick(repo, str(wt))[2]["name"] == "feat"  # asked for by the front end
    tasks = [{"id": "t1", "cwd": str(repo), "status": "working", "title": "A"},
             {"id": "t2", "cwd": str(wt), "status": "blocked", "title": "B"}]  # fmt: skip
    st = git.state(top, trees, git.pick(repo)[2], tasks)
    assert {t["name"]: [x["id"] for x in t["tasks"]] for t in st["trees"]} == {"main": ["t1"], "feat": ["t2"]}


def test_command_outside_a_repository(tmp_path):
    assert git.command("git.state", str(tmp_path), str(tmp_path), {}) == {"repo": False}


def test_log_rows_labels_and_lanes(repo):
    sh(repo, "checkout", "-qb", "side")
    (repo / "s.txt").write_text("s\n")
    commit_all(repo, "on side")
    sh(repo, "checkout", "-q", "main")
    (repo / "m.txt").write_text("m\n")
    commit_all(repo, "on main")
    sh(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    top, trees, cur = git.pick(repo)
    out = git.log_rows(top, trees, cur)
    rows = out["rows"]
    assert rows[0]["h"] == "wip" and rows[0]["p"] == [cur["head"]]
    assert [r["s"] for r in rows[1:]] == ["merge side", "on main", "on side", "first"]
    merge = rows[1]
    assert {(x["n"], x["k"]) for x in merge["refs"]} == {("main", "local"), ("wt: main", "wt")}
    assert any(x["head"] for x in merge["refs"])
    assert len(merge["p"]) == 2 and len([s for s in merge["segs"] if s[1] == 1]) == 2  # two lines leave the merge
    assert rows[0]["segs"] and all(s[5] for s in rows[0]["segs"])  # dashed from the uncommitted changes
    assert rows[2]["lane"] != rows[3]["lane"]  # the two branches side by side
    assert rows[-1]["lane"] == 0 and not out["more"]


def test_lanes_converge_and_free_their_slot():
    rows = [{"h": "m", "p": ["a", "b"]}, {"h": "b", "p": ["c"]}, {"h": "a", "p": ["c"]}, {"h": "c", "p": []}]
    git.lanes(rows)
    assert [r["lane"] for r in rows] == [0, 1, 0, 0]
    into_c = [s for s in rows[3]["segs"] if s[3] == 1]
    assert {s[0] for s in into_c} == {0, 1}  # both lines end in c
    assert rows[3]["w"] == 2


def test_commit_info_and_its_diff(repo):
    (repo / "a.txt").write_text("changed\n")
    sh(repo, "mv", "b.txt", "c.txt")
    commit_all(repo, "subject\n\nbody line")
    sha = sh(repo, "rev-parse", "HEAD").strip()
    info = git.commit_info(repo, sha)
    assert info["message"] == "subject\n\nbody line"
    assert {(f["st"], f["path"]) for f in info["files"]} == {("M", "a.txt"), ("R", "c.txt")}
    d = git.diff(repo, "commit", "a.txt", sha=sha)
    assert not d["hunkable"] and any(ln[0] == "+" and ln[3] == "changed" for ln in d["hunks"][0]["lines"])
    root = sh(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    assert git.commit_info(repo, root)["files"] == [{"st": "A", "path": "a.txt"}, {"st": "A", "path": "b.txt"}]
