#!/usr/bin/env bash
# Minimal driver for a Claude Code instance running in a tmux pane.
#
# Usage:
#   claude-tmux-driver.sh ask  <pane>  "your prompt"
#   claude-tmux-driver.sh read <pane>
#   claude-tmux-driver.sh wait <pane>   # block until pane is idle
#
# <pane> is a tmux target like "0:3.2".
#
# Heuristics:
#   - "send" types text then sends Enter.
#   - "wait" considers the pane idle when capture-pane output is byte-identical
#     across N consecutive polls (default 3 polls x 1.5s).
#   - "read" returns the last screen the pane rendered.
#
# Caveats: this is keystroke injection. Permission prompts inside the target
# instance will block forever unless you send "1" + Enter. No structured
# response — parse what you get.

set -euo pipefail

POLL_INTERVAL="${POLL_INTERVAL:-1.5}"
STABLE_POLLS="${STABLE_POLLS:-3}"
MAX_WAIT="${MAX_WAIT:-300}"   # seconds

die() { echo "error: $*" >&2; exit 2; }

require_pane() {
  local pane="$1"
  tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' \
    | grep -qx "$pane" || die "no such pane: $pane"
}

cmd_send() {
  local pane="$1" prompt="$2"
  require_pane "$pane"
  # -l sends literally; then a separate Enter.
  tmux send-keys -t "$pane" -l -- "$prompt"
  tmux send-keys -t "$pane" Enter
}

cmd_read() {
  local pane="$1"
  require_pane "$pane"
  tmux capture-pane -p -t "$pane"
}

cmd_wait() {
  local pane="$1"
  require_pane "$pane"
  # Convert MAX_WAIT seconds to integer poll budget using awk (POLL_INTERVAL may be float).
  local max_polls
  max_polls=$(awk -v m="$MAX_WAIT" -v p="$POLL_INTERVAL" 'BEGIN{printf "%d", (m/p)+1}')
  local prev="" curr="" stable=0 polls=0
  while (( polls < max_polls )); do
    curr="$(tmux capture-pane -p -t "$pane")"
    if [[ "$curr" == "$prev" ]]; then
      stable=$((stable + 1))
      (( stable >= STABLE_POLLS )) && return 0
    else
      stable=0
    fi
    prev="$curr"
    sleep "$POLL_INTERVAL"
    polls=$((polls + 1))
  done
  echo "warning: pane did not go idle within ${MAX_WAIT}s" >&2
  return 1
}

cmd_ask() {
  local pane="$1" prompt="$2"
  cmd_send "$pane" "$prompt"
  sleep 1
  cmd_wait "$pane"
  cmd_read "$pane"
}

case "${1:-}" in
  send) shift; cmd_send "$@" ;;
  read) shift; cmd_read "$@" ;;
  wait) shift; cmd_wait "$@" ;;
  ask)  shift; cmd_ask  "$@" ;;
  *) die "usage: $0 {send|read|wait|ask} <pane> [prompt]" ;;
esac
