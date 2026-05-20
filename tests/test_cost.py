"""Tests for orchestra.cost (token pricing + family extraction)."""
from __future__ import annotations

import pytest

from orchestra import cost


class TestFamilyOf:
    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("claude-opus-4-7", "opus"),
            ("claude-opus-4-7[1m]", "opus"),
            ("claude-sonnet-4-6-20251022", "sonnet"),
            ("claude-haiku-4-5-20251001", "haiku"),
            ("opus", "opus"),
            ("sonnet", "sonnet"),
            ("haiku", "haiku"),
            ("OPUS", "opus"),  # case-insensitive
        ],
    )
    def test_extracts_known_family(self, model_id: str, expected: str) -> None:
        assert cost.family_of(model_id) == expected

    def test_none_falls_back_to_opus(self) -> None:
        assert cost.family_of(None) == "opus"

    def test_empty_string_falls_back_to_opus(self) -> None:
        assert cost.family_of("") == "opus"

    def test_unknown_model_falls_back_to_opus(self) -> None:
        # Conservative: unknown identifier → price as opus (over-bill, don't under-bill).
        assert cost.family_of("gpt-4") == "opus"


class TestCostFor:
    def test_opus_one_million_each(self) -> None:
        # 1M in @ $15 + 1M out @ $75 = $90.00
        assert cost.cost_for("opus", 1_000_000, 1_000_000) == pytest.approx(90.0)

    def test_sonnet_input_only(self) -> None:
        assert cost.cost_for("sonnet", 1_000_000, 0) == pytest.approx(3.0)

    def test_haiku_output_only(self) -> None:
        assert cost.cost_for("haiku", 0, 1_000_000) == pytest.approx(5.0)

    def test_zero_tokens_is_zero_cost(self) -> None:
        assert cost.cost_for("opus", 0, 0) == 0.0

    def test_unknown_model_uses_opus_rate(self) -> None:
        # Same as Opus: $15 in / $75 out.
        assert cost.cost_for("gpt-4", 1_000_000, 0) == pytest.approx(15.0)
