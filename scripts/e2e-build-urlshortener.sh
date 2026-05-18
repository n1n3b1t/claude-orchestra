#!/usr/bin/env bash
# scripts/e2e-build-urlshortener.sh — claude-orchestra v1 acceptance test.
#
# Spawns a PM + two engineers and waits for them to autonomously build a
# URL shortener web app. Three watchdogs (wall-clock, activity, cost)
# bound the run. Exits 0 only if the PM marks itself done AND the verifier
# passes.
#
# Requires: claude CLI authenticated, tmux, orchestra installed.
# Consumes API credits. NOT in CI.
#
# Dependencies:
#   - sqlite3  (for state.db queries; e.g. apt install sqlite3)
#   - python3  (for token-cost summation; stdlib only)
#   - bc       (for floating-point budget comparison)
#   - jq       (for JSON parsing)
#   - tmux, claude, orchestra (core requirements)

set -euo pipefail

WALL_CLOCK_SECS="${WALL_CLOCK_SECS:-5400}"        # 90 min
ACTIVITY_TIMEOUT_SECS="${ACTIVITY_TIMEOUT_SECS:-600}"  # 10 min
MAX_BUDGET_USD="${MAX_BUDGET_USD:-10.00}"
PROJECT_DIR="${PROJECT_DIR:-/tmp/orch-urlshortener}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

for bin in claude orchestra tmux jq python3 sqlite3 bc; do
  command -v "$bin" >/dev/null 2>&1 || { echo "FAIL: $bin not in PATH" >&2; exit 2; }
done

# --- Cleanup any leftovers from a prior run -----------------------------
# Tmux session name MUST match orchestra.cli._session_name_for so we kill
# the right one. The script may otherwise re-attach to a leaked session
# and `orchestra spawn` will collide on the existing 'pm' window.
SESSION="orch-$(basename "$PROJECT_DIR" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+|-+$//g')"
tmux kill-session -t "$SESSION" 2>/dev/null || true
# Also kill the session on script exit so a failed run leaves nothing behind.
trap 'tmux kill-session -t "$SESSION" 2>/dev/null || true' EXIT
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

# --- Initialise the target repo ----------------------------------------
( cd "$PROJECT_DIR" \
  && git init -q -b main \
  && git config user.email "orch@local" \
  && git config user.name "orch" \
  && cp "$REPO_ROOT/examples/urlshortener-verifier.sh" verifier.sh \
  && chmod +x verifier.sh \
  && git add verifier.sh \
  && git commit -q -m "seed: verifier" )

( cd "$PROJECT_DIR" && orchestra init )

# --- Watchdogs --------------------------------------------------------
DB="$PROJECT_DIR/.orchestra/state.db"
WATCHDOG_LOG="$PROJECT_DIR/.orchestra/watchdog.log"
RESULT_FILE="$PROJECT_DIR/.orchestra/e2e-result"

_query() { sqlite3 "$DB" "$@"; }

_token_cost_usd() {
  # Per-million-token pricing for Anthropic public list (as of 2026-05-18):
  #   Opus 4.x   — $15 in / $75 out
  #   Sonnet 4.x — $3 in / $15 out
  #   Haiku 4.x  — $1 in / $5 out
  # Reads token counts from turn_complete payloads; picks a family by
  # matching the model id with a regex that handles both short aliases
  # ("opus", "sonnet", "haiku") and full IDs ("claude-opus-4-7",
  # "claude-opus-4-7[1m]", "claude-sonnet-4-6",
  # "claude-haiku-4-5-20251001"). Unknown → Opus (conservative).
  python3 - "$DB" <<'PY'
import json, re, sqlite3, sys

RATES = {
    "opus":   {"in": 15.00, "out": 75.00},
    "sonnet": {"in":  3.00, "out": 15.00},
    "haiku":  {"in":  1.00, "out":  5.00},
}
FAMILY_RE = re.compile(r"(?:^|[-_/])(opus|sonnet|haiku)(?:$|[-\[_/])", re.IGNORECASE)


def rate_for(model_id: str | None) -> dict[str, float]:
    """Pick a price tier from a model identifier. Conservative on miss."""
    if model_id:
        m = FAMILY_RE.search(model_id.lower())
        if m:
            return RATES[m.group(1).lower()]
    return RATES["opus"]  # unknown → over-bill rather than under-bill


db = sys.argv[1]
conn = sqlite3.connect(db)
worker_models = {
    wid: (m or "") for (wid, m) in conn.execute("SELECT id, model FROM workers")
}
total = 0.0
for (worker_id, payload) in conn.execute(
    "SELECT worker_id, payload FROM events WHERE kind = 'turn_complete'"
):
    try:
        p = json.loads(payload) if payload else {}
    except Exception:
        continue
    inp = int(p.get("input_tokens") or 0)
    out = int(p.get("output_tokens") or 0)
    # Prefer the per-turn model (recorded in the payload when available) over
    # the worker's spawn-time alias; some runs switch models mid-conversation.
    model_id = p.get("model") or worker_models.get(worker_id, "")
    rate = rate_for(model_id)
    total += inp / 1_000_000 * rate["in"]
    total += out / 1_000_000 * rate["out"]
print(f"{total:.4f}")
PY
}

