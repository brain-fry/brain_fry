import os
from datetime import datetime
from pathlib import Path

# Variables that Claude Code and VS Code set for their own children. When Brain Fry is
# started from inside one of them, a `claude` child that inherits these may believe that it
# is nested or attached to an IDE. Seen on 2026-11-09 (Claude Code 2.1.318 in VS Code).
# CLAUDE_CODE_* variables that a user sets on purpose (CLAUDE_CODE_USE_BEDROCK, ...) are kept.
CHILD_ENV_DROP = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EMIT_STARTUP_TIMING", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT",
    "CLAUDE_CODE_ENABLE_TASKS", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "ELECTRON_RUN_AS_NODE",
}


def claude_bin() -> str:
    """The Claude Code command. BRAIN_FRY_CLAUDE overrides it (tests use a fake)."""
    return os.environ.get("BRAIN_FRY_CLAUDE") or "claude"


def child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP and not k.startswith("VSCODE_")}
    env.update(extra)
    return env


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and a rename, so that a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def iso(epoch: float | None = None) -> str:
    """Local time with offset, to the second: 2026-11-09T14:03:12+01:00."""
    dt = datetime.fromtimestamp(epoch) if epoch is not None else datetime.now()
    return dt.astimezone().isoformat(timespec="seconds")
