# claude-orchestra

Experiments in driving Claude Code instances from outside the process — starting
with tmux send-keys, since every Claude Code session is just a TTY-attached
program running in a pane.

New to orchestra? Start with [GETTING_STARTED.md](GETTING_STARTED.md).

## Findings so far

### Yes, you can control Claude via tmux send-keys.

- `tmux send-keys -t <pane> -l "prompt"` then `Enter` types the prompt into the
  target Claude's input box and submits it.
- `tmux capture-pane -p -t <pane>` returns the visible screen. Use `-S -<n>` to
  pull `<n>` lines of scrollback when a reply spans more than a screen.
- Verified live against a real Claude Code pane: round-trip works.

### Caveats (these are the load-bearing ones)

- **Keystroke injection, not an API.** There is no structured reply, no ack, no
  turn boundary. You poll the pane and parse text. Anything that needs strict
  request/response correlation is on you.
- **Permission prompts block forever** unless the target was launched with
  `--dangerously-skip-permissions` or you script the `1`+`Enter` approval. The
  latter is brittle — only do it if you fully trust the prompts.
- **Race conditions.** If you send before Claude is at its input prompt, your
  keystrokes land somewhere unintended (e.g. the bash prompt if Claude exited).
  Pre-flight check `pane_current_command` before injecting.
- **Idle detection is fuzzy.** "Output stable for N polls" works in practice but
  also fires on frozen permission prompts. Pair it with a regex on the input
  box border if you need to distinguish "done" from "stuck."
- **No isolation.** The target shares your filesystem and credentials. This is
  not a sandbox boundary.

### Bug we hit

Early version of `cmd_wait` used `(( elapsed < MAX_WAIT ))` with a float
`POLL_INTERVAL` of 1.5s. Bash arithmetic is integer-only — the comparison
errors and the wait loop falls through silently. Fixed by converting the
budget to an integer poll count up front. The real damage wasn't the bash
error: while wait was broken, the pane state drifted (Claude was Ctrl-C'd and
restarted manually) and the round-trip only completed because of manual
intervention. Brittle polling logic isn't just a nuisance — it can leave the
target in an unexpected state.

## One-shot runner

To kick off a full PM-coordinated run from a mission file and block until
the PM signals done (or a watchdog fires):

```
orchestra mission new my-mission         # scaffolds missions/my-mission/
$EDITOR missions/my-mission/mission.md
orchestra mission run my-mission         # blocks until done or watchdog fires
```

Each run creates a row in `state.db.missions` so you can browse history with
`orchestra mission list` and inspect a specific mission with
`orchestra mission show <slug>`.

The legacy form `orchestra run <path-to-mission.md>` still works for any
mission file (it auto-creates a mission row with a timestamp-derived slug when
the path is not under `missions/<slug>/`), but new projects should use the
`mission` subcommands.

Place an executable `.orchestra/pre-run.sh` in your project to run setup steps
before the PM spawns — for example, `adb connect <ip>` for on-device testing
missions. A non-zero exit aborts the run before any API credits are spent.

## Defining custom roles

Role templates are markdown files. Lookup order:

1. `<your-project>/.orchestra/roles/<name>.md` (your override, wins)
2. `orchestra/roles/<name>.md` (bundled built-in)

A role file is a `str.format_map`-formatted prompt body with optional YAML
front matter that may declare `permissions.allow` / `permissions.deny`
(Claude Code tool patterns). Minimal example for a read-only reviewer:

```markdown
---
permissions:
  allow:
    - Read
    - Grep
    - Glob
    - "Bash(git log:*)"
    - "Bash(git diff:*)"
  deny:
    - Write
    - Edit
    - "Bash(rm:*)"
---
## ROLE: Reviewer
Worker ID: {worker_id}
Workspace: {cwd}

You are a read-only reviewer. Read the merged main branch and escalate
findings via `orchestra worker escalate --blocking --question "..."`.
```

Spawn with: `orchestra spawn rev sonnet "" --role reviewer` (no
`--worktree` — the reviewer reads main).

See `missions/kanban/.orchestra/roles/` for a full multi-role example (architect / backend / web / cli / reviewer). The example uses the same `.orchestra/roles/` convention as a real project, so it doubles as a template for your own.

## Layout

```
bin/
  claude-tmux-driver.sh   # send / read / wait / ask subcommands
missions/
  urlshortener/           # URL-shortener mission + verifier (v1 acceptance test)
  kanban/                 # Kanban app mission + multi-role example
```

`~/bin/claude-tmux-driver.sh` is a symlink to the script in this repo, so it
stays in PATH.

## Open follow-ups

- Pre-flight: refuse to `send` unless `pane_current_command` is `claude` (or
  whatever the caller declares).
- Idle detector: combine output-stability with a regex match on the input box
  border to distinguish "done" from "blocked on permission prompt."
- Multi-line input: Claude binds bare Enter to submit. Use `Escape Enter`
  between lines, or `tmux load-buffer` + `paste-buffer` for large bodies.
- Python rewrite with async polling and explicit turn objects, if this grows
  beyond toy use.
- Auto-launch targets: spawn `tmux new-window` + `claude --dangerously-skip-permissions`
  and return the pane id, so orchestration code doesn't need to know about
  existing layout.
