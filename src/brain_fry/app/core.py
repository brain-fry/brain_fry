"""The backend: task state, my commands, the PTYs, and two background jobs (the reader of
the spools, every 0.5 s; the digest worker, in its own thread). Runs in the asyncio thread.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import queue
import shlex
import threading
import time
from pathlib import Path

from .. import actions, digest, events, files, finder, firefox, git, install, paths, projects, selection, sessions, views
from .. import treesearch, window_settings
from ..logs import short
from ..util import child_env, claude_bin, write_atomic
from . import drops, taskmenu
from .ptys import PREFILL, PtySession

log = logging.getLogger(__name__)
SAVE_DIALOG = 30  # pywebview's FileDialog.SAVE (webview is imported only in the window)
OPEN_DIALOG = 10  # pywebview's FileDialog.OPEN
ui_log = logging.getLogger("brain_fry.ui")

CONSUME_EVERY = 0.5  # s
SWEEP_EVERY = 60  # s: retry failed digests, close idle sessions
DIGEST_RETRY = 120  # s after a failure
DIGEST_NOW = 0  # the priority of a turn in the digest queue; the shutdown's (-1) goes first
IDLE_CLOSE = 4 * 3600  # s without events and output, see DESIGN_DETAILS.md "App"
OUTSIDE_EVERY = 3  # s: which tasks run elsewhere, the end of a block, projects.json
PROJECT_WS = "project:"  # the id of a project's files with no task selected (fs.* and git.* commands, /file/)
SHELL_INIT = Path(__file__).resolve().parent.parent / "shell_init.bash"  # the bash of terminal tabs runs it
# commands that the front end sends all the time (polling, file reads): logged at DEBUG only.
# Browser commands too: placements come at every frame of a drag, and URLs are browsing history.
QUIET = {"fs.list", "fs.read", "fs.versions", "fs.search", "thread.get", "log", "projects", "selection.set", "files.dirty",
         "finder.list", "finder.info", "finder.thumb", "finder.tabs.set",
         "fs.apps", "finder.apps",  # at each Open With submenu
         "grid.set", "window.set",  # at each click on a tab, each drag of a splitter
         "drop.take",  # its names are file names; the command logs how many it found
         "menu.task",  # at each change of the selected task
         "browser.place", "browser.open", "browser.go", "browser.nav", "browser.find", "browser.snapshot",
         "browser.tabs.set", "browser.cookies.info",
         "git.state", "git.log", "git.diff", "git.commit_info"}  # fmt: skip


def owned_file() -> Path:
    """Tasks whose `claude` runs in this app. Left over after a crash: those processes are gone."""
    return paths.bf_home() / "owned.json"


def who(tid: str | None) -> str:
    """A task or a project's tabs, for the log: the start of the id, or "project <name>"."""
    return f"project {Path(tid[len(PROJECT_WS) :]).name}" if tid and tid.startswith(PROJECT_WS) else short(tid)


class ProjectTabs:
    """The owner of a project's tabs (`project:<path>`), where a task would be: what shells,
    terminal tabs and file links need (id, project, cwd, terminal_tabs). DESIGN_DETAILS.md, "Project tabs"."""

    def __init__(self, project: str) -> None:
        self.id = PROJECT_WS + project
        self.project = self.cwd = project
        self.terminal_tabs = actions.project_tabs(project)["terminal_tabs"]


