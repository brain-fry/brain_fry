# TODOs

## V2

see @DESIGN.md: in dev mode: 2a goes below 1a-c. saves space for the middle pannels of 3

3 allows to rearrange panels, e.g. 2 columns, or just 2 rows, but most of the time i will work with 4 panels. all resizeable.

support for a web browser in 3. ideally, it's my firefox browser with all cookies etc

support for another file explorer in 3, opened at another place. with tabs. The rationale is that very often, within a project, I'm using multiple instances of Finder windows to find different files. Ideally, it has the exact same look and feel as the macOSx Finder in Column View

remove the region "Other Claude Code sessions" under the board, and adopt (decided 2026-11-10). It only serves the sessions that started before `bf install` or `bf init`: every new session in a project fires hooks. Workaround: `claude --resume` in the project (it opens Claude Code's session picker), send one prompt, exit; the hook creates the task, and `events._import_history` imports the earlier turns from the transcript (not tested end to end with an old session). Removes `sessions.scan`, `_Summary`, `adopt`, the cards and drag-to-adopt in app.js: about 300 lines with tests. Keep `sessions.running()` (Claude Code's process files): the end of a block depends on it.



### 3rd mode: review

- view similar to SourceTree
- what i use and like:
    - I like the panel at the top with a graph, a column with the Git graph, and the Git messages, commit hash, author, and date. I need a panel like that at the top. The panel on the left, I don't use it much, but maybe it could still be there for the work trees, etc., and it varies.
    - I don't need to stash anything. You just need to have the branches, maybe, or the work tree, like a little panel on the left. The main panels at the bottom are the pending files that have a state, like the staged files and the unstaged files. This is really all I need.
    - On the right, I have a diff of the staged versus unstaged file. I like to work that way. The diff is read-only, and I can stage or unstage different hunks. I like that.
- support worktrees

## V3

keyboard shortcuts to rearrange the panels

be able to "use" the title of the window, add buttons to it. make it black, too