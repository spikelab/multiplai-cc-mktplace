"""End-to-end tests for the qmd resources backend in context_manager.

Drives the full stdin → stdout hook flow with resources_retrieval=qmd
and a FAKE qmd binary on PATH (qmd_mode=local), verifying:

- qmd results render in the === RESOURCES === section (path + excerpt
  + read-the-full-file preamble)
- the resources catalog/router path is skipped under the qmd backend
- fail-open when qmd is missing or broken
- a `local`-mode integration test against a real qmd, gated on qmd
  being installed (BM25 only — no embedding model download needed)
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
CONTEXT_MANAGER = SCRIPTS_DIR / "context_manager.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def env_setup(tmp_path):
    data_dir = tmp_path / "plugin_data"
    memory_dir = tmp_path / "memory"
    resources_dir = tmp_path / "resources"
    workspace = tmp_path / "ws"
    for d in (data_dir / "catalogs", memory_dir, resources_dir, workspace):
        d.mkdir(parents=True)
    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "memory_dir": memory_dir,
        "resources_dir": resources_dir,
        "workspace": workspace,
    }


def _fake_qmd(tmp_path: Path, results: list[dict], exit_code: int = 0) -> Path:
    """Install a fake qmd binary that prints *results* as JSON."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "qmd"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo '{json.dumps(results)}'
        exit {exit_code}
        """))
    script.chmod(0o755)
    return bin_dir


def _run_hook(env_setup, *, prompt: str, qmd_bin_dir: Path | None,
              extra_env: dict | None = None) -> dict:
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("CLAUDE_PLUGIN"):
            del env[k]
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    env["CLAUDE_PLUGIN_DATA"] = str(env_setup["data_dir"])
    env["CLAUDE_PLUGIN_OPTION_memory_dir"] = str(env_setup["memory_dir"])
    env["CLAUDE_PLUGIN_OPTION_resources_dir"] = str(env_setup["resources_dir"])
    env["CLAUDE_PLUGIN_OPTION_enable_resources"] = "true"
    env["CLAUDE_PLUGIN_OPTION_resources_retrieval"] = "qmd"
    env["CLAUDE_PLUGIN_OPTION_qmd_mode"] = "local"
    # Point HOME away from any real ~/.bun/bin qmd; PATH carries the fake.
    env["HOME"] = str(env_setup["tmp_path"])
    if qmd_bin_dir is not None:
        env["PATH"] = f"{qmd_bin_dir}:{env['PATH']}"
    else:
        env["PATH"] = "/usr/bin:/bin"
    if extra_env:
        env.update(extra_env)

    stdin = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "cwd": str(env_setup["workspace"]),
    })
    result = subprocess.run(
        [sys.executable, str(CONTEXT_MANAGER)],
        input=stdin, capture_output=True, text=True, env=env, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"context_manager exited {result.returncode}\nstderr: {result.stderr[:500]}"
        )
    out = result.stdout.strip().splitlines()
    if not out:
        raise AssertionError(f"No stdout. stderr: {result.stderr[:500]}")
    return json.loads(out[-1])


QMD_RESULTS = [
    {"file": "qmd://resources/water/filters.md", "score": 0.72,
     "title": "Water filter comparison", "snippet": "Reverse osmosis vs carbon."},
    {"file": "qmd://resources/gcp/isolation.md", "score": 0.44,
     "title": "GCP isolation", "snippet": "Project-per-env pattern."},
]


class TestQmdBackendE2E:
    def test_results_render_in_resources_section(self, env_setup):
        bin_dir = _fake_qmd(env_setup["tmp_path"], QMD_RESULTS)
        out = _run_hook(env_setup, prompt="which water filter should I buy?",
                        qmd_bin_dir=bin_dir)
        ctx = out["context"]
        assert "=== RESOURCES ===" in ctx
        assert f"{env_setup['resources_dir']}/water/filters.md" in ctx
        assert "Reverse osmosis vs carbon." in ctx
        assert "Read the full file" in ctx
        assert out["resources_files"] == 2

    def test_weak_scores_filtered(self, env_setup):
        weak = [dict(QMD_RESULTS[0], score=0.05)]
        bin_dir = _fake_qmd(env_setup["tmp_path"], weak)
        out = _run_hook(env_setup, prompt="which water filter should I buy?",
                        qmd_bin_dir=bin_dir)
        assert out.get("resources_files", 0) == 0

    def test_fail_open_when_qmd_missing(self, env_setup):
        (env_setup["memory_dir"] / "notes.md").write_text("# Notes\nsome memory")
        out = _run_hook(env_setup, prompt="which water filter should I buy?",
                        qmd_bin_dir=None)
        # No crash; resources empty, memory fallback still works.
        assert out.get("resources_files", 0) == 0
        assert out["memory_files"] >= 1

    def test_fail_open_when_qmd_errors(self, env_setup):
        bin_dir = _fake_qmd(env_setup["tmp_path"], [], exit_code=3)
        out = _run_hook(env_setup, prompt="which water filter should I buy?",
                        qmd_bin_dir=bin_dir)
        assert out.get("resources_files", 0) == 0

    def test_resources_catalog_not_loaded_under_qmd(self, env_setup):
        """A resources catalog on disk must be ignored when the backend is qmd."""
        from generators.base import CATALOG_SCHEMA_VERSION

        (env_setup["resources_dir"] / "catalog-doc.md").write_text("# Catalog doc")
        (env_setup["data_dir"] / "catalogs" / "resources.json").write_text(json.dumps({
            "schema_version": CATALOG_SCHEMA_VERSION,
            "entries": [{"source": "catalog-doc.md", "summary": "catalog doc",
                         "intent_domains": ["water filter shopping"]}],
        }))
        bin_dir = _fake_qmd(env_setup["tmp_path"], QMD_RESULTS)
        out = _run_hook(env_setup, prompt="which water filter should I buy?",
                        qmd_bin_dir=bin_dir)
        assert "catalog-doc.md" not in out["context"]
        assert "water/filters.md" in out["context"]

    def test_catalog_backend_untouched_by_default(self, env_setup):
        """resources_retrieval unset → catalog path (no qmd invocation)."""
        bin_dir = _fake_qmd(env_setup["tmp_path"], QMD_RESULTS)
        out = _run_hook(
            env_setup, prompt="which water filter should I buy?",
            qmd_bin_dir=bin_dir,
            extra_env={"CLAUDE_PLUGIN_OPTION_resources_retrieval": "catalog"},
        )
        # No resources catalog on disk → no resources under the catalog path.
        assert out.get("resources_files", 0) == 0


# ---------------------------------------------------------------------------
# Real-qmd integration (local mode, BM25 only), gated on qmd on PATH
# ---------------------------------------------------------------------------


def _real_qmd() -> str | None:
    return shutil.which("qmd", path=f"{os.path.expanduser('~')}/.bun/bin:"
                        + os.environ.get("PATH", ""))


@pytest.mark.skipif(_real_qmd() is None, reason="qmd not installed")
class TestQmdLocalIntegration:
    def test_bm25_search_roundtrip(self, tmp_path):
        import qmd_retrieval as qr

        ws = tmp_path / "ws"
        rd = ws / "RESOURCES"
        rd.mkdir(parents=True)
        (rd / "zanzibar-travel.md").write_text(
            "# Zanzibar travel research\nFerries run from Dar es Salaam daily."
        )
        env = dict(os.environ, HOME=str(tmp_path))
        subprocess.run([_real_qmd(), "init"], cwd=ws, env=env, check=True,
                       capture_output=True, timeout=60)
        subprocess.run(
            [_real_qmd(), "collection", "add", str(rd), "--name", "resources"],
            cwd=ws, env=env, check=True, capture_output=True, timeout=60,
        )
        subprocess.run([_real_qmd(), "update"], cwd=ws, env=env, check=True,
                       capture_output=True, timeout=120)

        target = qr.QmdTarget(
            workspace=str(ws), resources_dir=str(rd), strategy="fts",
        )
        entries = qr.search("zanzibar travel ferries", target)
        assert any("zanzibar-travel.md" in e["path"] for e in entries)
