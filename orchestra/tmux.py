"""tmux primitives for claude-orchestra.

Single rule: every function is a thin wrapper over `tmux` invocations.
Business logic (retries, choreography, idle policy) lives in higher layers.
"""
from __future__ import annotations

import re
import subprocess

# ANSI scrubber — covers CSI/OSC/DCS/charset/SI-SO that tmux pane output may contain.
# Match the patterns from primeline-ai/claude-tmux-orchestration; battle-tested.
_ANSI_RES = [
    re.compile(r"\x1b\[[0-9;:?<=>]*[a-zA-Z]"),       # CSI
    re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"),  # OSC
    re.compile(r"\x1bP[^\x1b]*(?:\x1b\\|$)"),         # DCS
    re.compile(r"\x1b[()][0-9A-Za-z]"),                # charset switches
    re.compile(r"[\x0e\x0f]"),                          # SI/SO
]

_SPINNER_RE = re.compile(r"(Running|thinking|Searching|Reading|Writing|Editing)")
_PROMPT_RE = re.compile(r"(?:❯|>)\s*$", re.MULTILINE)


def _strip_ansi(text: str) -> str:
    for r in _ANSI_RES:
        text = r.sub("", text)
    return text


def _run(argv: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "check": True,
        "capture_output": True,
        "text": True,
    }
    if input is not None:
        kwargs["input"] = input
    return subprocess.run(argv, **kwargs)  # type: ignore[call-overload,no-any-return]


# ---- send ----

def send_literal(target: str, text: str) -> None:
    _run(["tmux", "send-keys", "-t", target, "-l", text])


def send_enter(target: str) -> None:
    _run(["tmux", "send-keys", "-t", target, "Enter"])


def send_multiline(target: str, text: str, *, buffer_name: str = "orch") -> None:
    """Load text into a named tmux buffer and paste it, then submit with Enter.

    send-keys breaks on embedded newlines; paste-buffer is the only reliable path.
    -p enables paste bracket mode (no shell interpretation); -d deletes the buffer.
    """
    _run(["tmux", "load-buffer", "-b", buffer_name, "-"], input=text)
    _run(["tmux", "paste-buffer", "-p", "-d", "-b", buffer_name, "-t", target])
    _run(["tmux", "send-keys", "-t", target, "Enter"])


# ---- read ----

def capture(target: str, lines: int = 80) -> str:
    """Return the last `lines` lines from `target`, ANSI-stripped."""
    proc = _run(["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"])
    return _strip_ansi(proc.stdout)


def is_idle(target: str) -> bool:
    """Cheap idle heuristic: spinner overrides everything; otherwise look for prompt."""
    text = capture(target, lines=12)
    if _SPINNER_RE.search(text):
        return False
    return bool(_PROMPT_RE.search(text))


def pane_current_command(target: str) -> str:
    proc = _run(["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"])
    return proc.stdout.strip()


# ---- session / window ----

def ensure_session(name: str, *, cwd: str) -> None:
    """Create the session if it doesn't exist; no-op if it does."""
    try:
        _run(["tmux", "has-session", "-t", name])
    except subprocess.CalledProcessError:
        _run(["tmux", "new-session", "-d", "-s", name, "-c", cwd])


def new_window(*, session: str, name: str, cwd: str) -> str:
    """Create a new window in `session`. Returns its target string."""
    _run(["tmux", "new-window", "-t", f"{session}:", "-n", name, "-c", cwd])
    return f"{session}:{name}"
