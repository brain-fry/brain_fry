# TODOs

## V2

see @DESIGN.md: in dev mode: 2a goes below 1a-c. saves space for the middle pannels of 3

3 allows to rearrange panels, e.g. 2 columns, or just 2 rows, but most of the time i will work with 4 panels. all resizeable.

support for a web browser in 3. ideally, it's my firefox browser with all cookies etc

support for another file explorer in 3, opened at another place. with tabs. The rationale is that very often, within a project, I'm using multiple instances of Finder windows to find different files. Ideally, it has the exact same look and feel as the macOSx Finder in Column View

remove the region "Other Claude Code sessions" under the board, and adopt (decided 2026-11-10). It only serves the sessions that started before `bf install` or `bf init`: every new session in a project fires hooks. Workaround: `claude --resume` in the project (it opens Claude Code's session picker), send one prompt, exit; the hook creates the task, and `events._import_history` imports the earlier turns from the transcript (not tested end to end with an old session). Removes `sessions.scan`, `_Summary`, `adopt`, the cards and drag-to-adopt in app.js: about 300 lines with tests. Keep `sessions.running()` (Claude Code's process files): the end of a block depends on it.




## V3

keyboard shortcuts to rearrange the panels

be able to "use" the title of the window, add buttons to it. make it black, too