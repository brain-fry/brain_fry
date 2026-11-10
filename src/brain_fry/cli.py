"""The `bf` command."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import digest, events, install, logs, projects, taskfile, ublock, views

log = logging.getLogger(__name__)

STATUS_MARK = {"working": "o", "blocked": "R", "waiting": "G", "done": "-"}  # orange, red, green, gray


def cmd_install(a) -> None:
    for line in install.install() or ["nothing to do"]:
        log.info("install: %s", line)
        print(line)
    print("Restart running Claude Code sessions (or review the change in /hooks) to load the hook.")
    try:  # the browser of 3a works without it: a failure is a warning
        line = ublock.update()
        log.info("install: %s", line)
        print(line)
    except Exception as e:
        log.warning("install: uBlock Origin Lite not updated: %s", e)
        print(f"uBlock Origin Lite not updated ({e}). The browser works without it; `bf ublock` retries.")


def cmd_ublock(a) -> None:
    if a.zip:
        version = ublock.install_zip(Path(a.zip).expanduser(), a.sha256)
        line = f"installed uBlock Origin Lite {version} from {a.zip} in {ublock.ext_dir()} (restart Brain Fry to load it)"
    else:
        line = ublock.update()
    log.info("ublock: %s", line)
    print(line)


def cmd_uninstall(a) -> None:
    for line in install.uninstall() or ["nothing to do"]:
        print(line)


def cmd_init(a) -> None:
    path = Path(a.path).expanduser().resolve()
    new = projects.init(path)
    log.info("init %s: %s", path, "registered" if new else "already registered")
    print(f"{'registered' if new else 'already registered'}: {path}")


def cmd_consume(a) -> None:
    for project in projects.project_paths():
        for task_id in events.consume(project):
            print(f"{project.name}: {task_id}")


def cmd_digest(a) -> None:
    for project in projects.project_paths():
        events.consume(project)
        changed, errors = digest.digest_project(project)
        for task_id in changed:
            print(f"{project.name}: digested {task_id}")
        for e in errors:
            print(f"{project.name}: FAILED {e}", file=sys.stderr)


def cmd_list(a) -> None:
    if not a.no_consume:
        for project in projects.project_paths():
            events.consume(project)
    tasks = [t for p in projects.project_paths() for t in taskfile.all_tasks(p)]
    if not a.all:
        tasks = [t for t in tasks if t.status != "done"]
    inbox = sorted(filter(views.in_inbox, tasks), key=views.inbox_order)
    rest = sorted((t for t in tasks if not views.in_inbox(t)), key=views.board_order)
    for header, group in (("inbox", inbox), ("other", rest)):
        print(f"== {header} ({len(group)})")
        for t in group:
            mark = STATUS_MARK.get(t.status, "?") + ("*" if t.live else " ") + ("!" if t.unread else " ")
            pin = "  (pinned)" if t.pinned else ""
            print(f"{mark} {t.prio:<10} {Path(t.project).name:<16} {t.display_title}  [{t.id[:8]}]{pin}")
            if t.inbox and (line := views.inbox_line(t)):
                kind = t.threads[-1].kind if t.threads and t.threads[-1].digest else None
                print(f"{'':32}{f'[{kind}] ' if kind else ''}{line}")
                if t.threads and t.threads[-1].question:
                    print(f"{'':32}Q: {t.threads[-1].question}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bf", description="Brain Fry: Claude Code sessions as an owner inbox. Without a command: the window.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("install", help="install the hook (once per machine)").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="remove the hook from ~/.claude/settings.json").set_defaults(func=cmd_uninstall)
    p = sub.add_parser("ublock", help="install or update uBlock Origin Lite for the browser of 3a")
    p.add_argument("--zip", help="install this Safari build zip instead of downloading the latest release")
    p.add_argument("--sha256", help="with --zip: refuse the zip unless it has this SHA-256")
    p.set_defaults(func=cmd_ublock)
    p = sub.add_parser("init", help="make a folder a Brain Fry project")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_init)
    sub.add_parser("consume", help="apply pending hook events to task files").set_defaults(func=cmd_consume)
    sub.add_parser("digest", help="apply events, then digest pending threads with Sonnet").set_defaults(func=cmd_digest)
    p = sub.add_parser("list", help="list tasks, inbox first (status: o working, R blocked, G waiting, - done)")
    p.add_argument("--all", action="store_true", help="include archived tasks")
    p.add_argument("--no-consume", action="store_true", help="do not apply pending events first")
    p.set_defaults(func=cmd_list)
    ap.add_argument("--debug", action="store_true", help="window: right click --> Inspect Element")
    ap.add_argument("--browser", action="store_true", help="no window: print a URL to open in a browser")
    ap.add_argument("--selftest", type=float, metavar="SECONDS", help=argparse.SUPPRESS)
    ap.add_argument("--selftest-new", metavar="PROJECT", help=argparse.SUPPRESS)
    ap.add_argument("--selftest-shots", metavar="DIR", help=argparse.SUPPRESS)
    ap.add_argument("--selftest-js", metavar="JS", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if not a.cmd:
        from .app.main import run

        return run(debug=a.debug, browser=a.browser, selftest=a.selftest, selftest_new=a.selftest_new,
                   selftest_shots=a.selftest_shots, selftest_js=a.selftest_js)  # fmt: skip
    logs.setup(f"cli:{a.cmd}")
    log.info("bf %s", " ".join(argv if argv is not None else sys.argv[1:]))
    try:
        a.func(a)
    except Exception:
        log.exception("bf %s failed", a.cmd)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
