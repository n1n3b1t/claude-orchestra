#!/usr/bin/env bash
# v2.0 e2e: PM coordinates an architect, three parallel engineers (backend,
# web, cli), and a reviewer to build a Trello-lite kanban app. Three
# watchdogs (wall-clock, activity, cost). The kanban verifier exiting 0
# in the temp project is the acceptance signal.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="${KANBAN_PROJECT_DIR:-/tmp/kanban-e2e}"
SESSION_NAME="orch-kanban-e2e"
WALLCLOCK_SECONDS="${WALLCLOCK_SECONDS:-5400}"
ACTIVITY_SECONDS="${ACTIVITY_SECONDS:-600}"
COST_USD_CEILING="${COST_USD_CEILING:-5}"

cleanup_tmux() { tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true; }
trap cleanup_tmux EXIT
cleanup_tmux  # kill any leftover before we start

# --- Clean target dir, init git ---------------------------------------
if [[ -d "$PROJECT_DIR/.git" ]]; then
  ( cd "$PROJECT_DIR" \
    && git worktree list --porcelain 2>/dev/null \
       | awk '/^worktree /{print $2}' | grep -v "^$PROJECT_DIR\$" \
       | while read -r wt; do git worktree remove --force "$wt" || true; done \
    && git branch --list 'orch/*' | sed 's/^[*+ ] //' \
       | while read -r br; do [[ -n "$br" ]] && git branch -D "$br" || true; done
  ) || true
fi
rm -rf "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR"

( cd "$PROJECT_DIR" \
  && git init -q -b main \
  && git config user.email "orch@local" \
  && git config user.name "orch" \
  && mkdir -p docs examples/kanban \
  && cp "$REPO_ROOT/examples/kanban/verifier.sh" examples/kanban/verifier.sh \
  && chmod +x examples/kanban/verifier.sh \
  && printf '.orchestra/\nworktrees/\n' > .gitignore \
  && git add examples/kanban/verifier.sh .gitignore \
  && git commit -q -m "seed: kanban verifier + .gitignore" )

( cd "$PROJECT_DIR" && "$REPO_ROOT/.venv/bin/orchestra" init )

# Copy mission + roles into the target project's .orchestra/
mkdir -p "$PROJECT_DIR/.orchestra/briefs" "$PROJECT_DIR/.orchestra/roles"
cp "$REPO_ROOT/examples/kanban/mission.md" "$PROJECT_DIR/.orchestra/briefs/mission.md"
cp "$REPO_ROOT/examples/kanban/.orchestra/roles/"*.md "$PROJECT_DIR/.orchestra/roles/"

# --- Watchdogs ---------------------------------------------------------
DB="$PROJECT_DIR/.orchestra/state.db"
LOGS="$PROJECT_DIR/.orchestra"

_wallclock_watchdog() {
  sleep "$WALLCLOCK_SECONDS"
  echo "WALLCLOCK_TIMEOUT" >&2
  kill -- -$$ 2>/dev/null || true
  exit 124
}

_activity_watchdog() {
  local last
  last=$(date +%s)
  while sleep 30; do
    local now max
    now=$(date +%s)
    max=$(sqlite3 "$DB" "SELECT COALESCE(MAX(id), 0) FROM events" 2>/dev/null || echo 0)
    if [[ -f "$LOGS/last-event-id" ]]; then
      local prev
      prev=$(cat "$LOGS/last-event-id" 2>/dev/null || echo 0)
      if [[ "$max" != "$prev" ]]; then last="$now"; fi
    fi
    echo "$max" > "$LOGS/last-event-id"
    if (( now - last > ACTIVITY_SECONDS )); then
      echo "ACTIVITY_TIMEOUT" >&2
      kill -- -$$ 2>/dev/null || true
      exit 125
    fi
  done
}

_cost_watchdog() {
  # Per-million-token pricing (Anthropic public list 2026-05-18):
  #   Opus 4.x — $15 in / $75 out
  #   Sonnet 4.x — $3 in / $15 out
  #   Haiku 4.x — $1 in / $5 out
  while sleep 60; do
    local usd
    usd=$(python3 - "$DB" <<'PY'
import json, re, sqlite3, sys
RATES = {
  "opus":   {"in":15.0,"out":75.0},
  "sonnet": {"in": 3.0,"out":15.0},
  "haiku":  {"in": 1.0,"out": 5.0},
}
FAM = re.compile(r"(?:^|[-_/])(opus|sonnet|haiku)(?:$|[-\[_/])", re.IGNORECASE)
db=sys.argv[1]; conn=sqlite3.connect(db)
worker_models={wid:(m or "") for wid,m in conn.execute("SELECT id, model FROM workers")}
total=0.0
for wid,payload in conn.execute(
    "SELECT worker_id, payload FROM events WHERE kind='turn_complete'"):
    try: p=json.loads(payload)
    except Exception: p={}
    model=p.get("model") or worker_models.get(wid) or ""
    m=FAM.search(model.lower()); r=RATES["opus"] if not m else RATES[m.group(1).lower()]
    inp=int(p.get("input_tokens",0) or 0)
    out=int(p.get("output_tokens",0) or 0)
    total += (inp/1_000_000.0)*r["in"] + (out/1_000_000.0)*r["out"]
print(f"{total:.4f}")
PY
)
    if awk -v u="$usd" -v c="$COST_USD_CEILING" 'BEGIN{ exit !(u+0 > c+0) }'; then
      echo "COST_CEILING_EXCEEDED usd=$usd ceiling=$COST_USD_CEILING" >&2
      kill -- -$$ 2>/dev/null || true
      exit 126
    fi
  done
}

_wallclock_watchdog &  WALL=$!
_activity_watchdog &   ACT=$!
_cost_watchdog &       COST=$!
trap 'kill "$WALL" "$ACT" "$COST" 2>/dev/null; cleanup_tmux' EXIT

# --- Run the PM ------------------------------------------------------
export PATH="$REPO_ROOT/.venv/bin:$PATH"
RC=0
( cd "$PROJECT_DIR" \
  && orchestra run .orchestra/briefs/mission.md \
       --max-wallclock "$WALLCLOCK_SECONDS" \
       --max-activity "$ACTIVITY_SECONDS" ) || RC=$?

kill "$WALL" "$ACT" "$COST" 2>/dev/null || true

# Final acceptance: the kanban verifier must exit 0 from inside the project.
if [[ $RC -eq 0 ]]; then
  ( cd "$PROJECT_DIR" && bash examples/kanban/verifier.sh ) || RC=$?
fi
exit $RC
