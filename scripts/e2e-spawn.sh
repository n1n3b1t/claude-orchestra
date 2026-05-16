#!/usr/bin/env bash
# scripts/e2e-spawn.sh — opt-in end-to-end smoke test for claude-orchestra v0.
#
# Requires: claude CLI authenticated, tmux installed, orchestra installed
# (pip install -e .). Consumes API credits.
#
# Exits 0 on success.

set -euo pipefail

if ! command -v claude >/dev/null 2>&1; then
  echo "FAIL: claude CLI not in PATH" >&2; exit 2
fi
if ! command -v orchestra >/dev/null 2>&1; then
  echo "FAIL: orchestra CLI not in PATH (pip install -e .)" >&2; exit 2
fi

TMPDIR_E2E=$(mktemp -d)
# Sanitise to [a-z0-9-] so tmux's session.window target syntax stays unambiguous.
# Must match orchestra.cli._session_name_for exactly.
BASE=$(basename "$TMPDIR_E2E" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+|-+$//g')
SESSION="orch-${BASE:-default}"
trap 'tmux kill-session -t "$SESSION" 2>/dev/null || true; rm -rf "$TMPDIR_E2E"' EXIT

cd "$TMPDIR_E2E"
echo "[e2e] tmpdir: $TMPDIR_E2E  session: $SESSION"

orchestra init
echo "[e2e] init done"

TASK='Write the literal text OK to ./hello.txt then run: orchestra worker status --progress "done" --turns 1'
orchestra spawn w1 sonnet "$TASK"
echo "[e2e] spawn returned"

DEADLINE=$(( $(date +%s) + 120 ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
  if orchestra status --worker w1 2>/dev/null | grep -q "turns=1"; then
    echo "[e2e] worker hit turns=1 — pass"
    if [[ -f "$TMPDIR_E2E/hello.txt" ]]; then
      echo "[e2e] hello.txt present:"
      cat "$TMPDIR_E2E/hello.txt"
    fi
    exit 0
  fi
  sleep 5
done

echo "[e2e] FAIL: worker did not reach turns=1 within 120s"
orchestra status --worker w1 || true
exit 1
