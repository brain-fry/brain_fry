# TUI to manage agent sessions

Yet another GUI to manage agent sessions.
Loosely inspired by @brain_fry.md
See @DESIGN.md for the ongoing design: Renaud's spec and the core. The details, one section per area, are in DESIGN_DETAILS.md (not loaded at start): read the section of the area that you work on before you change it, and write new decisions there.

Python, targetted at Claude Code

Tests: `uv run pytest`

Logs of the running app: `~/.brain_fry/logs/bf.log` and `~/.brain_fry/logs/hook.log` (see DESIGN_DETAILS.md, "Logs"). When Renaud asks for a review of the logs, read both.

Repo: https://github.com/brain-fry/brain_fry.git
