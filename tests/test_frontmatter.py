"""Tests for orchestra._frontmatter — tiny YAML front-matter parser."""
from __future__ import annotations

import pytest

from orchestra._frontmatter import FrontmatterError, parse


class TestNoFrontmatter:
    def test_returns_empty_meta_and_original_body(self) -> None:
        text = "# Hello\nbody line\n"
        meta, body = parse(text)
        assert meta == {}
        assert body == text

    def test_text_without_opening_dashes_is_not_frontmatter(self) -> None:
        meta, body = parse("not-a-line\n---\nfoo: bar\n---\n")
        assert meta == {}


class TestSimplePairs:
    def test_string_value_unquoted(self) -> None:
        meta, body = parse("---\nname: pm\n---\n# Hello\n")
        assert meta == {"name": "pm"}
        assert body == "# Hello\n"

    def test_multiple_pairs(self) -> None:
        text = "---\nname: pm\nversion: 1\n---\nbody\n"
        meta, body = parse(text)
        assert meta == {"name": "pm", "version": "1"}
        assert body == "body\n"

    def test_quoted_string_value_keeps_quotes_stripped(self) -> None:
        meta, _ = parse('---\npattern: "Bash(rm:*)"\n---\nbody\n')
        assert meta == {"pattern": "Bash(rm:*)"}


class TestLists:
    def test_list_of_strings(self) -> None:
        text = (
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "    - Grep\n"
            "  deny:\n"
            "    - Write\n"
            "    - Edit\n"
            "---\n"
            "body\n"
        )
        meta, _ = parse(text)
        assert meta == {
            "permissions": {
                "allow": ["Read", "Grep"],
                "deny": ["Write", "Edit"],
            }
        }

    def test_quoted_list_items(self) -> None:
        text = (
            "---\n"
            "permissions:\n"
            "  allow:\n"
            '    - "Bash(git log:*)"\n'
            "---\n"
        )
        meta, _ = parse(text)
        assert meta["permissions"]["allow"] == ["Bash(git log:*)"]


class TestErrors:
    def test_missing_closing_dashes(self) -> None:
        with pytest.raises(FrontmatterError, match="missing closing"):
            parse("---\nname: pm\nbody without close\n")

    def test_flow_mapping_rejected(self) -> None:
        with pytest.raises(FrontmatterError, match="line 2"):
            parse("---\nname: {a: b}\n---\n")

    def test_flow_list_rejected(self) -> None:
        with pytest.raises(FrontmatterError, match="line 2"):
            parse("---\nname: [a, b]\n---\n")

    def test_nested_mapping_more_than_two_deep_rejected(self) -> None:
        text = "---\nouter:\n  middle:\n    inner:\n      - x\n---\n"
        with pytest.raises(FrontmatterError, match="line 4"):
            parse(text)
