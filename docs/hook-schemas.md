# Claude Code hook payload schemas — captured 2026-05-17

These are the actual JSON shapes Claude Code sends on stdin for each hook
event. Captured by running a real `claude` session against the spike-mode
`orchestra worker hook` handler. Source of truth for `orchestra/hooks.py`
typed dispatch.

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

## Captured payloads

(paste one JSON example per event kind here after running the manual capture above)

### SessionStart
```json
<paste>
```

### Stop
```json
<paste — note especially the token-count fields>
```

### PreToolUse / PostToolUse / SessionEnd / Notification
```json
<paste>
```
