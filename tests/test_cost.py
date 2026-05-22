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


class TestFormatTokens:
    def test_k_suffix(self) -> None:
        assert cost.format_tokens(42_000, 8_000, 180_000) == "42k/8k cache=180k"

    def test_all_zero(self) -> None:
        assert cost.format_tokens(0, 0, 0) == "0/0 cache=0"

    def test_mixed_m_k(self) -> None:
        assert cost.format_tokens(1_500_000, 250_000, 12_000_000) == "1.5M/250k cache=12M"

    def test_below_1k_verbatim(self) -> None:
        assert cost.format_tokens(950, 100, 0) == "950/100 cache=0"

    def test_exactly_1k(self) -> None:
        assert cost.format_tokens(1_000, 1_000, 1_000) == "1k/1k cache=1k"

    def test_exactly_1m_one_decimal(self) -> None:
        # 1_000_000 is ≥1M and <10M → one decimal place
        assert cost.format_tokens(1_000_000, 0, 0) == "1.0M/0 cache=0"

    def test_exactly_10m_no_decimal(self) -> None:
        # 10_000_000 is ≥10M → no decimal
        assert cost.format_tokens(10_000_000, 0, 0) == "10M/0 cache=0"

    def test_large_values(self) -> None:
        assert cost.format_tokens(100_000_000, 50_000_000, 200_000_000) == "100M/50M cache=200M"

    @pytest.mark.parametrize("n, expected_prefix", [
        (999, "999"),
        (1_000, "1k"),
        (999_999, "999k"),
        (1_000_000, "1.0M"),
        (9_900_000, "9.9M"),
        (10_000_000, "10M"),
    ])
    def test_boundary_values_for_input(self, n: int, expected_prefix: str) -> None:
        result = cost.format_tokens(n, 0, 0)
        assert result.startswith(expected_prefix + "/")
