"""Each gate's pass and fail cases. Gates re-read the commit; none calls a model."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from walkthrough_pipeline import gates, target
from walkthrough_pipeline.models import Citation, Fact, Hop, Reference, RepoInfo, SymbolEntry, Unit

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


def _webhook_hops(ws, *, second_symbol="ack_booking"):
    views = (ws["engine"] / "bookings/views.py").read_text().split("\n")
    client = (ws["engine"] / "bookings/client.py").read_text().split("\n")
    vd = next(i for i, l in enumerate(views, 1) if "def webhook" in l)
    vr = next(i for i, l in enumerate(views, 1) if "return ack_booking" in l)
    cd = next(i for i, l in enumerate(client, 1) if "def ack_booking" in l)
    cr = next(i for i, l in enumerate(client, 1) if "httpx.post" in l)
    return [
        Hop(symbol_called="hooks/acme/", path="engine/bookings/urls.py", line_start=6, line_end=6,
            quote="path('hooks/acme/', views.webhook"),
        Hop(symbol_called="views.webhook", path="engine/bookings/views.py", line_start=vd, line_end=vr,
            quote="return ack_booking(booking_id)"),
        Hop(symbol_called=second_symbol, path="engine/bookings/client.py", line_start=cd, line_end=cr,
            quote="httpx.post("),
    ]


SEED = SymbolEntry(kind="url", name="hooks/acme/", path="engine/bookings/urls.py", line=6,
                   quote="path('hooks/acme/', views.webhook, name='acme_webhook'),")


def test_trace_gate_passes_a_chain(repos, ws):
    assert gates.trace_gate(repos, SEED, _webhook_hops(ws), []) == (3, "")


def test_trace_gate_stops_at_a_broken_hop(repos, ws):
    good, reason = gates.trace_gate(repos, SEED, _webhook_hops(ws, second_symbol="send_mail"), [])
    assert good == 2 and "does not contain" in reason


def test_trace_gate_wants_hop_one_to_be_the_seed(repos, ws):
    hops = _webhook_hops(ws)[1:]
    assert gates.trace_gate(repos, SEED, hops, [])[0] == 0


def test_trace_gate_accepts_a_cross_repo_edge(repos):
    seed = Reference(repo="front", kind="http-route", symbol="hooks/acme/", path="front/src/api.ts", line=5,
                     quote="return fetch(`${BASE}/hooks/acme/`", method="ast-grep")
    hops = [Hop(symbol_called="notify", path="front/src/api.ts", line_start=5, line_end=5, quote="/hooks/acme/"),
            Hop(symbol_called="hooks/acme/", path="engine/bookings/urls.py", line_start=6, line_end=6,
                quote="views.webhook")]
    assert gates.trace_gate(repos, seed, hops, [("engine/bookings/urls.py", 6)]) == (2, "")
    # A hop that neither defines the name nor holds a boundary entry breaks the chain.
    elsewhere = [hops[0], hops[1].model_copy(update={"path": "engine/bookings/views.py", "line_start": 5,
                                                     "line_end": 5, "quote": "def webhook(request):"})]
    assert gates.trace_gate(repos, seed, elsewhere, [])[0] == 1
    assert gates.trace_gate(repos, seed, elsewhere, [("engine/bookings/views.py", 5)])[0] == 2


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
