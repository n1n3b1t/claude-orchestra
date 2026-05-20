"""Deep-merge orchestra's Claude Code hooks into .claude/settings.local.json.

We own only the entries whose `command` contains `orchestra worker hook`.
Any other hooks the user added are preserved untouched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOOK_MARKER = "orchestra worker hook"

EVENTS_NO_MATCHER = ("SessionStart", "Stop", "SessionEnd", "Notification")
EVENTS_WITH_MATCHER = ("PreToolUse", "PostToolUse")


def _our_entry(event: str) -> dict[str, Any]:
    inner = {"type": "command", "command": f"{HOOK_MARKER} {event}"}
    if event in EVENTS_WITH_MATCHER:
        return {"matcher": ".*", "hooks": [inner]}
    return {"hooks": [inner]}


def _entry_is_ours(entry: dict[str, Any]) -> bool:
    return any(HOOK_MARKER in (h.get("command") or "") for h in entry.get("hooks", []))


def _merge_event(existing: list[Any], event: str) -> list[Any]:
    ours = _our_entry(event)
    keep: list[Any] = []
    for e in existing or []:
        if isinstance(e, dict) and _entry_is_ours(e):
            continue  # drop any stale orchestra entry; we'll re-add a fresh one
        keep.append(e)
    keep.append(ours)
    return keep


def ensure_hooks(path: Path) -> None:
    """Merge canonical orchestra hooks into `path`. Creates the file if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data: Any
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = None
    else:
        data = None
    if not isinstance(data, dict):
        data = {}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    for event in EVENTS_NO_MATCHER + EVENTS_WITH_MATCHER:
        hooks[event] = _merge_event(hooks.get(event) or [], event)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _merge_string_list(existing: list[Any] | None, additions: list[Any]) -> list[str]:
    """Append additions to existing, keeping only strings, dedupe, preserve order."""
    out: list[str] = []
    for src in (existing or []), additions:
        for item in src:
            if isinstance(item, str) and item not in out:
                out.append(item)
    return out


def ensure_perms(path: Path, perms: dict[str, Any]) -> None:
    """Merge a permissions block into `path`. Creates the file if missing.

    `perms` shape: {"allow": [...], "deny": [...]} (either key optional).
    Entries that aren't strings are dropped silently — Claude Code is the
    schema authority and orchestra just plumbs the merge.
    """
    allow = list(perms.get("allow") or [])
    deny = list(perms.get("deny") or [])
    if not allow and not deny:
        return  # nothing to write

    path.parent.mkdir(parents=True, exist_ok=True)
    data: Any
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = None
    else:
        data = None
    if not isinstance(data, dict):
        data = {}

    perms_block = data.get("permissions")
    if not isinstance(perms_block, dict):
        perms_block = {}
        data["permissions"] = perms_block

    perms_block["allow"] = _merge_string_list(perms_block.get("allow"), allow)
    perms_block["deny"] = _merge_string_list(perms_block.get("deny"), deny)

    path.write_text(json.dumps(data, indent=2) + "\n")
