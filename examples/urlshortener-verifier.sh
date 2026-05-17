#!/usr/bin/env bash
# Verifier for the URL-shortener mission. Run from project root.
set -e

PROJECT_ROOT="${PROJECT_ROOT:-$PWD}"
PORT="${PORT:-8765}"

( cd "$PROJECT_ROOT" && pytest -q ) || { echo "VERIFIER: pytest failed"; exit 1; }

( cd "$PROJECT_ROOT" && uvicorn app:app --port "$PORT" ) &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null || true" EXIT
sleep 2

CODE=$(curl -fs -X POST "localhost:$PORT/shorten" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])') || {
    echo "VERIFIER: POST /shorten failed"; exit 2; }
test -n "$CODE" || { echo "VERIFIER: empty code"; exit 2; }

curl -fsI "localhost:$PORT/$CODE" | grep -q '^location: https://example.com' \
  || { echo "VERIFIER: GET /<code> did not 302 to example.com"; exit 3; }

curl -fs "localhost:$PORT/" | grep -q '<form' \
  || { echo "VERIFIER: GET / had no <form>"; exit 4; }

echo "VERIFIER OK code=$CODE"
