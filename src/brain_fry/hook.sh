#!/bin/bash
# Brain Fry hook. Installed by `bf install` as ~/.brain_fry/hook.sh and registered in
# ~/.claude/settings.json for UserPromptSubmit, Stop, Notification and SessionEnd.
# See DESIGN.md, "Hooks".
#
# Writes the payload, plus bf_ts (epoch seconds) and bf_project, as one file in
# <project>/.brain_fry/events/, where <project> is the nearest folder above cwd that
# has .brain_fry/. Temp file + rename: the reader never sees half an event.
#
# Prints nothing and always exits 0: on UserPromptSubmit, stdout would be injected into
# Claude's context, and a failing hook must never get in Claude's way.

[ -n "$BRAIN_FRY_INTERNAL" ] && exit 0
PATH="$PATH:/usr/bin:/opt/homebrew/bin:/usr/local/bin"
command -v jq >/dev/null 2>&1 || exit 0

payload=$(cat)
dir=$(printf '%s' "$payload" | jq -r '.cwd // empty' 2>/dev/null)

# Walk up, but stop below $HOME: ~/.brain_fry/ is the global folder, not a project.
while [ -n "$dir" ] && [ "$dir" != "/" ] && [ "$dir" != "$HOME" ]; do
  if [ -d "$dir/.brain_fry" ]; then
    spool="$dir/.brain_fry/events"
    mkdir -p "$spool" 2>/dev/null || exit 0
    name="$(date +%s)-$$-$RANDOM"
    if printf '%s' "$payload" \
        | jq -c --arg project "$dir" '. + {bf_ts: now, bf_project: $project}' \
          > "$spool/.$name.tmp" 2>/dev/null; then
      mv "$spool/.$name.tmp" "$spool/$name.json" 2>/dev/null
    fi
    rm -f "$spool/.$name.tmp" 2>/dev/null
    exit 0
  fi
  dir=$(dirname "$dir")
done
exit 0
