"""Regression tests for the no-client warning in session_start.py.

When neither claude-agent-sdk nor anthropic_api_key is available, the
SessionStart hook should emit a one-time user-visible warning so the
user knows LLM-backed features are disabled. A marker file in data_dir
suppresses the warning on subsequent sessions.
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_warning_emitted_on_first_call(tmp_path):
    import session_start
    out = io.StringIO()
    with redirect_stdout(out):
        session_start._emit_no_client_warning(tmp_path)
    text = out.getvalue()
    assert "[multiplai]" in text
    assert "Agent SDK" in text
    assert "anthropic_api_key" in text
    assert (tmp_path / "no_client_warning_emitted").exists()


def test_warning_suppressed_when_marker_exists(tmp_path):
    import session_start
    (tmp_path / "no_client_warning_emitted").touch()
    out = io.StringIO()
    with redirect_stdout(out):
        session_start._emit_no_client_warning(tmp_path)
    assert out.getvalue() == ""


def test_detect_client_type_none_string_format():
    """The warning trigger relies on client_type.startswith('none')."""
    from lib.model_client import detect_client_type
    import os
    saved_sdk = sys.modules.pop("claude_agent_sdk", None)
    saved_key = os.environ.pop("CLAUDE_PLUGIN_OPTION_anthropic_api_key", None)
    try:
        # Try a real call only if sdk is actually unavailable
        result = detect_client_type()
        if "claude_agent_sdk" not in sys.modules and not os.environ.get(
            "CLAUDE_PLUGIN_OPTION_anthropic_api_key"
        ):
            assert result.startswith("none"), (
                f"detect_client_type should start with 'none' when neither "
                f"SDK nor key is present; got: {result!r}"
            )
    finally:
        if saved_sdk is not None:
            sys.modules["claude_agent_sdk"] = saved_sdk
        if saved_key is not None:
            os.environ["CLAUDE_PLUGIN_OPTION_anthropic_api_key"] = saved_key
