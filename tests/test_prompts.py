from __future__ import annotations

from orchestra.prompts import render_startup_prompt


def test_contains_required_identity():
    p = render_startup_prompt(
        worker_id="w1", task="Implement auth", model="sonnet", ctx_files=[],
    )
    assert "w1" in p
    assert "Implement auth" in p
    assert "sonnet" in p
    assert "orch/w1" in p


def test_contains_required_commands():
    p = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    assert "orchestra worker status" in p
    assert "orchestra worker escalate" in p


def test_contains_required_rules():
    p = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    # case-insensitive checks
    low = p.lower()
    assert "commit" in low and "push" in low
    assert "do not spawn" in low
    assert "do not end this session" in low


def test_context_only_when_files_present():
    no_ctx = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    with_ctx = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet",
        ctx_files=["src/auth.py", "src/db.py"],
    )
    assert "CONTEXT" not in no_ctx
    assert "CONTEXT" in with_ctx
    assert "src/auth.py" in with_ctx
    assert "src/db.py" in with_ctx


def test_no_trailing_newline():
    p = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    assert not p.endswith("\n")
