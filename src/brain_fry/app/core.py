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
from ..logs import short
from ..util import child_env, claude_bin, write_atomic
from .ptys import PtySession

log = logging.getLogger(__name__)
SAVE_DIALOG = 30  # pywebview's FileDialog.SAVE (webview is imported only in the window)
ui_log = logging.getLogger("brain_fry.ui")

CONSUME_EVERY = 0.5  # s
SWEEP_EVERY = 60  # s: retry failed digests, close idle sessions
DIGEST_RETRY = 120  # s after a failure
DIGEST_NOW = 0  # the priority of a turn in the digest queue; the shutdown's (-1) goes first
IDLE_CLOSE = 4 * 3600  # s without events and output, see DESIGN_DETAILS.md "App"
OUTSIDE_EVERY = 3  # s: which tasks run elsewhere, the end of a block, projects.json
PROJECT_WS = "project:"  # the id of a project's files with no task selected (fs.* commands, /file/)
SHELL_INIT = Path(__file__).resolve().parent.parent / "shell_init.bash"  # the bash of terminal tabs runs it
# commands that the front end sends all the time (polling, file reads): logged at DEBUG only.
# Browser commands too: placements come at every frame of a drag, and URLs are browsing history.
QUIET = {"fs.list", "fs.read", "fs.versions", "thread.get", "log", "projects", "selection.set",
         "finder.list", "finder.info", "finder.thumb", "finder.tabs.set",
         "browser.place", "browser.open", "browser.go", "browser.nav", "browser.find", "browser.snapshot",
         "browser.tabs.set", "browser.cookies.info",
         "git.state", "git.log", "git.diff", "git.commit_info"}  # fmt: skip


