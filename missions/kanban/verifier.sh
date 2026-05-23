#!/usr/bin/env bash
# Acceptance verifier for the kanban e2e. Assumes the backend is running
# on http://localhost:8765 (started by the PM before invoking this).
set -euo pipefail

BASE="${KANBAN_URL:-http://localhost:8765}"
fail() { echo "FAIL: $1" >&2; exit 1; }

echo "1/6 GET $BASE/api/health"
curl -fsS "$BASE/api/health" >/dev/null || fail "/api/health not 200"

echo "2/6 POST $BASE/api/boards"
BOARD_JSON=$(curl -fsS -X POST -H 'content-type: application/json' \
  -d '{"name":"smoke"}' "$BASE/api/boards") || fail "create board"
BOARD_ID=$(printf '%s' "$BOARD_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["id"])') \
  || fail "board id"

echo "3/6 POST $BASE/api/boards/$BOARD_ID/cards"
CARD_JSON=$(curl -fsS -X POST -H 'content-type: application/json' \
  -d '{"title":"sanity","column":"todo"}' \
  "$BASE/api/boards/$BOARD_ID/cards") || fail "create card"
CARD_ID=$(printf '%s' "$CARD_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["id"])') || fail "card id"

echo "4/6 PATCH $BASE/api/cards/$CARD_ID column=done"
curl -fsS -X PATCH -H 'content-type: application/json' \
  -d '{"column":"done"}' "$BASE/api/cards/$CARD_ID" >/dev/null \
  || fail "patch card"

echo "5/6 GET $BASE/  (web client)"
HTML=$(curl -fsS "$BASE/") || fail "web /"
printf '%s' "$HTML" | grep -qi 'kanban' || fail "html lacks 'kanban'"
printf '%s' "$HTML" | grep -qi 'todo' || fail "html lacks 'todo'"

echo "6/6 python cli/kanban_cli.py list"
KANBAN_URL="$BASE" python3 cli/kanban_cli.py list | grep -q 'sanity' \
  || fail "cli list output missing 'sanity'"

echo "OK"
