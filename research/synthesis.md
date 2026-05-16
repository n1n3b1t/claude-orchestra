# Synthesis: existing tmux orchestration solutions

Two reference points, very different philosophies.

## Jedward23/Tmux-Orchestrator — prompt-driven

**Code surface:** ~24-line `send-claude-message.sh` (literal send-keys + 0.5s + Enter, no retry), `schedule_with_note.sh` (nohup + sleep + send-keys at future time, hardcoded `/Users/jasonedward/...` paths), `tmux_utils.py` (read-mostly inspection with an interactive `input("yes/no")` safety prompt that breaks automation).

**Where the intelligence lives:** a 716-line `CLAUDE.md` defining roles (Orchestrator → PM → Engineer/QA/DevOps/...), git discipline (30-min commits, feature branches, tags), escalation patterns, and check-in protocols. The actual orchestration "logic" is the LLM following this spec.

**Worth borrowing:**
- The hub-and-spoke model and role taxonomy.
- The communication-pattern insights (specific numbered questions > open-ended "how's it going?").
- Self-scheduling as a primitive — agents can call `schedule_with_note.sh <minutes> "note"` and the bash side wakes them.

**Don't copy:**
- Hardcoded paths, no portability.
- Single send-keys with no verification, no idle check, no ANSI stripping, no multi-line handling.
- No structured state — everything is terminal scraping or LLM memory.

## primeline-ai/claude-tmux-orchestration — infrastructure-driven

**Code surface:** ~700 lines of bash split across `orch-bootstrap.sh`, `heartbeat.sh` (the heart, 353 lines), `spawn-worker.sh`, `rate-limit-watchdog.sh`, plus `config.json` for tunables and a directory of state files.

**Key design decisions:**
- **Send-keys done right.** `-l` literal mode + separate `Enter`, `load-buffer` + `paste-buffer` for multiline, ANSI strip before *any* regex matching. This is the correct pattern.
- **Idle detection.** Spinner check (`Running|thinking|Searching|Reading|Writing|Editing`) overrides idle patterns; checks last 12 lines. Spinner-first is important — it catches busy states the prompt regex would miss.
- **Send verification.** After every send, `capture-pane` and `grep -qF "${message:0:40}"`. Retry up to N times. Means the heartbeat can survive a missed keystroke.
- **File-based coordination.** Workers write `_orchestrator/workers/w1.json` (status, progress, blockers) and can escalate via `_orchestrator/inbox/w1/escalation.json`. No IPC, no daemon, no server — just JSON files. Orchestrator reads them on every cycle.
- **Adaptive heartbeat.** 30s when stuck, 120s normal, 300s idle. Stop signal via `.stop` file, interruptible_sleep, PID file with takeover-on-stale.
- **Rate-limit watchdog as a separate process.** Genuine insight: detects 429s, waits cooldown, then sends an explicit `"this was a TEMPORARY rate limit, NOT a bug, retry the EXACT command, NO workaround"` message. A naive "continue" causes Claude to invent alternatives — this specific phrasing is the load-bearing piece.

**Worker spawn is the choreography we should keep:**

1. `tmux new-window` for the worker.
2. `claude --dangerously-skip-permissions` with `ORCHESTRATOR_WORKER_ID` and `PROJECT_ROOT` in ENV (hooks can use the ID to scope worker behavior).
3. Poll capture-pane until idle prompt appears (max 60s).
4. Double-Enter to dismiss any trust/welcome prompts.
5. Switch model via `/sonnet` / `/haiku` / `/opus`.
6. Inject the structured task prompt via `load-buffer`/`paste-buffer` (multiline-safe).
7. Poll for the worker's first `workers/<id>.json` write (max 90s) — this is the readiness handshake.

**Cracks in this approach:**
- 700 lines of bash gets hard to test and reason about.
- Heartbeat sends a literal `/orchestrate-cycle` slash command to the orchestrator pane — that command must be defined as a custom command in the orchestrator's Claude Code config. An undocumented dependency.
- `--dangerously-skip-permissions` for every worker means no sandbox; the docs say "don't run on prod" but the architecture invites it.
- Workers share the same working directory — they use branches for isolation, not worktrees. Two workers touching the same files = collision.
- The "review gate" is conceptual; the heartbeat doesn't actually enforce anything beyond sending the cycle command. Verification depends on the orchestrator's prompt logic.
- Single-host, single-user; no auth, no encryption, no network transport.

## Pattern intersection — what's load-bearing in both

1. **tmux as the substrate.** Every Claude Code session is a TTY-attached process; tmux makes those panes addressable and inspectable.
2. **send-keys with literal mode + separate Enter** is the only reliable injection pattern.
3. **Capture-pane + ANSI strip** is the only inspection mechanism. Anything based on raw output is brittle.
4. **Idle detection by combining spinner override + prompt regex.** Either alone is wrong.
5. **--dangerously-skip-permissions** is a hard requirement for unattended workers; this is a real trust/security tradeoff with no middle ground at the Claude Code layer.

## Where we have room

| Axis | Jedward23 | primeline | Where we can do better |
|---|---|---|---|
| Reliability of injection | None | Verify + retry | Same; possibly fail-loud on persistent failure |
| Idle detection | None | Spinner + prompt regex | Add a "blocked on permission prompt" state |
| Worker isolation | Shared cwd | Shared cwd, branch-per-worker | git worktree per worker |
| Coordination | LLM memory | JSON files in shared dir | Same JSON files, but add a schema + validator |
| Language | Bash + tiny Python | Bash + jq | Python or Go, easier to test |
| Multi-line prompts | Broken | load-buffer/paste-buffer | Same |
| Rate limiting | Ignored | Smart watchdog | Steal this whole subsystem |
| Self-scheduling | nohup + sleep | Heartbeat-driven | Heartbeat-driven is more honest |
| Observability | None | log.jsonl + workers/*.json | Add a TUI / web view that reads these |
| Permission prompts | Ignored | Bypassed via skip-perms | Add a per-tool allowlist / deny mode |

## Recommendation (one paragraph)

primeline's architecture is the right baseline — file-based coordination, verify-on-send, adaptive heartbeat, rate-limit watchdog, structured worker spawn. Its weaknesses are language (700 lines of bash is at the edge of maintainable) and isolation (shared cwd). Our value-add: rewrite in Python (or Go) for testability, add worktree-per-worker for real isolation, ship a small status TUI so the orchestration is observable without `tmux attach`, and add a deny-list mode for workers that don't need full skip-perms. Keep primeline's rate-limit message verbatim — that exact phrasing is the trick.
