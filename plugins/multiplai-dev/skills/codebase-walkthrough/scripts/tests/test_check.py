"""`check` re-runs citation_gate on a finished walkthrough; it exits 1 on a moved line."""

from __future__ import annotations

import re
from pathlib import Path

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


def _linker(repo_key: str, repo: Path, out_dir: Path):
    from walkthrough_pipeline import target
    from walkthrough_pipeline.models import RepoInfo
    from walkthrough_pipeline.render import Linker

    linker = Linker.__new__(Linker)
    linker.repos = {repo_key: RepoInfo(key=repo_key, path=str(repo), head_sha=target.head_sha(repo))}
    linker.out_dir = out_dir
    linker.citations = {}
    return linker


def _header(repo_key: str, repo: Path, sha: str) -> str:
    return f"# x\n\n## Commits read\n\n| Repo | Path | Commit |\n|---|---|---|\n| {repo_key} | {repo} | `{sha}` |\n\n"


def test_links_with_a_pipe_a_space_and_a_paren_render_and_pass_check(tmp_path):
    from walkthrough_pipeline.models import Citation

    repo = tmp_path / "my repo (copy)"
    sha = fixture_repo.make_repo(repo, {"q.py": "flags = READ | WRITE\nend = 1\n"})
    out = tmp_path / "out"
    out.mkdir()
    linker = _linker("my", repo, out)
    link = linker.link(Citation(path="my/q.py", line_start=1, line_end=1, quote="flags = READ | WRITE"))
    assert " " not in link.split("](")[1].split(" ")[0]          # the URL has no bare space
    row = f"| model | `flags` | {link} |"
    assert len(re.split(r"(?<!\\)\|", row)) == 5                # the pipe in the title does not split the row
    md = out / "x.md"
    md.write_text(_header("my", repo, sha) + row + "\n")
    assert main(["check", str(md)]) == 0


def test_a_snippet_past_the_end_of_the_file_passes_check(tmp_path):
    from walkthrough_pipeline.models import Citation

    repo = tmp_path / "r"
    sha = fixture_repo.make_repo(repo, {"q.py": "a = 1\nb = 2\nc = 3"})
    linker = _linker("r", repo, tmp_path)
    snip = linker.snippet(Citation(path="r/q.py", line_start=2, line_end=6, quote="b = 2"))
    assert "q.py:2-3 -->" in snip
    md = tmp_path / "x.md"
    md.write_text(_header("r", repo, sha) + snip + "\n")
    assert main(["check", str(md)]) == 0


def test_a_link_check_cannot_parse_is_a_failure(tmp_path, ws, capsys):
    sha = fixture_repo._git(ws["engine"], "rev-parse", "HEAD")
    md = tmp_path / "x.md"
    md.write_text(_header("engine", ws["engine"], sha) +
                  '[`engine/bookings/tasks.py:4`](../a b/tasks.py#L4 "def poll_task():")\n')
    assert main(["check", str(md)]) == 1
    assert "link could not be parsed" in capsys.readouterr().out


def test_check_repo_override_finds_a_moved_repository(fresh_ws, tmp_path, monkeypatch, fake_fetch, capsys):
    md = _walkthrough(fresh_ws, tmp_path, monkeypatch, fake_fetch)
    moved = tmp_path / "moved-engine"
    fresh_ws["engine"].rename(moved)
    from walkthrough_pipeline import gates
    gates._show.cache_clear()                             # the run above cached reads at the old path
    capsys.readouterr()
    assert main(["check", str(md)]) == 1                 # the header's path is gone
    capsys.readouterr()
    assert main(["check", str(md), "--repo", f"engine={moved}"]) == 0
    assert re.search(r"check: [1-9]\d* checked, 0 failed", capsys.readouterr().out)
