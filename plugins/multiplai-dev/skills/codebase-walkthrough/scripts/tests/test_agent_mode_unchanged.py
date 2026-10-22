"""Agent mode's Markdown is the same as before the human-mode explainer (plan-walkthrough-explainer-2026-10-21).

`fixtures/agent-mode-walkthrough.md` was written by this test, with
`WALKTHROUGH_WRITE_AGENT_FIXTURE=1`, on the commit before that change. The run's date and the
relative path from the output directory to the fixture workspace are replaced by placeholders,
since both differ from run to run.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main

EXPECTED = Path(__file__).resolve().parent / "fixtures" / "agent-mode-walkthrough.md"
WRITE = "WALKTHROUGH_WRITE_AGENT_FIXTURE"


def _normalise(md: str, out: Path, ws: Path) -> str:
    md = md.replace(date.today().isoformat(), "<DATE>")
    rel = os.path.relpath(ws, out)
    return md.replace(rel, "<WS>")


def test_agent_mode_markdown_is_unchanged(ws, tmp_path, monkeypatch, fake_fetch):
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    out = tmp_path / "out"
    assert (
        main(
            [
                "run",
                str(ws["engine"] / "bookings"),
                "--trust-repo",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--output",
                str(out),
                "--mode",
                "agent",
                "--lsp-timeout",
                "0",
            ]
        )
        == 0
    )
    md = _normalise(
        (out / "bookings-walkthrough.md").read_text(encoding="utf-8"), out, ws["ws"]
    )
    if os.environ.get(WRITE):
        EXPECTED.parent.mkdir(exist_ok=True)
        EXPECTED.write_text(md, encoding="utf-8")
    assert md == EXPECTED.read_text(encoding="utf-8"), (
        f"run with {WRITE}=1 only after a deliberate change"
    )
