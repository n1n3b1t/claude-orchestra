# Claude Code hook payload schemas

These are the JSON shapes Claude Code writes to a hook command's stdin for
each of the six hook events that `orchestra worker hook <EVENT>` consumes
(see `orchestra/hooks.py`). They are the source of truth for the typed
dispatch in `_handle()`.

Payloads marked **observed** were captured live by running a real Claude
Code session against the spike-mode handler and inspecting
`.orchestra/hook-debug.log` (capture date 2026-05-18, model
`claude-opus-4-7[1m]`). Payloads marked **inferred** weren't directly
observed during the capture window; the field list is reconstructed from
Claude Code docs as of 2026-05-18 plus the keys `orchestra/hooks.py`
actually reads — confirm against a fresh capture before relying on them.

All payloads share a common prelude (present on every event):

```
session_id:      str   — Claude Code session UUID
transcript_path: str   — absolute path to per-session JSONL transcript
cwd:             str   — Claude's project root at hook fire time
hook_event_name: str   — duplicates the event name
permission_mode: str   — e.g. "bypassPermissions" (PreToolUse / PostToolUse)
```

The `transcript_path` is the **only** place per-turn token usage lives —
`Stop`'s payload itself does not include `usage` keys (see issue #8). Each
line in the JSONL is one turn message with a `message.usage` object:

```
{"input_tokens": int,
 "output_tokens": int,
 "cache_read_input_tokens": int,
 "cache_creation_input_tokens": int,
 ...}
```

