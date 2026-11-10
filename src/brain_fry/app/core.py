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
import threading
import time
from pathlib import Path

from .. import actions, digest, events, files, firefox, git, install, paths, projects, selection, sessions, views
from ..logs import short
from ..util import child_env, claude_bin, write_atomic
from .ptys import PtySession

log = logging.getLogger(__name__)
ui_log = logging.getLogger("brain_fry.ui")

CONSUME_EVERY = 0.5  # s
SWEEP_EVERY = 60  # s: retry failed digests, close idle sessions
DIGEST_RETRY = 120  # s after a failure
DIGEST_NOW, DIGEST_LATER = 0, 1  # priorities in the digest queue: turns first, then short titles of older tasks
IDLE_CLOSE = 4 * 3600  # s without events and output, see DESIGN_DETAILS.md "App"
OTHERS_EVERY = 3  # s: other sessions, and which tasks run elsewhere
# commands that the front end sends all the time (polling, file reads): logged at DEBUG only.
# Browser commands too: placements come at every frame of a drag, and URLs are browsing history.
QUIET = {"fs.list", "fs.read", "fs.versions", "thread.get", "log", "projects", "selection.set",
         "browser.place", "browser.open", "browser.go", "browser.nav", "browser.find", "browser.snapshot",
         "browser.focus", "browser.tabs.set", "browser.cookies.info",
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
        self.queued: dict[str, int] = {}  # task id -> its best priority in the queue
        self._digest_seq = itertools.count()
        self.failed: dict[str, float] = {}
        self.digest_error: str | None = None
        self._broadcast_pending = False
        self.shell_seq = 0
        self.restored: set[str] = set()  # tasks whose terminal tabs were restored since start
        self.shutting_down = False
        self.others: list[dict] = []  # sessions of my projects that are not tasks
        self.running: dict[str, dict] | None = None  # session id -> its process, from Claude Code
        self._projects_version: int | None = None
        self._distrust_logged = False
        self.browser = None  # app.browser.BrowserHost: the browser of 3a, in the window only (main.py)

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._reset_leftover_owned)
        selection.clear()  # left over after a crash (the hook also checks that our pid lives)
        threading.Thread(target=self._digest_worker, name="digest", daemon=True).start()
        self.loop.create_task(self._consume_loop())
        self.loop.create_task(self._sweep_loop())
        self.loop.create_task(self._others_loop())

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
            if s.kind == "shell" and s.alive:
                shells.setdefault(s.task, []).append({"key": s.key, "gen": s.gen, "title": s.title})
        running = self.running or {}
        by_id = {t.id: views.task_json(t, gen := self._live_gen(t.id), shells.get(t.id),
                                       running.get(t.id) if gen is None else None) for t in tasks}  # fmt: skip
        inbox = [t.id for t in sorted(filter(views.in_inbox, tasks), key=views.inbox_order)]
        archive = [t.id for t in sorted((t for t in tasks if t.status == "done"), key=lambda t: -views.epoch(t.updated))]
        board = [t.id for t in sorted((t for t in tasks if t.status != "done"), key=views.board_order)]
        return {
            "type": "state",
            "tasks": by_id,
            "inbox": inbox,
            "archive": archive,
            "board": board,
            "projects": views.project_order(projects.load()["projects"], tasks),
            "selected": self.selected,
            "hookInstalled": (hook := self._hook_state())[0],
            "hookOutdated": hook[1],
            "digestError": self.digest_error,
            "others": self.others,
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
                self._enqueue_digest([t.id for t in tasks if digest.needs_call(t)], DIGEST_LATER)  # short titles
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

    def _enqueue_digest(self, ids: list[str], prio: int = DIGEST_NOW) -> None:
        """A task already queued with a lower priority is queued again; its first entry
        out of the queue does the work, the other is skipped."""
        now = time.time()
        for tid in ids:
            if self.queued.get(tid, 99) > prio and now - self.failed.get(tid, 0) > DIGEST_RETRY:
                log.debug("digest %s queued (priority %d)%s", short(tid), prio, " (retry)" if tid in self.failed else "")
                self.queued[tid] = prio
                self.digest_q.put((prio, next(self._digest_seq), tid))

    async def _others_loop(self) -> None:
        while not self.shutting_down:
            try:
                if await asyncio.to_thread(self._refresh_others):
                    self.request_broadcast()
            except Exception:
                log.exception("other sessions")
            await asyncio.sleep(OTHERS_EVERY)

    def _refresh_others(self) -> bool:
        """Which sessions run now (Claude Code's files), the tasks whose process started or
        ended outside the app, and the other sessions of my projects. True if any changed."""
        running = sessions.running()
        if running is not None and not self._trust(running):
            running = None
        tasks = actions.all_tasks()
        changed = self._sync_outside(tasks, running)
        changed |= self._end_blocks(tasks, running)
        others = sessions.scan({sid for t in tasks for sid in t.session_ids}, running)
        if [o["id"] for o in others] != [o["id"] for o in self.others]:
            log.info("other sessions: %d (%s)", len(others), ", ".join(short(o["id"]) for o in others[:12]))
        try:
            version = paths.projects_file().stat().st_mtime_ns
        except OSError:
            version = None
        if version != self._projects_version and self._projects_version is not None:
            log.info("projects.json changed: %s", ", ".join(str(p) for p in projects.project_paths()))
        changed |= others != self.others or running != self.running or version != self._projects_version
        self.others, self.running, self._projects_version = others, running, version
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
            prio, _, tid = self.digest_q.get()
            if tid is None:
                return
            if self.queued.get(tid) != prio:
                continue  # queued again with a higher priority, and done then
            del self.queued[tid]
            try:
                task = actions.load(tid)
                if digest.needs_call(task):
                    digest.digest_task(task)
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

    def _new_shell(self, task, rows: int, cols: int) -> PtySession:
        self.shell_seq += 1
        shell = os.environ.get("SHELL") or "/bin/zsh"
        return self._start_pty(f"shell:{task.id}:{self.shell_seq}", [shell, "-l"], task.cwd, rows, cols,
                               kind="shell", task=task.id, title=Path(shell).name)  # fmt: skip

    def _start_copied_shells(self, task, msg: dict, cols: int) -> None:
        """A new or forked task copied the terminal tabs of another task: fresh shells, now."""
        self.restored.add(task.id)
        for _ in task.terminal_tabs:
            self._new_shell(task, int(msg.get("shellRows") or 12), int(msg.get("shellCols") or cols))

    def _project_of(self, tid: str) -> str:
        return actions.load(tid).project

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
                if tid not in self.restored:  # terminal tabs from the last run: fresh shells
                    self.restored.add(tid)
                    for _ in range(len(task.terminal_tabs) - len(self._shells_of(tid))):
                        self._new_shell(task, int(msg.get("shellRows") or 12), int(msg.get("shellCols") or cols))
        elif cmd == "shell.new":
            task = await run(actions.load, tid)
            self.restored.add(tid)
            s = self._new_shell(task, rows, cols)
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
        elif cmd == "adopt":  # a session of the bottom region of the board becomes a task
            sid = str(msg.get("session"))
            task = await run(sessions.adopt, sid, str(msg.get("prio") or "working"), (self.running or {}).get(sid))
            self.others = [o for o in self.others if o["id"] != sid]
            if any(th.kind == "pending" for th in task.threads):
                self._enqueue_digest([task.id])
            self.request_broadcast()
            return {"id": task.id}
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
        elif k in ("id", "session", "from") and isinstance(v, str):
            out.append(f"{k}={short(v)}")
        else:
            out.append(f"{k}={str(v)[:120]}")
    return f"{msg.get('cmd')} {' '.join(out)}".strip()
