"""The backend: task state, my commands, the PTYs, and two background jobs (the reader of
the spools, every 0.5 s; the digest worker, in its own thread). Runs in the asyncio thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from .. import actions, digest, events, files, install, paths, projects, views
from ..util import child_env, claude_bin, write_atomic
from .ptys import PtySession

log = logging.getLogger("brain_fry")

CONSUME_EVERY = 0.5  # s
SWEEP_EVERY = 60  # s: retry failed digests, close idle sessions
DIGEST_RETRY = 120  # s after a failure
IDLE_CLOSE = 4 * 3600  # s without events and output, see DESIGN.md "App"


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
        self.digest_q: queue.Queue[str | None] = queue.Queue()  # None stops the worker
        self.queued: set[str] = set()
        self.failed: dict[str, float] = {}
        self.digest_error: str | None = None
        self._broadcast_pending = False
        self.shell_seq = 0
        self.restored: set[str] = set()  # tasks whose terminal tabs were restored since start
        self.shutting_down = False

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._reset_leftover_owned)
        threading.Thread(target=self._digest_worker, name="digest", daemon=True).start()
        self.loop.create_task(self._consume_loop())
        self.loop.create_task(self._sweep_loop())

    def _reset_leftover_owned(self) -> None:
        f = owned_file()
        if not f.exists():
            return
        for tid in json.loads(f.read_text() or "[]"):
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
        self.shutting_down = True  # stops the reader, the sweep and the digest worker
        self.digest_q.put(None)
        sessions = [s for s in self.ptys.values() if s.alive]
        for s in sessions:
            s.hangup()
        for s in sessions:
            s.kill()
            if s.kind == "claude":
                try:
                    actions.set_live(s.key, False)
                except actions.NotFound:
                    pass
        owned_file().unlink(missing_ok=True)

    # ---------------------------------------------------------------- state

    def snapshot(self) -> dict:
        tasks = actions.all_tasks()
        shells: dict[str, list[dict]] = {}
        for s in list(self.ptys.values()):
            if s.kind == "shell" and s.alive:
                shells.setdefault(s.task, []).append({"key": s.key, "gen": s.gen, "title": s.title})
        by_id = {t.id: views.task_json(t, self._live_gen(t.id), shells.get(t.id)) for t in tasks}
        inbox = [t.id for t in sorted((t for t in tasks if t.inbox and t.status != "done"), key=views.inbox_order)]
        archive = [t.id for t in sorted((t for t in tasks if t.status == "done"), key=lambda t: -views.epoch(t.updated))]
        board = [t.id for t in sorted((t for t in tasks if t.status != "done"), key=views.board_order)]
        return {
            "type": "state",
            "tasks": by_id,
            "inbox": inbox,
            "archive": archive,
            "board": board,
            "projects": projects.load()["projects"],
            "selected": self.selected,
            "hookInstalled": self._hook_installed(),
            "digestError": self.digest_error,
        }

    @staticmethod
    def _hook_installed() -> bool:
        s = install.default_settings()
        try:
            return paths.hook_file().exists() and install.hook_command() in s.read_text()
        except OSError:
            return False

    def _live_gen(self, tid: str) -> int | None:
        s = self.ptys.get(tid)
        return s.gen if s and s.alive else None

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
        for p in projects.project_paths():
            changed += events.consume(p)
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
        now = time.time()
        for tid in ids:
            if tid not in self.queued and now - self.failed.get(tid, 0) > DIGEST_RETRY:
                self.queued.add(tid)
                self.digest_q.put(tid)

    def _digest_worker(self) -> None:
        while True:
            tid = self.digest_q.get()
            if tid is None:
                return
            self.queued.discard(tid)
            try:
                task = actions.load(tid)
                if any(th.kind == "pending" for th in task.threads):
                    digest.digest_task(task)
                    self.failed.pop(tid, None)
                    self.digest_error = None
                    self.request_broadcast()
            except digest.DigestError as e:
                self.failed[tid] = time.time()
                self.digest_error = f"digest failed for {tid[:8]}: {e}"
                log.warning(self.digest_error)
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

    def _project_of(self, tid: str) -> str:
        return actions.load(tid).project

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

        # ---- files (2a, 3a): replies only, no state change
        if cmd == "fs.list":
            return await run(files.list_dir, await run(self._project_of, tid), msg.get("path") or "")
        if cmd == "fs.read":
            return await run(files.read_file, await run(self._project_of, tid), msg["path"])
        if cmd == "fs.write":
            project = await run(self._project_of, tid)
            try:
                return await run(files.write_file, project, msg["path"], msg["text"], msg.get("base"), bool(msg.get("force")))
            except files.Conflict:
                return {"conflict": True}
        if cmd == "fs.versions":
            return await run(files.versions, await run(self._project_of, tid), list(msg.get("paths") or []))
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

        if cmd == "select":
            self.selected = tid
            if tid:
                task = await run(actions.load, tid)
                if task.unread:
                    await run(actions.mark_read, tid)
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
            task = await run(actions.new_task, Path(msg["project"]))
            self._start_pty(task.id, [claude_bin(), "--session-id", task.id], task.cwd, rows, cols)
            self.selected = task.id
        elif cmd == "fork":
            task = await run(actions.fork_task, tid)
            argv = [claude_bin(), "--resume", tid, "--fork-session", "--session-id", task.id]
            self._start_pty(task.id, argv, task.cwd, rows, cols)
            self.selected = task.id
        elif cmd == "dismiss":
            await run(actions.dismiss, tid)
        elif cmd == "archive":
            await run(actions.archive, tid)
        elif cmd == "rename":
            await run(actions.rename, tid, str(msg.get("title", "")))
        elif cmd == "prio":
            await run(actions.set_prio, tid, str(msg.get("prio")))
        else:
            raise ValueError(f"unknown command {cmd!r}")
        self.request_broadcast()
        return None