The cost watchdog in `scripts/e2e-build-urlshortener.sh` is the current
consumer of the transcript (planned — issue #8); `orchestra/hooks.py`
itself does not yet read the JSONL.

## How to reproduce

```bash
mkdir -p /tmp/hook-capture && cd /tmp/hook-capture
orchestra init
mkdir -p .claude
cat > .claude/settings.local.json <<'EOF'
{
  "hooks": {
    "SessionStart":  [{"hooks": [{"type": "command", "command": "orchestra worker hook SessionStart"}]}],
    "Stop":          [{"hooks": [{"type": "command", "command": "orchestra worker hook Stop"}]}],
    "PreToolUse":    [{"matcher": ".*", "hooks": [{"type": "command", "command": "orchestra worker hook PreToolUse"}]}],
    "PostToolUse":   [{"matcher": ".*", "hooks": [{"type": "command", "command": "orchestra worker hook PostToolUse"}]}],
    "SessionEnd":    [{"hooks": [{"type": "command", "command": "orchestra worker hook SessionEnd"}]}],
    "Notification":  [{"hooks": [{"type": "command", "command": "orchestra worker hook Notification"}]}]
  }
}
EOF
ORCHESTRA_WORKER_ID=spike ORCHESTRA_STATE_DB=$PWD/.orchestra/state.db \
  claude --dangerously-skip-permissions
# inside claude: ask it to write a file, run a bash command, then exit
tail -n 200 .orchestra/hook-debug.log
```

---

## SessionStart

**Status:** observed.

Fired when Claude Code's session is initialised, **after** the trust
prompt is dismissed (so trust-prompt UI is invisible to this hook —
`orchestra/spawn.py` runs trust-prompt dismissal via pane capture in
parallel with the `session_ready` event wait).

### Fields

| Field             | Type | Notes                                           |
|-------------------|------|-------------------------------------------------|
| `session_id`      | str  | Claude Code session UUID                        |
| `transcript_path` | str  | Absolute path to the per-session JSONL          |
| `cwd`             | str  | Claude's project root                           |
| `hook_event_name` | str  | Always `"SessionStart"`                         |
| `source`          | str  | `"startup"` on fresh boot; `"resume"` on resume |
| `model`           | str  | Effective model id, e.g. `claude-opus-4-7[1m]`  |

### Example

```json
{
  "session_id": "3d8eaad9-9ece-4f32-9fbb-face3d85ad28",
  "transcript_path": "<cwd>/.../<session_id>.jsonl",
  "cwd": "<cwd>",
  "hook_event_name": "SessionStart",
  "source": "startup",
  "model": "claude-opus-4-7[1m]"
}
```

### Consumed by

`orchestra/hooks.py:_handle()` — reads `payload.session_id` and records a
`session_ready` event, setting `worker.status = "working"`. Has special
preserved-`done` logic: if the worker already cooperatively completed
(`status == "done"`), the SessionStart re-entry is intercepted and
recorded as `done_to_working_blocked` instead of clobbering the final
status (issue #2 / fix in #14).

---

## Stop

**Status:** inferred (not captured in the 2026-05-18 spike). Field list
based on Claude Code docs and the fields `_extract_token_usage` checks
for. The most important nuance is documented in issue #8: **the Stop
payload itself does not carry `usage` data**; per-turn token counts
live in the JSONL pointed to by `transcript_path`.

### Fields

| Field                | Type   | Notes                                              |
|----------------------|--------|----------------------------------------------------|
| `session_id`         | str    |                                                    |
| `transcript_path`    | str    | Read this file to count tokens                     |
| `cwd`                | str    |                                                    |
| `hook_event_name`    | str    | Always `"Stop"`                                    |
| `stop_hook_active`   | bool   | True if `stopHook: continue: false` was previously returned (re-entry guard) |
| `usage` *(optional)* | object | Some past versions exposed usage here directly — `_extract_token_usage` tolerates both shapes; in current builds this is absent and tokens must be read from the transcript |

### Transcript-line shape (`transcript_path` JSONL)

Each line is one turn record. The fields the cost watchdog cares about:

```
{
  "type": "assistant" | "user" | "system",
  "message": {
    "model": "claude-opus-4-7[1m]",
    "usage": {
      "input_tokens": int,
      "output_tokens": int,
      "cache_read_input_tokens": int,
      "cache_creation_input_tokens": int
    }
  },
  ...
}
```

### Example (Stop payload itself — inferred)

```json
{
  "session_id": "<session_id>",
  "transcript_path": "<cwd>/.../<session_id>.jsonl",
  "cwd": "<cwd>",
  "hook_event_name": "Stop",
  "stop_hook_active": false
}
```

### Consumed by

`orchestra/hooks.py:_handle()` — increments `worker.turns` and records a
`turn_complete` event. `_extract_token_usage()` is currently best-effort:
if a `usage` key is present it extracts it, otherwise records zeros.
Reading the transcript JSONL to populate real token counts is tracked in
issue #8.

---

## PreToolUse

**Status:** observed.

Fired once per tool invocation, **before** the tool runs. Can return
`{"continue": false, "stopReason": "..."}` to abort the call — orchestra
does not exercise that path; it only logs.

### Fields

| Field             | Type   | Notes                                              |
|-------------------|--------|----------------------------------------------------|
| `session_id`      | str    |                                                    |
| `transcript_path` | str    |                                                    |
| `cwd`             | str    |                                                    |
| `permission_mode` | str    | e.g. `"bypassPermissions"`                         |
| `effort`          | object | `{"level": "xhigh"}` — reasoning-effort selector   |
| `hook_event_name` | str    | Always `"PreToolUse"`                              |
| `tool_name`       | str    | e.g. `"Bash"`, `"Edit"`, `"Read"`                  |
| `tool_input`      | object | Tool-specific (e.g. `{"command": "...", "description": "..."}` for Bash) |
| `tool_use_id`     | str    | Pairs with the matching `PostToolUse`              |

### Example

```json
{
  "session_id": "<session_id>",
  "transcript_path": "<cwd>/.../<session_id>.jsonl",
  "cwd": "<cwd>",
  "permission_mode": "bypassPermissions",
  "effort": {"level": "xhigh"},
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {
    "command": "ls .orchestra/ && git status --short",
    "description": "Check working directory state"
  },
  "tool_use_id": "toolu_01Li8oqxX2NmPNnzZngpfmyX"
}
```

### Consumed by

`orchestra/hooks.py:_handle()` — records a `tool_started` event with
`tool=payload.tool_name` and `input_summary=str(payload.tool_input)[:200]`.
The full payload is also appended to `hook-debug.log` as a temporary
diagnostic.

---

## PostToolUse

**Status:** observed.

Fired once per tool invocation, **after** the tool returns. `tool_use_id`
matches the preceding `PreToolUse`.

### Fields

Same prelude as PreToolUse, plus:

| Field             | Type   | Notes                                                   |
|-------------------|--------|---------------------------------------------------------|
| `tool_name`       | str    |                                                         |
| `tool_input`      | object | Same shape as in PreToolUse                             |
| `tool_response`   | object | Tool-specific result. For Bash: `{"stdout": str, "stderr": str, "interrupted": bool, "isImage": bool, "noOutputExpected": bool}` |
| `tool_use_id`     | str    | Pairs with the matching `PreToolUse`                    |
| `duration_ms`     | int    | Wall-clock duration of the tool call                    |

`orchestra/hooks.py` reads `tool_output`, not `tool_response` — a known
mismatch noted in the source. The captured payloads in
`hook-debug.log` use `tool_response`; the `output_summary` field on the
event row therefore comes out empty/`""` for Bash today. Tracked
implicitly under the broader hook-schema follow-ups.

### Example (Bash)

```json
{
  "session_id": "<session_id>",
  "transcript_path": "<cwd>/.../<session_id>.jsonl",
  "cwd": "<cwd>",
  "permission_mode": "bypassPermissions",
  "effort": {"level": "xhigh"},
  "hook_event_name": "PostToolUse",
  "tool_name": "Bash",
  "tool_input": {
    "command": "ls .orchestra/",
    "description": "Check working directory state"
  },
  "tool_response": {
    "stdout": "briefs\nconfig.toml\nhook-debug.log\nstate.db\n",
    "stderr": "",
    "interrupted": false,
    "isImage": false,
    "noOutputExpected": false
  },
  "tool_use_id": "toolu_01Li8oqxX2NmPNnzZngpfmyX",
  "duration_ms": 62
}
```

### Consumed by

`orchestra/hooks.py:_handle()` — records a `tool_finished` event with
`tool=payload.tool_name` and `output_summary=str(payload.tool_output)[:200]`
(see the `tool_response` vs `tool_output` mismatch above).

---

## SessionEnd

**Status:** inferred (not captured in the 2026-05-18 spike).

Fired when the Claude Code session exits — either cleanly (user `/exit`,
Ctrl-D) or via parent-process termination.

### Fields

| Field             | Type | Notes                                              |
|-------------------|------|----------------------------------------------------|
| `session_id`      | str  |                                                    |
| `transcript_path` | str  |                                                    |
| `cwd`             | str  |                                                    |
| `hook_event_name` | str  | Always `"SessionEnd"`                              |
| `reason`          | str  | `"exit"`, `"clear"`, `"logout"`, `"prompt_input_exit"`, etc. — inferred from Claude Code docs |

### Example (inferred)

```json
{
  "session_id": "<session_id>",
  "transcript_path": "<cwd>/.../<session_id>.jsonl",
  "cwd": "<cwd>",
  "hook_event_name": "SessionEnd",
  "reason": "exit"
}
```

### Consumed by

`orchestra/hooks.py:_handle()` — reads `payload.reason` and records a
`session_ended` event. Sets `worker.status = "done"` **unless** the
status is already terminal (`"error"`, `"stopped"`, `"stop_send_failed"`,
or already `"done"`) — this is the SessionEnd half of the preserved-done
fix (issue #2 / #14).

---

## Notification

**Status:** inferred (not captured in the 2026-05-18 spike).

Fired when Claude Code emits a permission prompt or other user-facing
notice (e.g. "waiting for your input"). Claude Code's notification system
is sparsely documented; this is the field list `orchestra/hooks.py`
reads, not necessarily exhaustive.

### Fields

| Field             | Type | Notes                                              |
|-------------------|------|----------------------------------------------------|
| `session_id`      | str  |                                                    |
| `transcript_path` | str  |                                                    |
| `cwd`             | str  |                                                    |
| `hook_event_name` | str  | Always `"Notification"`                            |
| `message`         | str  | Human-readable notification text                   |

### Example (inferred)

```json
{
  "session_id": "<session_id>",
  "transcript_path": "<cwd>/.../<session_id>.jsonl",
  "cwd": "<cwd>",
  "hook_event_name": "Notification",
  "message": "Claude needs your permission to use Bash"
}
```

### Consumed by

`orchestra/hooks.py:_handle()` — records a `notification` event with
`message=payload.message`. No state mutation; PM polling treats
`notification` as an "interesting" kind so the orchestrator notices
permission stalls.
