"""Each gate's pass and fail cases. Gates re-read the commit; none calls a model."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from walkthrough_pipeline import gates, target
from walkthrough_pipeline.models import Citation, Fact, Reference, RepoInfo, SymbolEntry, Unit

GATES_SRC = Path(gates.__file__).read_text()


@pytest.fixture
def repos(ws) -> dict[str, RepoInfo]:
    out = {}
    for key in ("engine", "front", "warehouse"):
        out[key] = RepoInfo(key=key, path=str(ws[key]), head_sha=target.head_sha(ws[key]))
    return out


def cite(path, a, b, quote):
    return Citation(path=path, line_start=a, line_end=b, quote=quote)


def test_no_gate_calls_a_model():
    assert not re.search(r"agent_call|run_agent", GATES_SRC)


def test_citation_gate_passes_a_real_quote(repos):
    assert gates.citation_gate(repos, cite("engine/bookings/urls.py", 6, 6, "path('hooks/acme/'")).passed


@pytest.mark.parametrize("c, reason", [
    (("engine/bookings/urls.py", 5, 5, "path('hooks/acme/'"), "quote not at cited lines"),
    (("engine/bookings/nowhere.py", 1, 1, "x"), "path not at commit"),
    (("nope/bookings/urls.py", 1, 1, "x"), "path names no known repo"),
    (("engine/bookings/urls.py", 6, 6, "   "), "empty quote"),
])
def test_citation_gate_fails(repos, c, reason):
    g = gates.citation_gate(repos, cite(*c))
    assert not g.passed and g.reason == reason


def test_citation_gate_reads_the_commit_not_the_working_tree(fresh_ws):
    repo = RepoInfo(key="engine", path=str(fresh_ws["engine"]), head_sha=target.head_sha(fresh_ws["engine"]))
    (fresh_ws["engine"] / "bookings" / "urls.py").write_text("changed in the working tree only\n")
    assert gates.citation_gate({"engine": repo}, cite("engine/bookings/urls.py", 6, 6, "hooks/acme/")).passed


def test_boundary_gate_drops_an_entry_whose_quote_moved(repos):
    good = Reference(repo="front", kind="http-route", symbol="hooks/acme/", path="front/src/api.ts", line=5,
                     quote="return fetch(`${BASE}/hooks/acme/`, { method: 'POST' });", method="ast-grep")
    bad = good.model_copy(update={"line": 4})
    sym = SymbolEntry(kind="model", name="Reservation", path="engine/bookings/models.py", line=4,
                      quote="class Reservation(models.Model):")
    kept_s, kept_r, dropped = gates.boundary_gate(repos, [sym], [good, bad])
    assert kept_s == [sym] and kept_r == [good]
    assert [d["reason"] for d in dropped] == ["quote not at cited lines"]


def test_partition_gate():
    files = ["e/a.py", "e/b.py", "e/c.py"]
    lines = {"e/a.py": 10, "e/b.py": 10, "e/c.py": 50}
    ok = [Unit(id="u1", files=["e/a.py", "e/b.py"], lines=20), Unit(id="u2", files=["e/c.py"], lines=50, oversize=True)]
    assert gates.partition_gate(ok, files, lines, 30).passed
    assert "in no unit" in gates.partition_gate(ok[:1], files, lines, 30).reason
    twice = ok + [Unit(id="u3", files=["e/a.py"], lines=10)]
    assert "is in u1 and u3" in gates.partition_gate(twice, files, lines, 30).reason
    over = [Unit(id="u1", files=files, lines=70)]
    assert "has 70 lines" in gates.partition_gate(over, files, lines, 30).reason


def test_file_coverage_gate_exempts_small_files():
    facts = [Fact(claim="x", citations=[cite("e/a.py", 1, 1, "q")])]
    assert gates.file_coverage_gate(["e/a.py", "e/b.py", "e/c.py"], {"e/a.py": 99, "e/b.py": 30, "e/c.py": 31},
                                    facts) == ["e/c.py"]


def test_boundary_coverage_gate_needs_overlapping_lines():
    e = SymbolEntry(kind="model", name="R", path="e/m.py", line=10, quote="class R")
    assert gates.boundary_coverage_gate([e], [Fact(claim="x", citations=[cite("e/m.py", 8, 12, "q")])]) == []
    assert gates.boundary_coverage_gate([e], [Fact(claim="x", citations=[cite("e/m.py", 11, 12, "q")])]) == ["e/m.py:10"]


def test_doc_quote_gate():
    texts = {"https://d.test/a.md": "POST /api/v1/bookings/:id/ack   acknowledges\na booking revision."}
    assert gates.doc_quote_gate(texts, "https://d.test/a.md", "acknowledges a booking revision").passed
    assert gates.doc_quote_gate(texts, "https://d.test/a.md", "deletes a booking").reason == "doc quote not in the page"
    assert "not in llms.txt" in gates.doc_quote_gate(texts, "https://d.test/b.md", "acknowledges").reason


def test_write_gate(ws, tmp_path):
    corpus = gates.Corpus([ws["engine"]])
    cits = {"c1": cite("engine/bookings/urls.py", 6, 6, "hooks/acme/")}
    ok = "The route [[c1]] calls `webhook` in engine/bookings/urls.py:6."
    assert gates.write_gate(ok, citations=cits, corpus=corpus, labels=set()) == []
    reasons = gates.write_gate("See [[c9]] and engine/bookings/nowhere.py:99 and `teleport_guest`.",
                               citations=cits, corpus=corpus, labels=set())
    assert any("[[c9]]" in r for r in reasons)
    assert any("nowhere.py:99" in r for r in reasons)
    assert any("teleport_guest" in r for r in reasons)
    mermaid = "```mermaid\ngraph LR\n  A[\"views.py\"] --> B[\"warp_drive\"]\n```"
    reasons = gates.write_gate(mermaid, citations=cits, corpus=corpus, labels={"views.py"})
    assert reasons == ["Mermaid label 'warp_drive' is not a file, symbol or repo from the facts"]


def test_glossary_gate(ws):
    corpus = gates.Corpus([ws["engine"]])
    assert gates.glossary_gate("Ospite", corpus).passed
    assert not gates.glossary_gate("prenotazione", corpus).passed


def test_sections_gate():
    md = "# T\n\n## Overview\n\n## 2. Glossary\n\n## Files (12)\n"
    assert gates.sections_gate(md, ["Overview", "Glossary", "Files"]).passed
    assert gates.sections_gate(md, ["Overview", "Coverage"]).reason == "missing sections: Coverage"


def test_target_gate(ws, tmp_path):
    info, _ = target.build_target(ws["engine"] / "bookings", tmp_path)
    assert target.target_gate(info).passed
    empty = info.model_copy(update={"files": []})
    assert "no files" in target.target_gate(empty).reason
    with pytest.raises(target.TargetError, match="not inside a git repository"):
        target.resolve_target(ws["docs"])


def test_corpus_without_rg_names_ripgrep(ws, monkeypatch):
    import shutil
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "rg" else real_which(name, *a, **k))
    with pytest.raises(target.MissingToolError, match="ripgrep"):
        gates.Corpus([ws["engine"]]).contains("Ospite")


@pytest.mark.parametrize("ws_path", [".github/workflows/ci.yml", "./.github/workflows/ci.yml"])
def test_split_path_keeps_the_leading_dot_of_a_repo_key(ws_path):
    repo = RepoInfo(key=".github", path="/x/.github", head_sha="0" * 40)
    assert gates.split_path({".github": repo}, ws_path) == (repo, "workflows/ci.yml")


def test_split_path_strips_only_a_leading_dot_slash():
    repo = RepoInfo(key="shop", path="/x/shop", head_sha="0" * 40)
    assert gates.split_path({"shop": repo}, "./shop/.github/workflows/ci.yml") == (repo, ".github/workflows/ci.yml")
