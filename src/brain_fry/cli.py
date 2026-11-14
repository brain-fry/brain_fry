"""The `bf` command."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import install, logs, projects, ublock

log = logging.getLogger(__name__)


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


COMMANDS = ("install", "uninstall", "ublock", "init")  # also in shell_init.bash, the `bf` function


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and not args[0].startswith("-") and args[0] not in COMMANDS:  # `bf notes.txt`
        print("bf: `bf <file>` opens a file in Brain Fry's editor, and works only in the terminal tabs of Brain Fry (bash).",
              file=sys.stderr)  # fmt: skip
        return 2
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
    ap.add_argument("--debug", action="store_true", help="window: right click --> Inspect Element")
    ap.add_argument("--browser", action="store_true", help="no window: print a URL to open in a browser")
    ap.add_argument("--selftest", type=float, metavar="SECONDS", help=argparse.SUPPRESS)
    ap.add_argument("--selftest-shots", metavar="DIR", help=argparse.SUPPRESS)
    ap.add_argument("--selftest-js", metavar="JS", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if not a.cmd:
        from .app.main import run

        return run(debug=a.debug, browser=a.browser, selftest=a.selftest,
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
