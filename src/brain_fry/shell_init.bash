# Brain Fry's start file for the bash of its terminal tabs (app/core.py, _new_shell). Bash runs it
# in place of ~/.bashrc (`bash --init-file`), as VS Code's shell integration does. It reads the
# files that a login shell reads, then adds two hooks that tell the app the last command and the
# current folder, as escape sequences that the terminal ignores (OSC 7771, read by app/ptys.py).
# The list of Cmd+N O shows them, and a command that may change files reloads the explorer
# (DESIGN_DETAILS.md, "2a, 3a, 3b: files and terminals"). It also
# defines `bf <file>`, which opens a file in the editor.

# what `bash -l` reads
[ -r /etc/profile ] && . /etc/profile
if [ -r ~/.bash_profile ]; then . ~/.bash_profile
elif [ -r ~/.bash_login ]; then . ~/.bash_login
elif [ -r ~/.profile ]; then . ~/.profile
fi

__bf_send() { builtin printf '\033]7771;%s;%s\007' "$1" "${2//[[:cntrl:]]/ }"; }

# The number of the last history entry, and its text in __bf_line.
__bf_last() {
  local h lead
  h=$(HISTTIMEFORMAT= builtin history 1)
  lead=${h%%[! ]*}; h=${h#"$lead"}
  __bf_num=${h%%[ *]*}
  h=${h#"$__bf_num"}; h=${h#\*}
  lead=${h%%[! ]*}; __bf_line=${h#"$lead"}
}

# Before a command line runs (the DEBUG trap fires before each simple command): its text, once
# per line. A line that history did not record (the same line again under HISTIGNORE="&" or
# ignoredups, a line with a leading space under ignorespace) leaves the history number as it was:
# it sends `ran` and its first command, since it too may change files (the explorer reloads,
# ptys.changes_files). An empty line runs only PROMPT_COMMAND, whose first command is __bf_prompt:
# nothing is sent.
__bf_preexec() {
  [ -n "$__bf_ready" ] || return 0
  [ -n "$COMP_LINE" ] && return 0
  __bf_last
  __bf_ready=
  if [ "$__bf_num" != "$__bf_hist" ]; then
    __bf_hist=$__bf_num
    __bf_send cmd "$__bf_line"
  elif [ "$BASH_COMMAND" != __bf_prompt ]; then
    __bf_send ran "$BASH_COMMAND"
  fi
}

# First in PROMPT_COMMAND: marks the start of the prompt for __bf_preexec. Keeps $?.
__bf_prompt() { return $?; }

# Before each prompt (last in PROMPT_COMMAND): the folder. Keeps $? for the prompt.
__bf_precmd() {
  local s=$?
  __bf_send cwd "$PWD"
  __bf_ready=1
  return $s
}

# `bf <file>...` opens the files in Brain Fry's editor (DESIGN_DETAILS.md, "2a, 3a, 3b", `bf <file>`):
# each absolute path goes to the app as OSC 7771 `open` (app/ptys.py, Core._shell_open), written to
# the terminal itself, so a redirected output does not swallow it. `bf` alone, its commands (the
# list of cli.COMMANDS) and its options run the real `bf`; `bf ./init` opens a file named `init`.
bf() {
  case "$1" in
    ''|-*|install|uninstall|ublock|init) command bf "$@"; return ;;
  esac
  local f
  for f in "$@"; do
    case "$f" in /*) ;; *) f="$PWD/$f" ;; esac
    __bf_send open "$f" > /dev/tty
  done
}

__bf_last
__bf_hist=$__bf_num
__bf_ready=
trap '__bf_preexec' DEBUG
PROMPT_COMMAND="__bf_prompt"$'\n'"${PROMPT_COMMAND:+$PROMPT_COMMAND$'\n'}__bf_precmd"
