"""orchestra mission lint — static pre-flight check for mission files."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestra.role_prompts import RoleNotFoundError, _load_role


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning"
    message: str
    line: int | None = None


def _extract_jsonl_blocks(text: str) -> list[tuple[int, str]]:
    """Return [(start_line, block_text)] for each fenced ```jsonl ... ``` block."""
    out: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```jsonl"):
            start = i + 2  # line after the fence (1-indexed)
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            out.append((start, "\n".join(lines[i + 1 : j])))
            i = j + 1
        else:
            i += 1
    return out


def _parse_specs(block: str, start_line: int) -> list[tuple[int, dict[str, Any]]]:
    specs: list[tuple[int, dict[str, Any]]] = []
    for idx, raw in enumerate(block.splitlines()):
        line_no = start_line + idx
        line = raw.strip()
        if not line:
            continue
        try:
            specs.append((line_no, json.loads(line)))
        except json.JSONDecodeError as e:
            raise ValueError(f"line {line_no}: invalid JSON: {e}") from None
    return specs


def lint(
    mission_path: Path, project_root: Path | None = None, strict: bool = False
) -> list[Finding]:
    """Run all checks; return findings."""
    if project_root is None:
        project_root = mission_path.parent.parent  # mission lives under <root>/.orchestra/briefs/

    findings: list[Finding] = []
    text = mission_path.read_text()
    lines = text.splitlines()

    seen_worktrees: set[str] = set()
    for start_line, block in _extract_jsonl_blocks(text):
        try:
            specs = _parse_specs(block, start_line)
        except ValueError as e:
            findings.append(Finding("error", str(e)))
            continue
        for line_no, spec in specs:
            if "brief" in spec:
                p = (project_root / spec["brief"]).resolve()
                if not p.is_file():
                    brief_severity = "error" if strict else "warning"
                    findings.append(
                        Finding(brief_severity, f"brief not found: {spec['brief']}", line_no)
                    )
            if "role" in spec:
                try:
                    _load_role(spec["role"], project_root=project_root)
                except RoleNotFoundError as e:
                    findings.append(Finding("error", str(e), line_no))
            wt = spec.get("worktree")
            if wt is not None:
                if wt in seen_worktrees:
                    findings.append(
                        Finding("error", f"duplicate worktree name: {wt}", line_no)
                    )
                seen_worktrees.add(wt)

    has_accept = any(
        re.match(r"^##\s+(ACCEPTANCE|VERIFIER)", ln, re.IGNORECASE) for ln in lines
    )
    if not has_accept:
        findings.append(Finding("error", "no ## ACCEPTANCE or ## VERIFIER section"))

    has_team = any(re.match(r"^##\s+TEAM", ln, re.IGNORECASE) for ln in lines)
    if not has_team:
        findings.append(
            Finding("warning", "no ## TEAM section — PM may have no team context")
        )
    if "worker_done" not in text:
        findings.append(
            Finding(
                "warning",
                "mission body doesn't mention 'worker_done' — PM may not know how to terminate",
            )
        )
    return findings


def render(findings: list[Finding]) -> str:
    """Human-readable output."""
    out: list[str] = []
    for f in findings:
        prefix = f"{f.severity}: "
        if f.line is not None:
            prefix = f"line {f.line}: " + prefix
        out.append(prefix + f.message)
    if not findings:
        out.append("OK")
    return "\n".join(out)


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)
