#!/bin/bash
# Brain Fry hook. Installed by `bf install` as ~/.brain_fry/hook.sh and registered in
# ~/.claude/settings.json for UserPromptSubmit, Stop, Notification and SessionEnd.
# See DESIGN.md, "Hooks", and DESIGN_DETAILS.md, "Hooks".
#
# Writes the payload, plus bf_ts (epoch seconds), as one file in
# <project>/.brain_fry/events/, where <project> is the nearest folder above cwd that
# has .brain_fry/. Temp file + rename: the reader never sees half an event.
# Appends one line per written event, and each error, to ~/.brain_fry/logs/hook.log.
#
# Always exits 0: a failing hook must never get in Claude's way. Prints nothing, except on
# UserPromptSubmit the text that I selected in Brain Fry's editor, when the prompt goes to the
# task of that editor: Claude Code adds that stdout to the model's context (DESIGN_DETAILS.md,
# "Selection as context"). Brain Fry writes the selection to ~/.brain_fry/selection.json.

[ -n "$BRAIN_FRY_INTERNAL" ] && exit 0
PATH="$PATH:/usr/bin:/opt/homebrew/bin:/usr/local/bin"
logfile="${BRAIN_FRY_HOME:-$HOME/.brain_fry}/logs/hook.log"
note() {
  mkdir -p "$(dirname "$logfile")" 2>/dev/null
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$logfile" 2>/dev/null
}
command -v jq >/dev/null 2>&1 || { note "ERROR jq not found: event lost"; exit 0; }

payload=$(cat)
# \x1f between the fields: with a tab, bash's read would merge empty fields
IFS=$'\x1f' read -r dir event sid < <(printf '%s' "$payload" \
  | jq -r '[.cwd // "", .hook_event_name // "", .session_id // ""] | join("\u001f")' 2>/dev/null)

# The selection: only for its own task, and only while the app that wrote it runs (a crash
# leaves the file behind).
selnote=""
selfile="${BRAIN_FRY_HOME:-$HOME/.brain_fry}/selection.json"
if [ "$event" = "UserPromptSubmit" ] && [ -n "$sid" ] && [ -f "$selfile" ]; then
  IFS=$'\x1f' read -r selsid selpid seldesc < <(jq -r \
    '[.session_id // "", (.pid // 0 | tostring), "\(.path) \(.from)-\(.to)"] | join("\u001f")' "$selfile" 2>/dev/null)
  if [ "$selsid" = "$sid" ] && kill -0 "$selpid" 2>/dev/null; then
    jq -r '"The user selected "
      + (if .from == .to then "line \(.from)" else "the lines \(.from) to \(.to)" end) + " from \(.path)"
      + (if .dirty then " (in the editor, with unsaved changes: the file on disk differs)" else "" end) + ":\n"
      + .text + (if .cut then "\n[... cut here: read the file for the rest]" else "" end)
      + "\n\nThis may or may not be related to the current task."' "$selfile" 2>/dev/null \
      && selnote=" + selection $seldesc"
  fi
fi

# Walk up, but stop below $HOME: ~/.brain_fry/ is the global folder, not a project.
while [ -n "$dir" ] && [ "$dir" != "/" ] && [ "$dir" != "$HOME" ]; do
  if [ -d "$dir/.brain_fry" ]; then
    spool="$dir/.brain_fry/events"
    mkdir -p "$spool" 2>/dev/null || { note "ERROR cannot create $spool: $event ${sid:0:8} lost"; exit 0; }
    name="$(date +%s)-$$-$RANDOM"
    if printf '%s' "$payload" \
        | jq -c '. + {bf_ts: now}' \
          > "$spool/.$name.tmp" 2>/dev/null \
        && mv "$spool/.$name.tmp" "$spool/$name.json" 2>/dev/null; then
      note "$event ${sid:0:8} --> $dir$selnote"
    else
      note "ERROR cannot write $event ${sid:0:8} to $spool: event lost"
    fi
    rm -f "$spool/.$name.tmp" 2>/dev/null
    exit 0
  fi
  dir=$(dirname "$dir")
done
exit 0