run_watchdog() {
  local start=$(date +%s)
  local last_max_id=0
  local last_event_seen=$(date +%s)
  while sleep 30; do
    local now=$(date +%s)
    local elapsed=$(( now - start ))
    # Wall-clock
    if (( elapsed > WALL_CLOCK_SECS )); then
      echo "WATCHDOG: wall-clock $WALL_CLOCK_SECS elapsed" | tee -a "$WATCHDOG_LOG"
      echo "wall_clock" > "$RESULT_FILE"; return 124
    fi
    # Activity
    local current_max
    current_max=$(_query "SELECT COALESCE(MAX(id),0) FROM events" 2>/dev/null || echo 0)
    if (( current_max > last_max_id )); then
      last_max_id=$current_max; last_event_seen=$now
    elif (( now - last_event_seen > ACTIVITY_TIMEOUT_SECS )); then
      echo "WATCHDOG: no events for ${ACTIVITY_TIMEOUT_SECS}s" | tee -a "$WATCHDOG_LOG"
      echo "activity" > "$RESULT_FILE"; return 125
    fi
    # Cost
    local cost
    cost=$(_token_cost_usd)
    if (( $(echo "$cost > $MAX_BUDGET_USD" | bc -l) )); then
      echo "WATCHDOG: cost \$$cost > \$$MAX_BUDGET_USD" | tee -a "$WATCHDOG_LOG"
      echo "cost" > "$RESULT_FILE"; return 126
    fi
    # PM done?
    local pm_status
    pm_status=$(_query "SELECT status FROM workers WHERE id='pm'" 2>/dev/null || echo "")
    if [[ "$pm_status" == "done" ]]; then
      echo "WATCHDOG: PM reports done" | tee -a "$WATCHDOG_LOG"
      echo "pm_done" > "$RESULT_FILE"; return 0
    fi
  done
}

run_watchdog &
WATCHDOG_PID=$!

# --- Kick off the PM --------------------------------------------------
cd "$PROJECT_DIR"
orchestra spawn pm opus "$(cat "$REPO_ROOT/examples/urlshortener-mission.md")" \
  --role pm \
  --brief "$REPO_ROOT/examples/urlshortener-mission.md"

# --- Wait for watchdog to decide --------------------------------------
wait "$WATCHDOG_PID" || true
RESULT=$(cat "$RESULT_FILE" 2>/dev/null || echo "unknown")

# --- Final summary ----------------------------------------------------
echo
echo "==================== e2e summary ===================="
echo "Project dir: $PROJECT_DIR"
echo "Watchdog result: $RESULT"
echo "Final cost: \$$(_token_cost_usd)"
echo "Worker final states:"
_query "SELECT id, role, status, turns FROM workers" \
  | column -ts'|' -N 'id,role,status,turns' || true
echo "Recent events (last 20):"
_query ".headers on" "SELECT id, worker_id, ts, kind FROM events ORDER BY id DESC LIMIT 20" \
  | column -ts'|' || true

# --- Final acceptance gate -------------------------------------------
if [[ "$RESULT" == "pm_done" ]]; then
  if ( cd "$PROJECT_DIR" && bash verifier.sh ); then
    echo "[e2e] PASS"
    exit 0
  else
    echo "[e2e] FAIL: PM reported done but verifier failed"
    exit 10
  fi
else
  echo "[e2e] FAIL: watchdog tripped ($RESULT)"
  case "$RESULT" in
    wall_clock) exit 124 ;;
    activity)   exit 125 ;;
    cost)       exit 126 ;;
    *)          exit 1 ;;
  esac
fi
