"""Tiny YAML front-matter parser for orchestra role files.

Supports a deliberately small subset:
- Front matter delimited by `---\\n` at start and a matching `\\n---\\n` close.
- Flat top-level keys mapping to strings.
- One level of nesting: a key with no inline value, whose children are
  two-space-indented `key: value` lines or four-space-indented `- item` lines.

Rejects: flow syntax (`{}` / `[]`), nesting beyond two levels, tabs as
indentation. We avoid pyyaml to keep the install surface small; the schema
is stable and unsupported syntax raises clearly so future schema
extensions are a deliberate parser upgrade rather than a silent failure.
"""
from __future__ import annotations

import re
from typing import Any

_DELIM = "---"


class FrontmatterError(ValueError):
    """Raised on any malformed or out-of-scope front matter."""


_KV = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")
_LIST = re.compile(r"^(?P<indent>[ ]*)-\s+(?P<value>.+)$")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _check_no_flow(value: str, lineno: int) -> None:
    if "{" in value or "}" in value or value.strip().startswith("["):
        raise FrontmatterError(f"flow syntax not supported (line {lineno})")


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown string into (front matter dict, remaining body).

    If `text` has no front matter, returns ({}, text) unchanged.
    """
    if not text.startswith(_DELIM + "\n"):
        return {}, text

    lines = text.split("\n")
    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i] == _DELIM:
            close_idx = i
            break
    if close_idx == -1:
        raise FrontmatterError("missing closing --- delimiter")

    fm_lines = lines[1:close_idx]
    body = "\n".join(lines[close_idx + 1 :])

    meta: dict[str, Any] = {}
    current_key: str | None = None
    current_child_key: str | None = None

    for idx, raw in enumerate(fm_lines):
        lineno = idx + 2  # +1 for opening ---, +1 for 1-based
        if not raw.strip():
            continue
        if "\t" in raw:
            raise FrontmatterError(f"tabs not supported (line {lineno})")

        m_list = _LIST.match(raw)
        if m_list:
            indent = len(m_list.group("indent"))
            value = _strip_quotes(m_list.group("value"))
            _check_no_flow(value, lineno)
            if indent == 4 and current_key is not None and current_child_key is not None:
                target = meta[current_key].setdefault(current_child_key, [])
                if not isinstance(target, list):
                    raise FrontmatterError(f"mixing list and value (line {lineno})")
                target.append(value)
                continue
            raise FrontmatterError(f"unexpected list item (line {lineno})")

        m_kv = _KV.match(raw)
        if not m_kv:
            raise FrontmatterError(f"unrecognized syntax (line {lineno})")

        indent = len(m_kv.group("indent"))
        key = m_kv.group("key")
        value = m_kv.group("value")
        _check_no_flow(value, lineno)

        if indent == 0:
            current_key = key
            current_child_key = None
            if value.strip() == "":
                meta[key] = {}
            else:
                meta[key] = _strip_quotes(value)
            continue
        if indent == 2:
            if current_key is None or not isinstance(meta.get(current_key), dict):
                raise FrontmatterError(f"nested key without parent (line {lineno})")
            current_child_key = key
            if value.strip() == "":
                meta[current_key][key] = []
            else:
                meta[current_key][key] = _strip_quotes(value)
            continue
        raise FrontmatterError(f"unsupported indent level (line {lineno})")

    return meta, body