def owned_file() -> Path:
    """Tasks whose `claude` runs in this app. Left over after a crash: those processes are gone."""
    return paths.bf_home() / "owned.json"


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
        self.shutting_down = False
        self.running: dict[str, dict] | None = None  # session id -> its process, from Claude Code
        self._projects_version: int | None = None
        self._distrust_logged = False
        self.browser = None  # app.browser.BrowserHost: the browser of 3a, in the window only (main.py)
        self.window = None  # pywebview's window (main.py): the save panel of an untitled tab (fs.saveas)

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
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
        """Blocking, from the main thread once the window is gone. Ends every live session
        and shell. Terminal tabs stay in the task files, to be restored at the next start."""
        self.shutting_down = True  # stops the background loops and the digest worker
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
        shells: dict[str, list[dict]] = {}
        for s in list(self.ptys.values()):
            if s.kind == "shell" and s.alive:  # cmd, cwd: from bash (shell_init.bash), for Cmd+N O
                shells.setdefault(s.task, []).append({"key": s.key, "gen": s.gen, "title": s.title,
                                                      "cmd": s.command, "cwd": s.folder})  # fmt: skip
        running = self.running or {}
        by_id = {t.id: views.task_json(t, gen := self._live_gen(t.id), shells.get(t.id),
                                       running.get(t.id) if gen is None else None) for t in tasks}  # fmt: skip
        inbox = [t.id for t in sorted(filter(views.in_inbox, tasks), key=views.inbox_order)]
        return {
            "type": "state",
            "tasks": by_id,
            "inbox": inbox,
            "projects": views.project_order(projects.load()["projects"], tasks),
            "selected": self.selected,
            "hookInstalled": (hook := self._hook_state())[0],
            "hookOutdated": hook[1],
            "digestError": self.digest_error,
        }

    @staticmethod
    def _hook_state() -> tuple[bool, bool]:
        """(installed, outdated): ~/.brain_fry/hook.sh differs from this version's hook.sh."""
        try:
            installed = paths.hook_file().exists() and install.hook_command() in install.default_settings().read_text()
            return installed, installed and paths.hook_file().read_text() != install.packaged_hook()
        except OSError:
            return False, False

    def _live_gen(self, tid: str) -> int | None:
        s = self.ptys.get(tid)
        return s.gen if s and s.alive else None

    def notify(self, msg: dict) -> None:
        """Thread-safe. A message to every front end, outside the state pushes."""
        if self.loop:
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
        if self.loop:
            self.loop.call_soon_threadsafe(self._schedule_broadcast)

    def _schedule_broadcast(self) -> None:
        if not self._broadcast_pending:
            self._broadcast_pending = True
            self.loop.call_later(0.05, lambda: self.loop.create_task(self._broadcast()))

    async def _broadcast(self) -> None:
        self._broadcast_pending = False
        if not self.clients:
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
        while not self.shutting_down:
            try:
                tasks = await asyncio.to_thread(actions.all_tasks)
                self._enqueue_digest([t.id for t in tasks if any(th.kind == "pending" for th in t.threads)])
                now = time.time()
                for t in tasks:
                    s = self.ptys.get(t.id)
                    if (s and s.alive and not t.inbox and now - views.epoch(t.updated) > IDLE_CLOSE
                            and now - s.last_output > IDLE_CLOSE):  # fmt: skip
                        log.info("closing idle session %s", t.id)
                        s.hangup()
            except Exception:
                log.exception("sweep")
            await asyncio.sleep(SWEEP_EVERY)

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
                log.info("task %s %r: %s", short(t.id), t.display_title[:50],
                         f"runs in {run['where']} (pid {run['pid']})" if run else "no process runs it any more")  # fmt: skip
                try:
                    actions.set_live(t.id, bool(run))
                    changed = True
                except actions.NotFound:
                    pass
        return changed

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
            except actions.NotFound:
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
            except digest.DigestError as e:  # logged by digest_task
                self.failed[tid] = time.time()
                self.digest_error = f"digest failed for {tid[:8]}: {e}"
                self.request_broadcast()
            except actions.NotFound:
                pass
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

        def mark() -> None:
            try:
                actions.set_live(s.key, False)
            except actions.NotFound:
                pass
            self._save_owned()
            self.request_broadcast()

        self.loop.run_in_executor(None, mark)

    def _shells_of(self, tid: str) -> list[PtySession]:
        return [s for s in self.ptys.values() if s.kind == "shell" and s.task == tid and s.alive]

    def _save_terminal_tabs(self, tid: str) -> None:
        try:
            actions.set_terminal_tabs(tid, [s.title for s in self._shells_of(tid)])
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
        too), else with the `python` of the shell's PATH."""
        p = files.entry(project, rel)
        if not p.is_file() or p.suffix != ".py":
            raise ValueError(f"Not a Python file: {rel}")
        venv = files.resolve(project, "") / ".venv" / "bin" / "python"
        return shlex.join([".venv/bin/python" if venv.exists() else "python", rel.strip("/")])

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
    def _resume_argv(task) -> list[str]:
        if task.threads:
            return [claude_bin(), "--resume", task.id]
        if task.parent:  # forked, never prompted: nothing to resume yet, fork again
            return [claude_bin(), "--resume", task.parent, "--fork-session", "--session-id", task.id]
        return [claude_bin(), "--session-id", task.id]  # started here, never prompted

    # ---------------------------------------------------------------- commands

    async def command(self, msg: dict):
        """Runs one command from the front end. The return value is the reply's data."""
        cmd, tid = msg.get("cmd"), msg.get("id")
        rows, cols = int(msg.get("rows") or 40), int(msg.get("cols") or 120)
        run = asyncio.to_thread
        (log.debug if cmd in QUIET else log.info)("command %s", _describe(msg))

        if cmd == "log":  # from the front end: its errors
            level = logging.ERROR if msg.get("level") == "error" else logging.INFO
            ui_log.log(level, "%s", str(msg.get("msg"))[:4000])
            return None
        if cmd == "projects":  # the picker asks each time it opens: projects.json may have changed
            return views.project_order((await run(projects.load))["projects"], await run(actions.all_tasks))
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
        if cmd == "fs.open":  # with the default app of macOS: a file that 3a does not show
            p = await run(files.resolve, await run(self._project_of, tid), msg["path"])
            await run(files.open_external, p)
            log.info("opened %s with the default app", msg["path"])
            return None
        if cmd == "fs.saveas":  # an untitled tab (Cmd+N F): the save panel of macOS; None if cancelled
            if self.window is None:
                raise ValueError("Saving a new file works only in the Brain Fry window, not with --browser.")
            project = await run(self._project_of, tid)
            chosen = await run(self._save_panel, project, str(msg.get("name") or "Untitled"))
            if not chosen:
                return None
            rel = await run(files.relative_in, project, chosen)
            log.info("new file %s in %s", rel, Path(project).name)
            return {"path": rel}
        if cmd == "fs.versions":
            return await run(files.versions, await run(self._project_of, tid), list(msg.get("paths") or []))
        if cmd == "fs.link":  # Cmd+click on a file link in a terminal: the target as it was written
            task = await run(actions.load, tid)
            return await run(files.link_target, task.project, task.cwd, str(msg.get("target") or ""))
        # the right-click menu of 2a: they change files, so they are logged
        if cmd == "fs.rename":
            project = await run(self._project_of, tid)
            new = await run(files.rename, project, msg["path"], str(msg.get("name") or ""))
            log.info("renamed %s to %s in %s", msg["path"], new, Path(project).name)
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
        if cmd == "finder.open":  # with the default app of macOS (a folder: in Finder)
            p = await run(finder.absolute, str(msg.get("path") or ""))
            await run(files.open_external, p)
            log.info("opened %s with the default app (Finder tab)", p.name)
            return None
        if cmd == "finder.rename":
            new = await run(finder.rename, str(msg.get("path") or ""), str(msg.get("name") or ""))
            log.info("renamed %s to %s (Finder tab)", msg.get("path"), Path(new).name)
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
            await run(actions.set_finder_tabs, tid, list(msg.get("tabs") or []))
            return None
        if cmd == "thread.get":  # one thread in full, for its tab in 3a
            task = await run(actions.load, tid)
            for th in task.threads:
                if th.n == int(msg.get("n", -1)):
                    return {"n": th.n, "ts": th.ts, "kind": th.kind, "digest": th.digest,
                            "question": th.question, "prompt": th.prompt, "reply": th.reply}  # fmt: skip
            raise ValueError(f"no thread {msg.get('n')} in task {tid}")
        if cmd == "files.set":  # the open file tabs of a task, restored when it is selected
            await run(actions.set_open_files, tid, list(msg.get("paths") or []))
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
            await run(actions.set_browser_tabs, tid, list(msg.get("urls") or []))
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

        # ---- Review mode (git.py): the repository of the task's folder; replies only
        if cmd.startswith("git."):
            task = await run(actions.load, tid)
            tasks = None
            if cmd == "git.state":  # the tasks of panel 1, under the worktree that holds their folder
                tasks = [{"id": t.id, "cwd": t.cwd, "status": t.status, "title": t.display_title}
                         for t in await run(actions.all_tasks) if t.project == task.project and t.status != "done"]  # fmt: skip
            return await run(git.command, cmd, task.cwd or task.project, task.project, msg, tasks)

        if cmd == "select":
            self.selected = tid
            if tid:
                task = await run(actions.mark_opened, tid)
                if self._live_gen(tid) is None and not task.live:
                    self._start_pty(tid, self._resume_argv(task), task.cwd, rows, cols)
                self._restore_shells(task, msg, cols)
        elif cmd == "idle":  # nothing needs me: 4a shows a photo, column 3 the tabs of the task that I opened last
            self._restore_shells(await run(actions.load, tid), msg, cols)  # no session, not selected, not `opened`
        elif cmd == "shell.new":  # path: "Open a terminal here" in 2a; folder: in a Finder tab; else the task's cwd
            task = await run(actions.load, tid)
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
            task = await run(actions.load, tid)
            src = self.ptys.get(str(msg.get("from")))
            if not src or src.kind != "shell" or not src.alive:
                raise ValueError("That terminal has ended.")
            if (await run(actions.load, src.task)).project != task.project:
                raise ValueError("That terminal belongs to another project.")
            self.restored.add(tid)
            s = self._new_shell(task, rows, cols, src.folder)
            await run(self._save_terminal_tabs, tid)
            self.request_broadcast()
            return {"key": s.key}
        elif cmd == "shell.close":
            s = self.ptys.get(str(msg.get("key")))
            if s and s.kind == "shell":
                s.hangup()  # the exit removes the tab and saves the list
        elif cmd == "resume":  # also for a session that seems to run elsewhere
            task = await run(actions.load, tid)
            if self._live_gen(tid) is None:
                self._start_pty(tid, self._resume_argv(task), task.cwd, rows, cols)
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
        elif cmd == "pin":  # or unpin a pinned task
            await run(actions.pin, tid)
        elif cmd == "rename":
            await run(actions.rename, tid, str(msg.get("title", "")))
        elif cmd == "prio":
            await run(actions.set_prio, tid, str(msg.get("prio")))
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
        else:
            out.append(f"{k}={str(v)[:120]}")
    return f"{msg.get('cmd')} {' '.join(out)}".strip()
