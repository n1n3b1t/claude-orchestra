# Mission: URL shortener web app

Build a small FastAPI web app that shortens URLs.

## Acceptance
- `pytest` passes from the project root.
- `uvicorn app:app --port 8765` starts the server.
- `curl -X POST localhost:8765/shorten -H 'content-type: application/json' -d '{"url":"https://example.com"}'`
  returns HTTP 200 with a JSON body `{"code":"<short>"}`.
- `curl -I localhost:8765/<short>` returns HTTP 302 with `Location: https://example.com`.
- `curl localhost:8765/` returns an HTML page with a form posting to `/shorten`.

## Team
Spawn two engineers in their own worktrees:
- `backend` (sonnet) — implements the FastAPI app, SQLite storage, and tests.
- `frontend` (sonnet) — implements the HTML form page and any static assets.

You mediate the API contract. You merge their work into main. You run the
acceptance checks. You only mark yourself done when all four acceptance
checks pass.

The verifier script is at `./verifier.sh`.
