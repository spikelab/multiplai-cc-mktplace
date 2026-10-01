"""`check` re-runs citation_gate on a finished walkthrough; it exits 1 on a moved line."""

from __future__ import annotations

import re

from fakes import FakeAgents
import fixture_repo
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main


def _walkthrough(ws, tmp_path, monkeypatch, fake_fetch):
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents())
    args = ["run", str(ws["engine"] / "bookings"), "--trust-repo", "--runs-dir", str(tmp_path / "runs"),
            "--output", str(tmp_path / "out"), "--docs", "https://docs.acme.test", "--lsp-timeout", "0"]
    assert main(args) == 0
    return tmp_path / "out" / "bookings-walkthrough.md"


def test_check_exits_1_on_a_moved_line(fresh_ws, tmp_path, monkeypatch, fake_fetch, capsys):
    md = _walkthrough(fresh_ws, tmp_path, monkeypatch, fake_fetch)
    assert main(["check", str(md)]) == 0
    # A link that now points one line off: its quote is no longer at that line.
    text = md.read_text()
    moved = text.replace("`engine/bookings/urls.py:6`](", "`engine/bookings/urls.py:5`](", 1)
    assert moved != text
    md.write_text(moved)
    capsys.readouterr()
    assert main(["check", str(md)]) == 1
    out = capsys.readouterr().out
    assert "FAIL engine/bookings/urls.py:5-5: quote not at cited lines" in out
    assert re.search(r"check: \d+ checked, 1 failed", out)


def test_check_at_head_says_the_code_moved(fresh_ws, tmp_path, monkeypatch, fake_fetch, capsys):
    md = _walkthrough(fresh_ws, tmp_path, monkeypatch, fake_fetch)
    urls = fresh_ws["engine"] / "bookings" / "urls.py"
    urls.write_text("# a new first line\n" + urls.read_text())
    fixture_repo.commit(fresh_ws["engine"], "move the route down")
    assert main(["check", str(md)]) == 0                 # the commits in the header did not change
    capsys.readouterr()
    assert main(["check", str(md), "--at-head"]) == 1    # HEAD has moved on
    assert "engine/bookings/urls.py:6" in capsys.readouterr().out


def test_check_catches_an_edited_snippet(tmp_path, ws):
    sha = fixture_repo._git(ws["engine"], "rev-parse", "HEAD")
    md = tmp_path / "x.md"
    md.write_text(
        "# x\n\n## Commits read\n\n| Repo | Path | Commit |\n|---|---|---|\n"
        f"| engine | {ws['engine']} | `{sha}` |\n\n"
        "<!-- snippet engine/bookings/tasks.py:4-5 -->\n[`engine/bookings/tasks.py:4-5`](x \"def poll_task():\")\n\n"
        "```python\n@shared_task(name='bookings.poll')\ndef poll_task_renamed():\n```\n")
    assert main(["check", str(md)]) == 1


def test_a_snippet_ending_on_a_blank_line_passes_check(tmp_path, ws):
    from walkthrough_pipeline import target
    from walkthrough_pipeline.models import Citation, RepoInfo
    from walkthrough_pipeline.render import Linker

    lines = (ws["engine"] / "bookings/views.py").read_text().split("\n")
    blank = next(i for i, l in enumerate(lines, 1) if i > 1 and not l.strip())
    sha = target.head_sha(ws["engine"])
    linker = Linker.__new__(Linker)
    linker.repos = {"engine": RepoInfo(key="engine", path=str(ws["engine"]), head_sha=sha)}
    linker.out_dir = tmp_path
    linker.citations = {}
    snip = linker.snippet(Citation(path="engine/bookings/views.py", line_start=blank - 1, line_end=blank,
                                   quote=lines[blank - 2].strip()))
    assert f"views.py:{blank - 1}-{blank - 1} -->" in snip
    md = tmp_path / "x.md"
    md.write_text("# x\n\n## Commits read\n\n| Repo | Path | Commit |\n|---|---|---|\n"
                  f"| engine | {ws['engine']} | `{sha}` |\n\n{snip}\n")
    assert main(["check", str(md)]) == 0
