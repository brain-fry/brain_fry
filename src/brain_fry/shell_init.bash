# Brain Fry's start file for the bash of its terminal tabs (app/core.py, _new_shell). Bash runs it
# in place of ~/.bashrc (`bash --init-file`), as VS Code's shell integration does. It reads the
# files that a login shell reads, then adds two hooks that tell the app the last command and the
# current folder, as escape sequences that the terminal ignores (OSC 7771, read by app/ptys.py).
# The list of Cmd+N O shows them (DESIGN_DETAILS.md, "2a, 3a, 3b: files and terminals").

if [ -n "$BF_SHELL_LOGIN" ]; then # what `bash -l` reads
  unset BF_SHELL_LOGIN
  [ -r /etc/profile ] && . /etc/profile
  if [ -r ~/.bash_profile ]; then . ~/.bash_profile
  elif [ -r ~/.bash_login ]; then . ~/.bash_login
  elif [ -r ~/.profile ]; then . ~/.profile
  fi
else
  [ -r ~/.bashrc ] && . ~/.bashrc
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
# per line. A line that history did not record (an empty line, the commands of PROMPT_COMMAND, a
# line that HISTCONTROL ignores) leaves the history number as it was, and is not sent.
__bf_preexec() {
  [ -n "$__bf_ready" ] || return 0
  [ -n "$COMP_LINE" ] && return 0
  __bf_last
  [ "$__bf_num" = "$__bf_hist" ] && return 0
  __bf_ready=
  __bf_hist=$__bf_num
  __bf_send cmd "$__bf_line"
}

# Before each prompt (last in PROMPT_COMMAND): the folder. Keeps $? for the prompt.
__bf_precmd() {
  local s=$?
  __bf_send cwd "$PWD"
  __bf_ready=1
  return $s
}

__bf_last
__bf_hist=$__bf_num
__bf_ready=
trap '__bf_preexec' DEBUG
PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND$'\n'}__bf_precmd"
