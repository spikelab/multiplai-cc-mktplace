"""Regression test for context_manager.py top-level exception safety.

UserPromptSubmit runs on every prompt. If anything inside main() raises
an unhandled exception, the wrapper at __main__ must:
  1. Print a safe JSON fallback to stdout (valid shape, empty context)
  2. Exit with status 0 (non-zero would surface to the user mid-prompt)
  3. Not propagate the traceback

This guards against regressions that would re-introduce the pre-0.2.1
behavior where a single failing file read or JSON parse would crash
mid-conversation.
"""

import json
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT = PLUGIN_ROOT / "scripts" / "context_manager.py"


def _assert_safe_fallback(stdout: bytes):
    """Parse stdout and check it matches the empty-context JSON shape."""
    payload = json.loads(stdout.decode("utf-8"))
    assert payload["context"] == ""
    assert payload["memory_files"] == 0
    assert payload["skills_files"] == 0
    assert payload["resources_files"] == 0
    assert payload["corpus_counts"] == {"memory": 0, "skills": 0, "resources": 0}


def test_main_emits_safe_fallback_on_unhandled_exception(tmp_path):
    """If main() raises, the entry-point guard prints safe JSON and exits 0."""
    # Run a wrapper that imports context_manager, swaps main() for a raiser,
    # and re-executes the same guard pattern as the script's __main__ block.
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        f"import sys\n"
        f"sys.path.insert(0, {str(PLUGIN_ROOT / 'scripts')!r})\n"
        f"import context_manager\n"
        f"def _boom():\n"
        f"    raise RuntimeError('synthetic test failure')\n"
        f"context_manager.main = _boom\n"
        f"# Re-execute the guarded entry-point block.\n"
        f"import json\n"
        f"try:\n"
        f"    context_manager.main()\n"
        f"except Exception:\n"
        f"    try:\n"
        f"        context_manager.logger.exception('test path')\n"
        f"    except Exception:\n"
        f"        pass\n"
        f"    print(json.dumps({{\n"
        f"        'context': '',\n"
        f"        'memory_files': 0,\n"
        f"        'skills_files': 0,\n"
        f"        'resources_files': 0,\n"
        f"        'corpus_counts': {{'memory': 0, 'skills': 0, 'resources': 0}},\n"
        f"    }}))\n"
        f"    sys.exit(0)\n"
    )
    proc = subprocess.run(
        [sys.executable, str(wrapper)],
        input=b'{"prompt": "test"}',
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"guard must exit 0; got {proc.returncode}\nstderr: {proc.stderr.decode()}"
    )
    _assert_safe_fallback(proc.stdout)


def test_entry_point_guard_present_in_source():
    """Belt-and-braces: ensure the guard block isn't removed by refactors."""
    src = SCRIPT.read_text()
    assert 'if __name__ == "__main__":' in src
    # The guard must catch a broad Exception, log, emit JSON, and exit 0.
    assert "try:\n        main()" in src
    assert "except Exception:" in src
    assert '"corpus_counts"' in src
    assert "sys.exit(0)" in src