class Core:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ptys: dict[str, PtySession] = {}
        self.gens: dict[str, int] = {}
        self.clients: set = set()
        self.selected: str | None = None
        # (priority, seq, task id); a task id None stops the worker
        self.digest_q: queue.PriorityQueue[tuple[int, int, str | None]] = queue.PriorityQueue()
        self.queued: set[str] = set()  # the task ids in the digest queue
        self._digest_seq = itertools.count()
        self.failed: dict[str, float] = {}
        self.digest_error: str | None = None
        self._broadcast_pending = False
        self.shell_seq = 0
        self.restored: set[str] = set()  # tasks whose terminal tabs were restored since start
        self.restarting: dict[str, tuple[list[str], str]] = {}  # task id -> (argv, cwd) of its next session (`restart`)
        self.shutting_down = False
        self.running: dict[str, dict] | None = None  # session id -> its process, from Claude Code
        self._projects_version: int | None = None
        self._distrust_logged = False
        self._save_failed: set[str] = set()  # tasks that _refresh_outside could not save: logged once
        self.browser = None  # app.browser.BrowserHost: the browser of 3a, in the window only (main.py)
        self.window = None  # pywebview's window (main.py): the save panel of an untitled tab (fs.saveas)
        self.dirty_files: list[str] = []  # the files with unsaved edits in the front end (files.dirty): the question at quit
        # The state holds the open tasks, plus the archived tasks that this run touched (named by a
        # command, changed by an event: the front end may still show them), plus every archived
        # task while the front end asks for them (archive.show: the board with H, or a search).
        self.touched: set[str] = set()
        self.show_archive = False

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(actions.migrate_archive)  # archived files of the top folders --> archive/
        await asyncio.to_thread(actions.migrate_rank)  # before any event: open files older than ranks keep their place
        await asyncio.to_thread(self._reset_leftover_owned)
        selection.clear()  # left over after a crash (the hook also checks that our pid lives)
        threading.Thread(target=self._digest_worker, name="digest", daemon=True).start()
        self.loop.create_task(self._consume_loop())
        self.loop.create_task(self._sweep_loop())
        self.loop.create_task(self._outside_loop())

    def _reset_leftover_owned(self) -> None:
        f = owned_file()
        if not f.exists():
            return
        for tid in json.loads(f.read_text() or "[]"):
            log.info("task %s: its session died with the last run of the app", short(tid))
            try:
                actions.set_live(tid, False)
            except actions.NotFound:
                pass
        f.unlink()

    def _save_owned(self) -> None:
        alive = [k for k, s in self.ptys.items() if s.alive and s.kind == "claude"]
        paths.bf_home().mkdir(parents=True, exist_ok=True)
        write_atomic(owned_file(), json.dumps(alive))

    def working_count(self) -> int:
        n = 0
        for tid, s in list(self.ptys.items()):
            if s.alive and s.kind == "claude":
                try:
                    n += actions.load(tid).status == "working"
                except actions.NotFound:
                    pass
        return n

    def shutdown(self) -> None:
        """Blocking, from the main thread, once (main.py's _Quit): the window may still be there
        (Cmd+Q, a signal). Ends every live session and shell. Terminal tabs stay in the task
        files, to be restored at the next start. No state push after this: the asyncio thread may
        outlive it while Python shuts down, and its executor then refused the snapshot."""
        self.shutting_down = True  # stops the background loops, the digest worker and the state pushes
        self.digest_q.put((-1, 0, None))
        alive = [s for s in self.ptys.values() if s.alive]
        log.info("shutdown: ending %d process(es): %s", len(alive), ", ".join(s.key for s in alive) or "none")
        for s in alive:
            s.hangup()
        for s in alive:
            s.kill()
            if s.kind == "claude":
                try:
                    actions.set_live(s.key, False)
                except actions.NotFound:
                    pass
        owned_file().unlink(missing_ok=True)
        selection.clear()

    # ---------------------------------------------------------------- state

    def snapshot(self) -> dict:
        tasks = actions.all_tasks()
        projs = views.project_order(projects.load()["projects"], tasks, actions.archive_times())
        tasks += self._archived_in_state({t.id for t in tasks})
        shells: dict[str, list[dict]] = {}
        for s in list(self.ptys.values()):
            if s.kind == "shell" and s.alive:  # cmd, cwd: from bash (shell_init.bash), for Cmd+N O
                shells.setdefault(s.task, []).append({"key": s.key, "gen": s.gen, "title": s.title,
                                                      "cmd": s.command, "cwd": s.folder})  # fmt: skip
        running = self.running or {}
        by_id = {t.id: views.task_json(t, gen := self._live_gen(t.id), shells.get(t.id),
                                       running.get(t.id) if gen is None else None) for t in tasks}  # fmt: skip
        inbox = [t.id for t in sorted(filter(views.in_inbox, tasks), key=views.inbox_order)]
        for p in projs:  # the tabs that every task of the project shows
            p["tabs"] = views.project_tabs_json(actions.project_tabs(p["path"]), shells.get(PROJECT_WS + p["path"]))
        return {
            "type": "state",
            "tasks": by_id,
            "inbox": inbox,
            "working": views.working_list(tasks),  # 1a with "All working" on
            "projects": projs,
            "selected": self.selected,
            "hookInstalled": (hook := self._hook_state())[0],
            "hookOutdated": hook[1],
            "digestError": self.digest_error,
        }

    @staticmethod
    def _hook_state() -> tuple[bool, bool]:
        """(installed, outdated): outdated when ~/.brain_fry/hook.sh differs from this version's
        hook.sh, or settings.json lacks one of its events (install.hook_state)."""
        return install.hook_state()

    def _archived_in_state(self, open_ids: set[str]) -> list:
        """Blocking. The archived tasks of the state: all of them while the front end asks
        (archive.show), else the ones that this run touched and the selected one."""
        if self.show_archive:
            return [t for t in actions.archived_tasks() if t.id not in open_ids]
        out = []
        for tid in list(self.touched) + [self.selected]:
            if not tid or tid in open_ids:
                continue
            open_ids.add(tid)  # once
            try:
                out.append(actions.load(tid))
            except actions.NotFound:  # deleted: forget it
                self.touched.discard(tid)
        return out

    def _touch(self, *ids) -> None:
        """Task ids that a command named or an event changed: the front end may show them after
        an archive (the tabs of 2a and column 3, Cmd+Up back to it), so the state keeps them."""
        self.touched.update(i for i in ids if isinstance(i, str) and i and not i.startswith(PROJECT_WS))

    def _live_gen(self, tid: str) -> int | None:
        s = self.ptys.get(tid)
        return s.gen if s and s.alive else None

    def notify(self, msg: dict) -> None:
        """Thread-safe. A message to every front end, outside the state pushes."""
        if self.loop and not self.shutting_down:
            data = json.dumps(msg)
            self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self._send_all(data)))

    async def _send_all(self, data: str) -> None:
        for ws in list(self.clients):
            try:
                await ws.send(data)
            except Exception:
                self.clients.discard(ws)

    def request_broadcast(self) -> None:
        """Thread-safe. Coalesces bursts of changes into one push."""
        if self.loop and not self.shutting_down:
            self.loop.call_soon_threadsafe(self._schedule_broadcast)

    def _schedule_broadcast(self) -> None:
        if not self._broadcast_pending:
            self._broadcast_pending = True
            self.loop.call_later(0.05, lambda: self.loop.create_task(self._broadcast()))

    async def _broadcast(self) -> None:
        self._broadcast_pending = False
        if not self.clients or self.shutting_down:
            return
        msg = json.dumps(await asyncio.to_thread(self.snapshot))
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                self.clients.discard(ws)

    # ---------------------------------------------------------------- background jobs

    async def _consume_loop(self) -> None:
        while not self.shutting_down:
            try:
                if changed := await asyncio.to_thread(self._consume_all):
                    self._touch(*changed)  # a `close` archives its task: the state keeps it
                    self._enqueue_digest(changed)
                    self.request_broadcast()
            except Exception:
                log.exception("consume")
            await asyncio.sleep(CONSUME_EVERY)

    def _consume_all(self) -> list[str]:
        changed: list[str] = []
        prompted: list[dict] = []

        def on_event(tid: str, ev: dict) -> None:
            # my answer, not the result of a background agent that starts the next turn
            if ev.get("hook_event_name") == "UserPromptSubmit" and events.typed_by_me(ev):
                prompted.append({"type": "prompted", "id": tid, "slash": (ev.get("prompt") or "").lstrip().startswith("/")})

        for p in projects.project_paths():
            changed += events.consume(p, on_event)
        for msg in prompted:  # before the state push (coalesced 50 ms later): auto-advance in the front end
            self.notify(msg)
        sel = self.selected
        if sel in changed:  # I am looking at it: a new reply is read at once, stays in the inbox
            try:
                if actions.load(sel).unread:
                    actions.mark_read(sel)
            except actions.NotFound:
                pass
        return changed

    async def _sweep_loop(self) -> None:
        first = True
        while not self.shutting_down:
            try:
                self._enqueue_digest(await asyncio.to_thread(self._sweep, first))
                first = False
            except Exception:
                log.exception("sweep")
            await asyncio.sleep(SWEEP_EVERY)

    def _sweep(self, archive: bool = False) -> list[str]:
        """Blocking. Closes the idle sessions and returns the tasks whose digest to try again. A
        session of the app may run an archived task (archiving does not end it), so the idle close
        goes through the live sessions, not through the open tasks. The digests: the pending threads
        of the open tasks, and the failed ones, which may be archived (the turn of a `close` prompt
        archives its task before its digest); `archive` (the first sweep of a run): the archive too."""
        tasks = actions.all_tasks() + (actions.archived_tasks() if archive else [])
        retry = [t.id for t in tasks if digest.needs_call(t)] + list(self.failed)
        now = time.time()
        for s in [s for s in list(self.ptys.values()) if s.kind == "claude" and s.alive]:
            try:
                t = actions.load(s.key)
            except actions.NotFound:
                continue
            if not t.inbox and now - views.epoch(t.updated) > IDLE_CLOSE and now - s.last_output > IDLE_CLOSE:
                log.info("closing idle session %s", t.id)
                s.hangup()
        return retry

    def _enqueue_digest(self, ids: list[str]) -> None:
        """A task that is already in the queue is not queued again."""
        now = time.time()
        for tid in ids:
            if tid not in self.queued and now - self.failed.get(tid, 0) > DIGEST_RETRY:
                log.debug("digest %s queued%s", short(tid), " (retry)" if tid in self.failed else "")
                self.queued.add(tid)
                self.digest_q.put((DIGEST_NOW, next(self._digest_seq), tid))

    async def _outside_loop(self) -> None:
        while not self.shutting_down:
            try:
                if await asyncio.to_thread(self._refresh_outside):
                    self.request_broadcast()
            except Exception:
                log.exception("Claude Code's process files")
            await asyncio.sleep(OUTSIDE_EVERY)

    def _refresh_outside(self) -> bool:
        """Which sessions run now (Claude Code's files), the tasks whose process started or
        ended outside the app, the blocks whose dialog closed, and projects.json. True if any changed."""
        running = sessions.running()
        if running is not None and not self._trust(running):
            running = None
        tasks = actions.all_tasks()
        changed = self._sync_outside(tasks, running)
        changed |= self._end_blocks(tasks, running)
        try:
            version = paths.projects_file().stat().st_mtime_ns
        except OSError:
            version = None
        if version != self._projects_version and self._projects_version is not None:
            log.info("projects.json changed: %s", ", ".join(str(p) for p in projects.project_paths()))
        changed |= running != self.running or version != self._projects_version
        self.running, self._projects_version = running, version
        return changed

    def _trust(self, running: dict[str, dict]) -> bool:
        """Claude Code's files must list the sessions that this app runs (after a few
        seconds to start). If they do not, their format changed: say nothing from them."""
        now = time.time()
        missing = [s.key for s in list(self.ptys.values())
                   if s.kind == "claude" and s.alive and now - s.started > 15 and s.key not in running]  # fmt: skip
        if missing and not self._distrust_logged:
            log.warning("~/.claude/sessions does not list the sessions of this app (%s): not used", ", ".join(map(short, missing)))
        self._distrust_logged = bool(missing)
        return not missing

    def _sync_outside(self, tasks, running: dict[str, dict] | None) -> bool:
        """`live` of the tasks that have no process in this app follows Claude Code's list of
        running sessions. Hooks do the same, but a session started before `bf install` has no hooks."""
        if running is None:
            return False
        changed = False
        for t in tasks:
            if self._live_gen(t.id) is not None:
                continue
            run = running.get(t.id)
            if t.live != bool(run):
                if t.id not in self._save_failed:
                    log.info("task %s %r: %s", short(t.id), t.display_title[:50],
                             f"runs in {run['where']} (pid {run['pid']})" if run else "no process runs it any more")  # fmt: skip
                try:
                    actions.set_live(t.id, bool(run))
                    changed = True
                    self._save_failed.discard(t.id)
                except actions.NotFound:
                    pass
                except Exception:
                    self._not_saved(t.id)
        return changed

    def _not_saved(self, tid: str) -> None:
        """A task that cannot be saved must not stop the check of the other tasks (before 2026-11-14,
        one such task ended every _refresh_outside). Tried again at each check, logged once per run."""
        if tid not in self._save_failed:
            self._save_failed.add(tid)
            log.exception("task %s: not saved; tried again every %d s, logged this once", short(tid), OUTSIDE_EVERY)

    def _end_blocks(self, tasks, running: dict[str, dict] | None) -> bool:
        """A blocked task whose dialog closed (Claude Code's process file left `waiting`
        after the block began) leaves the inbox, and the front end advances as after an
        answer: the `prompted` message goes out before the state push."""
        if running is None:
            return False
        changed = False
        for t in tasks:
            run = running.get(t.id)
            if t.status != "blocked" or not run or run["status"] not in ("busy", "idle") or not run.get("since"):
                continue
            try:
                task = actions.end_block(t.id, run["status"], run["since"])
                self._save_failed.discard(t.id)
            except actions.NotFound:
                continue
            except Exception:
                self._not_saved(t.id)
                continue
            if task:
                log.info("task %s %r: its dialog closed (Claude Code: %s in %s, pid %s): block over", short(t.id),
                         t.display_title[:50], run["status"], run["where"], run["pid"])  # fmt: skip
                self.notify({"type": "prompted", "id": t.id, "slash": False, "why": "dialog closed"})
                changed = True
        return changed

    def _digest_worker(self) -> None:
        while True:
            _, _, tid = self.digest_q.get()
            if tid is None:
                return
            self.queued.discard(tid)
            try:
                task = actions.load(tid)
                if digest.needs_call(task):
                    held = task.held
                    try:
                        digest.digest_task(task)
                    finally:  # a held reply that reaches the inbox while I look at it: read at once
                        if held and tid == self.selected and actions.load(tid).unread:
                            actions.mark_read(tid)
                    self.failed.pop(tid, None)
                    self.digest_error = None
                    self.request_broadcast()
                else:  # nothing pending (an event without a reply queued it): no more retries (the sweep retries `failed`)
                    self.failed.pop(tid, None)
            except digest.DigestError as e:  # logged by digest_task
                self.failed[tid] = time.time()
                self.digest_error = f"digest failed for {tid[:8]}: {e}"
                self.request_broadcast()
            except actions.NotFound:
                self.failed.pop(tid, None)
            except Exception:
                log.exception("digest %s", tid)

    # ---------------------------------------------------------------- PTYs

    def _start_pty(self, key: str, argv: list[str], cwd: str, rows: int, cols: int,
                   kind: str = "claude", task: str | None = None, title: str = "") -> PtySession:  # fmt: skip
        """kind "claude": a task's session in 4a, key = task id. kind "shell": a terminal
        tab (3b) of `task`, key = "shell:<task>:<n>"."""
        gen = self.gens.get(key, 0) + 1
        self.gens[key] = gen
        env = child_env(TERM="xterm-256color", COLORTERM="truecolor")
        if kind == "claude":  # Claude Code writes its links as OSC 8 (DESIGN_DETAILS.md "Browser (3a)")
            env["FORCE_HYPERLINK"] = "1"
        if not Path(cwd).is_dir():
            cwd = str(Path.home())
        s = PtySession(key, gen, argv, cwd, env, rows, cols, self._on_pty_exit)
        s.kind, s.task, s.title = kind, task or key, title or kind
        if kind == "shell":  # its last command and its folder, for the list of Cmd+N O
            s.scan = True
            s.on_info = lambda _s: self.request_broadcast()
            s.on_open = self._shell_open  # `bf <file>`
        s.attach(self.loop)
        self.ptys[key] = s
        if kind == "claude":
            self._save_owned()
        return s

    def _on_pty_exit(self, s: PtySession) -> None:
        if s.kind == "shell":  # `exit` in a terminal tab closes the tab
            if self.ptys.get(s.key) is s:
                del self.ptys[s.key]
            if not self.shutting_down:
                self.loop.run_in_executor(None, self._save_terminal_tabs, s.task)
            self.request_broadcast()
            return
        if self._retry_without_prefill(s) or self._start_again(s):
            return
        if self.shutting_down:  # shutdown marks it not live and removes owned.json itself
            return

        def mark() -> None:
            try:
                actions.set_live(s.key, False)
            except actions.NotFound:
                pass
            self._save_owned()
            self.request_broadcast()

        self.loop.run_in_executor(None, mark)

    def _retry_without_prefill(self, s: PtySession) -> bool:
        """--prefill is a hidden option of Claude Code: a version that drops it stops at once with
        "unknown option". Then the session starts again without it (an empty input box), once,
        rather than a planned card that can never start."""
        plain = [a for a in s.argv if not a.startswith(PREFILL)]
        if plain == s.argv or self.shutting_down or self.ptys.get(s.key) is not s:
            return False
        if b"unknown option '--prefill" not in bytes(s.replay).lower():
            return False
        log.warning("pty %s: claude does not know --prefill, started again without the title and notes", s.key)
        self._start_pty(s.key, plain, s.cwd, s.rows, s.cols)
        self.request_broadcast()
        return True

    def _start_again(self, s: PtySession) -> bool:
        """Restart (the right-click menu of an inbox row): the old `claude` has exited, the new one
        starts in the same callback, so no state push shows the task without a session (4a would
        flash "No live session"). Its SessionEnd hook still arrives: `live` off, and a turn that was
        running goes to the inbox (actions.end_session), which is true, since it was cut off."""
        how = self.restarting.pop(s.key, None)
        if how is None or self.shutting_down or self.ptys.get(s.key) is not s:
            return False
        argv, cwd = how
        log.info("pty %s: started again (restart)", s.key)
        self._start_pty(s.key, argv, cwd, s.rows, s.cols)
        self.request_broadcast()
        return True

    def _shells_of(self, tid: str) -> list[PtySession]:
        return [s for s in self.ptys.values() if s.kind == "shell" and s.task == tid and s.alive]

    def _save_terminal_tabs(self, tid: str) -> None:
        titles = [s.title for s in self._shells_of(tid)]
        try:
            if tid.startswith(PROJECT_WS):
                actions.set_project_tabs(self._project_of(tid), "terminal_tabs", titles)
            else:
                actions.set_terminal_tabs(tid, titles)
        except actions.NotFound:
            pass

    def _new_shell(self, task, rows: int, cols: int, cwd: str | None = None) -> PtySession:
        """bash runs shell_init.bash, which reads what a login shell reads and then tells the app
        its commands and its folder (ptys.scan_info). Other shells: `-l`, and they tell nothing."""
        self.shell_seq += 1
        shell = os.environ.get("SHELL") or "/bin/zsh"
        argv, tells = [shell, "-l"], False
        if Path(shell).name == "bash" and SHELL_INIT.is_file():
            argv, tells = [shell, "--init-file", str(SHELL_INIT)], True
        s = self._start_pty(f"shell:{task.id}:{self.shell_seq}", argv, cwd or task.cwd, rows, cols,
                            kind="shell", task=task.id, title=Path(shell).name)  # fmt: skip
        s.tells = tells
        return s

    @staticmethod
    def _python_line(project: str, rel: str) -> str:
        """Blocking: the play button of a Python file. The command line that runs the file `rel`
        from the project's root: with the project's `.venv` when it has one (VS Code picks it
        too), else with the `python` of the shell's PATH. A file outside the project (an absolute
        `rel`, files.locate) runs the same way, named by its absolute path."""
        outside = rel.startswith("/")
        p = files.locate(project, rel) if outside else files.entry(project, rel)
        if not p.is_file() or p.suffix != ".py":
            raise ValueError(f"Not a Python file: {rel}")
        venv = files.resolve(project, "") / ".venv" / "bin" / "python"
        return shlex.join([".venv/bin/python" if venv.exists() else "python", rel if outside else rel.strip("/")])

    def _type_when_ready(self, s: PtySession, line: str, start: float | None = None) -> None:
        """Types `line` and Enter into a new shell once it waits for a command, so that the line
        shows after its first prompt, as if I had typed it: when the prompt comes (bash with
        shell_init.bash tells it), else when its output has been quiet for 0.3 s; after 5 s
        anyway. Typed earlier, the terminal would echo the line once before the prompt."""
        now = time.time()
        start = start or now
        if not s.alive:
            return
        if s.prompts if s.tells else (s.bytes_out and now - s.last_output > 0.3):
            s.write(line.encode() + b"\r")
        elif now - start > 5:
            log.info("shell %s: no prompt after 5 s, typed anyway", s.key)
            s.write(line.encode() + b"\r")
        else:
            self.loop.call_later(0.05, self._type_when_ready, s, line, start)

    @staticmethod
    def _shell_folder(project: str, rel: str) -> str:
        """Blocking: "Open a terminal here" in 2a. The folder `rel` of the project, as the tree
        shows it (a symlink is not followed), or the folder of the file `rel`; "" is the project."""
        p = files.entry(project, rel) if rel.strip("/") else files.resolve(project, "")
        return str(p if p.is_dir() else p.parent)

    @staticmethod
    def _finder_folder(path: str) -> str:
        """Blocking: "Open a terminal here" in a Finder tab: the folder, or the folder of the file,
        anywhere on the disk."""
        p = finder.absolute(path)
        folder = p if p.is_dir() else p.parent
        if not folder.is_dir():
            raise NotADirectoryError(f"no such folder: {folder}")
        return str(folder)

    def _restore_shells(self, task, msg: dict, cols: int) -> None:
        """The terminal tabs of the task's last run, as fresh shells, once per run of the app:
        at its first select, or when its tabs show while nothing is selected (`idle`)."""
        if task.id in self.restored:
            return
        self.restored.add(task.id)
        for _ in range(len(task.terminal_tabs) - len(self._shells_of(task.id))):
            self._new_shell(task, int(msg.get("shellRows") or 12), int(msg.get("shellCols") or cols))

    async def _restore_view_shells(self, owner, msg: dict, cols: int) -> None:
        """The shells of a task and those of its project's tabs, or those of a project's tabs
        alone (the project view), as `_restore_shells`."""
        self._restore_shells(owner, msg, cols)
        if isinstance(owner, ProjectTabs) or PROJECT_WS + owner.project in self.restored:
            return
        try:
            proj = await asyncio.to_thread(self._owner, PROJECT_WS + owner.project)
        except actions.NotFound:  # a project no longer registered
            return
        self._restore_shells(proj, msg, cols)

    def _set_tabs(self, tid: str, key: str, values: list) -> None:
        """Blocking: one list of tabs (`key`: open_files, browser_tabs, finder_tabs) of a task, or
        of a project's tabs (`project:<path>`, kept in project.json)."""
        if tid.startswith(PROJECT_WS):
            actions.set_project_tabs(self._project_of(tid), key, values)
        else:
            {"open_files": actions.set_open_files, "browser_tabs": actions.set_browser_tabs,
             "finder_tabs": actions.set_finder_tabs}[key](tid, values)  # fmt: skip

    def _owner(self, tid: str):
        """Blocking: the task, or the ProjectTabs of `project:<path>` (a registered project only)."""
        if tid.startswith(PROJECT_WS):
            return ProjectTabs(self._project_of(tid))
        return actions.load(tid)

    def _start_copied_shells(self, task, msg: dict, cols: int) -> None:
        """A new or forked task copied the terminal tabs of another task: fresh shells, now."""
        self.restored.add(task.id)
        for _ in task.terminal_tabs:
            self._new_shell(task, int(msg.get("shellRows") or 12), int(msg.get("shellCols") or cols))

    def _project_of(self, tid: str) -> str:
        """The project folder of a task, or of `project:<path>`: 2a and the file tabs of a project
        picked while it has no open task (the front end's `wsId`). Only a registered project."""
        if tid.startswith(PROJECT_WS):
            path = tid[len(PROJECT_WS) :]
            if path not in {p["path"] for p in projects.load()["projects"]}:
                raise actions.NotFound(f"no project {path}")
            return path
        return actions.load(tid).project

    def _shell_open(self, s: PtySession, target: str) -> None:
        """`bf <file>` in a terminal tab (shell_init.bash): the front end opens the file in the view
        that shows this terminal (`onShellOpen`, files.js), or toasts why not. On the loop; the
        checks of the disk run in a thread. The path is not logged: it is part of a command."""

        def check() -> dict:
            try:
                return files.bf_target(self._project_of(s.task), target)
            except (OSError, ValueError, actions.NotFound) as e:  # outside, a folder, no such folder
                return {"error": str(e)}

        prev = s.opening

        async def go() -> None:
            r = await asyncio.to_thread(check)
            if prev:  # `bf a b`: the messages go out in that order
                await asyncio.wait([prev])
            log.info("bf <file> in terminal %s: %s", s.key, "refused" if "error" in r else "new file" if r["new"] else "opened")
            self.notify({"type": "open", "tid": s.task, **r})

        if self.loop and not self.shutting_down:
            s.opening = self.loop.create_task(go())

    def _open_panel(self, folder: str) -> list[str]:
        """Blocking: macOS's open panel, modal, starting in `folder`, several files allowed.
        The chosen paths; [] if cancelled."""
        r = self.window.create_file_dialog(OPEN_DIALOG, directory=folder, allow_multiple=True)
        return [str(x) for x in r] if r else []

    def _app_panel(self) -> str | None:
        """Blocking: macOS's open panel in /Applications, apps only (an app is a file there). The app, or None."""
        r = self.window.create_file_dialog(OPEN_DIALOG, directory="/Applications", file_types=("Applications (*.app)",))
        return str(r[0]) if r else None

    @staticmethod
    def _open_how(p: Path, msg: dict) -> str:
        """fs.open and finder.open: with the default app; with `app` (Open With, an .app bundle);
        `reveal` (Open in Finder for a file: its folder, the file selected). What was done, for the log."""
        if msg.get("app"):
            files.open_with(p, str(msg["app"]))
            return f"opened with {Path(str(msg['app'])).stem}"
        if msg.get("reveal"):
            files.reveal_in_finder(p)
            return "revealed in Finder"
        files.open_external(p)
        return "opened with the default app"

    def _save_panel(self, folder: str, name: str) -> str | None:
        """Blocking: macOS's save panel, modal, starting in `folder`. It asks itself before it
        replaces a file. The chosen path, or None."""
        r = self.window.create_file_dialog(SAVE_DIALOG, directory=folder, save_filename=name)
        if isinstance(r, (list, tuple)):
            r = r[0] if r else None
        return str(r) if r else None

    def viewable(self, tid: str, rel: str) -> tuple[Path, str]:
        """An image or a PDF of the task's project, for `/file/...` (server.py)."""
        return files.viewable(self._project_of(tid), rel)

    @staticmethod
    def _prefill(task) -> str:
        """A planned card (DESIGN_DETAILS.md "Modes"): what its session puts in Claude Code's input
        box, not sent. My title, a blank line, my notes, for a task that has no thread and no parent
        (a fork starts from its parent's conversation). A task of Dev mode has neither title nor notes
        before its first turn, so it gets nothing (unless I renamed it: then it is my title too)."""
        if task.threads or task.parent:
            return ""
        return "\n\n".join(p for p in (task.title.strip(), task.notes.strip()) if p).replace("\x00", "")

    @staticmethod
    def _resume_argv(task) -> list[str]:
        if task.threads:
            return [claude_bin(), "--resume", task.id]
        if task.parent:  # forked, never prompted: nothing to resume yet, fork again
            return [claude_bin(), "--resume", task.parent, "--fork-session", "--session-id", task.id]
        # started here, never prompted. --prefill: a hidden option of Claude Code (2.1.325), the
        # `=` form so that a text that starts with "-" stays its value
        text = Core._prefill(task)
        return [claude_bin(), "--session-id", task.id] + ([PREFILL + text] if text else [])

    # ---------------------------------------------------------------- commands

    async def command(self, msg: dict):
        """Runs one command from the front end. The return value is the reply's data."""
        cmd, tid = msg.get("cmd"), msg.get("id")
        rows, cols = int(msg.get("rows") or 40), int(msg.get("cols") or 120)
        run = asyncio.to_thread
        (log.debug if cmd in QUIET else log.info)("command %s", _describe(msg))
        self._touch(tid, msg.get("from"))  # an archived task that a command names stays in the state

        if cmd == "log":  # from the front end: its errors
            level = logging.ERROR if msg.get("level") == "error" else logging.INFO
            ui_log.log(level, "%s", str(msg.get("msg"))[:4000])
            return None
        if cmd == "projects":  # the picker asks each time it opens: projects.json may have changed
            return views.project_order((await run(projects.load))["projects"], await run(actions.all_tasks),
                                       await run(actions.archive_times))  # fmt: skip
        if cmd == "menu.task":  # what the Task menu of the menu bar shows (taskmenu.py)
            taskmenu.update(msg)
            return None
        if cmd == "archive.show":  # the board can show archived cards (H, or a search): all of them in the state, or not
            self.show_archive = bool(msg.get("on"))
            self._touch(*(msg.get("ids") or []))  # the ones that the front end holds (selected, idle, ...)
            self.request_broadcast()
            return None
        if cmd == "project.init":  # File > Add New Project... (main.py's folder dialog): bf init
            path = Path(str(msg.get("path"))).expanduser().resolve()
            if not path.is_dir():
                raise NotADirectoryError(f"not a folder: {path}")
            new = await run(projects.init, path)
            log.info("init %s: %s", path, "registered" if new else "already registered")
            self.request_broadcast()
            return {"path": str(path), "name": path.name, "new": new}
        if cmd == "drop.take":  # files and folders dropped from Finder: their paths, by the names that the page got (drops.py)
            names = [str(n) for n in msg.get("names") or []]
            found = await run(drops.take, names)
            log.info("drop: %d dropped, %d with a path (%d folders)", len(names), len(found), sum(f["dir"] for f in found))
            return found
        if cmd == "project.deactivate":  # the x of a row of Cmd+P; bf init in the folder undoes it
            await run(projects.deactivate, str(msg.get("path")))
            self.request_broadcast()
            return None

        # ---- files (2a, 3a): replies only, no state change
        if cmd == "fs.list":
            return await run(files.list_dir, await run(self._project_of, tid), msg.get("path") or "")
        if cmd == "fs.read":
            return await run(files.read_file, await run(self._project_of, tid), msg["path"])
        if cmd == "fs.write":
            project = await run(self._project_of, tid)
            try:
                r = await run(files.write_file, project, msg["path"], msg["text"], msg.get("base"), bool(msg.get("force")))
                log.info("saved %s in %s (%d chars%s)", msg["path"], Path(project).name, len(msg["text"]),
                         ", over a newer version on disk" if msg.get("force") else "")  # fmt: skip
                return r
            except files.Conflict:
                log.info("save of %s refused: it changed on disk since it was opened", msg["path"])
                return {"conflict": True}
        if cmd == "fs.open":  # with the default app of macOS: a file that 3a does not show; `app`, `reveal`: _open_how
            p = await run(files.locate, await run(self._project_of, tid), msg["path"])  # path "": the project folder
            log.info("%s %s", await run(self._open_how, p, msg), msg["path"] or "the project folder")
            return None
        if cmd == "fs.apps":  # Open With: the apps that macOS offers (finder.apps_for)
            return await run(finder.apps_for, await run(files.locate, await run(self._project_of, tid), msg["path"]))
        if cmd == "apps.pick":  # Open With > Other...: macOS's open panel in /Applications; None if cancelled
            if self.window is None:
                raise ValueError("Other... works only in the Brain Fry window, not with --browser.")
            return await run(self._app_panel)
        if cmd == "fs.openpanel":  # File > Open...: macOS's open panel; None if cancelled
            if self.window is None:
                raise ValueError("File > Open works only in the Brain Fry window, not with --browser.")
            project = await run(self._project_of, tid)
            chosen = await run(self._open_panel, project)
            if not chosen:
                return None
            paths = [await run(files.tab_path, project, c) for c in chosen]  # outside the project: absolute
            log.info("File > Open: %d file(s), %d outside %s", len(paths), sum(x.startswith("/") for x in paths), Path(project).name)
            return {"paths": paths}
        if cmd == "fs.saveas":  # an untitled tab (Cmd+N F): the save panel of macOS; None if cancelled
            if self.window is None:
                raise ValueError("Saving a new file works only in the Brain Fry window, not with --browser.")
            project = await run(self._project_of, tid)
            chosen = await run(self._save_panel, project, str(msg.get("name") or "Untitled"))
            if not chosen:
                return None
            rel = await run(files.tab_path, project, chosen)  # outside the project: absolute
            log.info("new file %s in %s", rel, Path(project).name)
            return {"path": rel}
        if cmd == "fs.versions":
            return await run(files.versions, await run(self._project_of, tid), list(msg.get("paths") or []))
        if cmd == "fs.search":  # the explorer's search (treesearch.py): the query is `text`, never logged
            return await run(treesearch.command, self._project_of, tid, msg)
        if cmd == "fs.link":  # Cmd+click on a file link in a terminal: the target as it was written
            task = await run(self._owner, tid)  # a project's tabs: its folder is the cwd
            return await run(files.link_target, task.project, task.cwd, str(msg.get("target") or ""))
        if cmd == "fs.paths":  # the plain paths of a terminal line (never logged: text of a reply)
            task = await run(self._owner, tid)
            s = self.ptys.get(str(msg.get("shell") or ""))  # a terminal tab: its folder, after any cd
            base = s.folder if s and s.kind == "shell" else task.cwd
            return await run(files.text_paths, task.project, base, [str(x) for x in msg.get("targets") or []])
        # the right-click menu of 2a: they change files, so they are logged
        if cmd == "fs.rename":
            project = await run(self._project_of, tid)
            new = await run(files.rename, project, msg["path"], str(msg.get("name") or ""))
            log.info("renamed %s to %s in %s", msg["path"], new, Path(project).name)
            return new
        if cmd == "fs.new":  # New File... / New Folder...: `dir` is the folder, "" the project folder
            project = await run(self._project_of, tid)
            is_dir = bool(msg.get("folder"))
            new = await run(files.create, project, msg.get("dir") or "", str(msg.get("name") or ""), is_dir)
            log.info("new %s %s in %s", "folder" if is_dir else "file", new, Path(project).name)
            return new
        if cmd == "fs.trash":
            project = await run(self._project_of, tid)
            await run(files.trash, project, msg["path"])
            log.info("moved %s of %s to the Trash", msg["path"], Path(project).name)
            return None
        if cmd == "fs.paste":  # `from`: the task where I chose Cut or Copy (maybe another project)
            src, dst = await run(self._project_of, msg.get("from") or tid), await run(self._project_of, tid)
            cut = bool(msg.get("cut"))
            pairs = await run(files.paste, src, list(msg.get("paths") or []), dst, msg.get("dir") or "", cut)
            log.info("%s %s from %s into %s", "moved" if cut else "copied", ", ".join(f"{a} -> {b}" for a, b in pairs) or "nothing",
                     Path(src).name, Path(dst).name)  # fmt: skip
            return pairs
        if cmd == "fs.zip":  # Zip: "<name>.zip" next to the entry; concurrent (server.CONCURRENT), a big folder takes time
            project = await run(self._project_of, tid)
            new = await run(files.zip_entry, project, msg["path"])
            size = await run(lambda: (Path(project) / new).stat().st_size)
            log.info("zipped %s to %s in %s (%d bytes)", msg["path"], new, Path(project).name, size)
            return new
        if cmd == "fs.copypath":
            text = await run(files.path_text, await run(self._project_of, tid), msg["path"], bool(msg.get("relative")))
            await run(files.copy_to_clipboard, text)
            return text
        # ---- Finder tabs of column 3 (finder.py): absolute paths, anywhere on the disk
        if cmd == "finder.list":
            return await run(finder.list_folder, str(msg.get("path") or ""))
        if cmd == "finder.info":  # the preview column; `thumb` is slower and comes on its own
            return await run(finder.info, str(msg.get("path") or ""))
        if cmd == "finder.thumb":
            return await run(finder.thumbnail, str(msg.get("path") or ""))
        if cmd == "finder.open":  # with the default app of macOS (a folder: in Finder); `app`, `reveal`: as fs.open
            p = await run(finder.absolute, str(msg.get("path") or ""))
            log.info("%s %s (Finder tab)", await run(self._open_how, p, msg), p.name)
            return None
        if cmd == "finder.apps":
            return await run(finder.apps_for, await run(finder.absolute, str(msg.get("path") or "")))
        if cmd == "finder.rename":
            new = await run(finder.rename, str(msg.get("path") or ""), str(msg.get("name") or ""))
            log.info("renamed %s to %s (Finder tab)", msg.get("path"), Path(new).name)
            return new
        if cmd == "finder.new":  # New File / New Folder: in `dir`, under a free name that the tab then renames
            is_dir = bool(msg.get("folder"))
            new = await run(finder.create, str(msg.get("dir") or ""), is_dir)
            log.info("new %s %s (Finder tab)", "folder" if is_dir else "file", new)
            return new
        if cmd == "finder.trash":
            await run(finder.trash, str(msg.get("path") or ""))
            log.info("moved %s to the Trash (Finder tab)", msg.get("path"))
            return None
        if cmd == "finder.paste":
            cut = bool(msg.get("cut"))
            pairs = await run(finder.paste, list(msg.get("paths") or []), str(msg.get("dir") or ""), cut)
            log.info("%s %s (Finder tab)", "moved" if cut else "copied", ", ".join(f"{a} -> {b}" for a, b in pairs) or "nothing")
            return pairs
        if cmd == "finder.copypath":
            project = await run(self._project_of, tid) if tid else None
            return await run(finder.copy_path, str(msg.get("path") or ""), project, bool(msg.get("relative")))
        if cmd == "finder.tabs.set":  # the Finder tabs of a task: [{root, path}], restored when it is selected
            await run(self._set_tabs, tid, "finder_tabs", list(msg.get("tabs") or []))
            return None
        if cmd == "thread.get":  # one thread in full, for its tab in 3a
            task = await run(actions.load, tid)
            for th in task.threads:
                if th.n == int(msg.get("n", -1)):
                    return {"n": th.n, "ts": th.ts, "kind": th.kind, "digest": th.digest,
                            "question": th.question, "prompt": th.prompt, "reply": th.reply}  # fmt: skip
            raise ValueError(f"no thread {msg.get('n')} in task {tid}")
        if cmd == "files.set":  # the open file tabs of a task, restored when it is selected
            await run(self._set_tabs, tid, "open_files", list(msg.get("paths") or []))
            return None
        if cmd == "files.dirty":  # the files with unsaved edits in every task, each time the list changes: the question at quit (main.py)
            self.dirty_files = [str(f) for f in msg.get("files") or []]
            return None
        if cmd == "grid.set":  # where each tab of a task (or of a project's tabs) sits in column 3 (grid.js)
            if tid.startswith(PROJECT_WS):
                await run(actions.set_project_grid, await run(self._project_of, tid), msg.get("grid"))
            else:
                await run(actions.set_grid, tid, msg.get("grid"))
            return None  # no state push: the front end has it, and reads it only at a restore
        if cmd == "window.set":  # the window's settings (store in app.js): one key, None removes it
            if not isinstance(msg.get("key"), str) or not msg["key"]:
                raise ValueError(f"window.set: no key in {_describe(msg)}")
            await run(window_settings.update, {msg["key"]: msg.get("value")})
            return None
        if cmd == "selection.set":  # the selection in 3a, for the hook: no id or no path clears it
            if tid and msg.get("path"):
                task = await run(actions.load, tid)
                await run(selection.write, task, str(msg["path"]), int(msg["from"]), int(msg["to"]),
                          str(msg.get("text") or ""), int(msg.get("total") or 0), bool(msg.get("dirty")))  # fmt: skip
            else:
                await run(selection.clear)
            return None

        # ---- browser (3a): the URLs of a task's tabs here; the pages in the native host
        if cmd == "browser.tabs.set":
            await run(self._set_tabs, tid, "browser_tabs", list(msg.get("urls") or []))
            return None
        if cmd == "browser.cookies.info":  # the burger menu: when my Firefox cookies were last copied
            return {"last": await run(firefox.last_import)}
        if cmd.startswith("browser."):
            if self.browser is None:
                raise ValueError("The browser works only in the Brain Fry window, not with --browser.")
            fut = self.browser.call(msg)  # runs on the main (Cocoa) thread
            if msg.get("req") is None:
                return None  # placements, navigation: nobody waits, errors go to the log
            return await asyncio.wrap_future(fut)

        # ---- Review mode (git.py): the repository of the task's folder, or with no task selected
        # of the project in the dropdown (`project:<path>`); replies only
        if cmd.startswith("git."):
            if tid.startswith(PROJECT_WS):
                project = cwd = await run(self._project_of, tid)
            else:
                task = await run(actions.load, tid)
                project, cwd = task.project, task.cwd or task.project
            tasks = None
            if cmd == "git.state":  # the tasks of panel 1, under the worktree that holds their folder
                tasks = [{"id": t.id, "cwd": t.cwd, "status": t.status, "title": t.display_title}
                         for t in await run(actions.all_tasks) if t.project == project and t.status != "done"]  # fmt: skip
            return await run(git.command, cmd, cwd, project, msg, tasks)

        if cmd == "select":
            self.selected = tid
            if tid:
                task = await run(actions.mark_opened, tid)
                if self._live_gen(tid) is None and not task.live:
                    self._start_pty(tid, self._resume_argv(task), task.cwd, rows, cols)
                await self._restore_view_shells(task, msg, cols)
        elif cmd == "idle":  # nothing needs me: 4a shows a photo, column 3 the tabs of the task that I opened last
            # no session, not selected, not `opened`; `project:<path>`: the project view (no task)
            await self._restore_view_shells(await run(self._owner, tid), msg, cols)
        elif cmd == "shell.new":  # path: "Open a terminal here" in 2a; folder: in a Finder tab; else the task's cwd
            task = await run(self._owner, tid)  # or a project's tabs: its shells start in its folder
            cwd = None if msg.get("path") is None else await run(self._shell_folder, task.project, str(msg["path"]))
            if msg.get("folder") is not None:
                cwd = await run(self._finder_folder, str(msg["folder"]))
            line = None
            if msg.get("python") is not None:  # the play button of a Python file: run it from the project's root
                line = await run(self._python_line, task.project, str(msg["python"]))
                cwd = await run(self._shell_folder, task.project, "")
            self.restored.add(tid)
            s = self._new_shell(task, rows, cols, cwd)
            if line:
                log.info("shell %s: runs %s", s.key, line)
                self._type_when_ready(s, line)
            await run(self._save_terminal_tabs, tid)
            self.request_broadcast()
            return {"key": s.key}
        elif cmd == "shell.copy":  # Cmd+N O: a new shell in the folder where the shell `from` is now
            task = await run(self._owner, tid)
            src = self.ptys.get(str(msg.get("from")))
            if not src or src.kind != "shell" or not src.alive:
                raise ValueError("That terminal has ended.")
            if await run(self._project_of, src.task) != task.project:
                raise ValueError("That terminal belongs to another project.")
            self.restored.add(tid)
            s = self._new_shell(task, rows, cols, src.folder)
            await run(self._save_terminal_tabs, tid)
            self.request_broadcast()
            return {"key": s.key}
        elif cmd == "shell.move":  # a double-click on its tab: from a task to its project's tabs, or back
            s = self.ptys.get(str(msg.get("key")))
            to = str(msg.get("to") or "")
            if not s or s.kind != "shell" or not s.alive:
                raise ValueError("That terminal has ended.")
            if await run(self._project_of, to) != await run(self._project_of, s.task):
                raise ValueError("A terminal stays in its project.")
            old, s.task = s.task, to
            log.info("shell %s: moved from %s to %s", s.key, who(old), who(to))
            await run(self._save_terminal_tabs, old)
            await run(self._save_terminal_tabs, to)
        elif cmd == "shell.close":
            s = self.ptys.get(str(msg.get("key")))
            if s and s.kind == "shell":
                s.hangup()  # the exit removes the tab and saves the list
        elif cmd == "resume":  # also for a session that seems to run elsewhere
            task = await run(actions.load, tid)
            if self._live_gen(tid) is None:
                self._start_pty(tid, self._resume_argv(task), task.cwd, rows, cols)
        elif cmd == "restart":  # the right-click menu of an inbox row: a new `claude` on the same conversation
            s = self.ptys.get(tid)
            if not s or s.kind != "claude" or not s.alive:
                raise ValueError("No Claude Code session of this app runs for this task.")
            task = await run(actions.load, tid)
            self.restarting[tid] = (self._resume_argv(task), task.cwd)  # _start_again, at its exit
            # SIGHUP, then SIGTERM and SIGKILL 3 s apart if it stays: a hung session must restart
            # too. In a thread, so that the commands of this connection do not wait for the exit.
            self.loop.run_in_executor(None, s.kill, 9.0)
        elif cmd == "new":
            # from: the previous task, whose tabs the new one starts with (actions.new_task)
            task = await run(actions.new_task, Path(msg["project"]), msg.get("from"))
            self._start_pty(task.id, [claude_bin(), "--session-id", task.id], task.cwd, rows, cols)
            self._start_copied_shells(task, msg, cols)
            self.selected = task.id
        elif cmd == "plan":  # Organize mode: a card on the board, no session, the selection stays
            task = await run(actions.plan_task, Path(msg["project"]), str(msg.get("title", "")),
                             str(msg.get("prio") or "working"), str(msg.get("notes") or ""))  # fmt: skip
            self.request_broadcast()
            return {"id": task.id}
        elif cmd == "fork":
            task = await run(actions.fork_task, tid)
            argv = [claude_bin(), "--resume", tid, "--fork-session", "--session-id", task.id]
            self._start_pty(task.id, argv, task.cwd, rows, cols)
            self._start_copied_shells(task, msg, cols)
            self.selected = task.id
        elif cmd == "archive":
            await run(actions.archive, tid)
        elif cmd == "delete":  # the right-click menu of a card or an inbox row: its processes end, its file goes to the Trash
            self.restarting.pop(tid, None)  # a restart on its way must not start the deleted task again
            for s in [self.ptys.get(tid), *self._shells_of(tid)]:
                if s and s.alive:
                    s.hangup()  # the exit finds no task to mark: actions.NotFound is ignored there
            await run(actions.delete, tid)
            self.touched.discard(tid)
            if self.selected == tid:
                self.selected = None
        elif cmd == "pin":  # or unpin a pinned task
            await run(actions.pin, tid)
        elif cmd == "rename":
            await run(actions.rename, tid, str(msg.get("title", "")))
        elif cmd == "prio":  # rank: the Undo of park puts each task back at its place
            rank = msg.get("rank")
            await run(actions.set_prio, tid, str(msg.get("prio")), float(rank) if isinstance(rank, (int, float)) else None)
        elif cmd == "order":  # Shift+Up/Down on the board: just above (below) the card `over`
            await run(actions.place, tid, str(msg.get("over")), bool(msg.get("after")))
        elif cmd == "park":  # Organize mode: the working tasks of the other projects --> next
            ids = await run(actions.park_others, Path(msg["project"]))
            self.request_broadcast()
            return {"ids": ids}
        else:
            raise ValueError(f"unknown command {cmd!r}")
        self.request_broadcast()
        return None


def _describe(msg: dict) -> str:
    """A command for the log: short ids, the length of a text instead of the text."""
    out = []
    for k, v in msg.items():
        if k in ("cmd", "req", "rows", "cols", "shellRows", "shellCols"):
            continue
        if k in ("text", "notes", "message"):  # free text, as a prompt: never logged
            out.append(f"{k}=<{len(str(v))} chars>")
        elif k in ("id", "from") and isinstance(v, str):
            out.append(f"{k}={short(v)}")
        elif k == "names" and isinstance(v, list):  # of dropped files (drop.take): pasted into Claude Code, they are a part of a prompt
            out.append(f"{k}=<{len(v)}>")
        else:
            out.append(f"{k}={str(v)[:120]}")
    return f"{msg.get('cmd')} {' '.join(out)}".strip()
