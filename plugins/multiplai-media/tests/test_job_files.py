"""The review page's input files (stages/job_files.py and skills/video-edit/schema/)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_job
from stages import job_files as jf


@pytest.fixture
def job(tmp_path: Path) -> dict[str, Path]:
    return make_job.build(tmp_path / "job", video=False)


@pytest.fixture
def media(tmp_path: Path) -> dict[str, str]:
    d = tmp_path / "media"
    d.mkdir()
    names = (
        "style.md",
        "sample.html",
        "brand.json",
        "bed.m4a",
        "whoosh.wav",
        "pop.wav",
    )
    for n in names:
        (d / n).write_bytes(b"x")
    return {n.split(".")[0]: str(d / n) for n in names}


def _looks(m: dict[str, str]) -> dict:
    return {
        "styles": [{"name": "clean", "file": m["style"], "sample_html": m["sample"]}],
        "brands": [{"name": "house", "file": m["brand"]}],
        "captions": [
            {
                "name": "big",
                "values": {
                    "size": 72,
                    "position_y": 0.7,
                    "words_per_line": 3,
                    "highlight": True,
                },
            }
        ],
    }


def _music(m: dict[str, str]) -> dict:
    return {
        "beds": [{"name": "calm", "file": m["bed"]}],
        "sfx_sets": [{"name": "soft", "files": [m["whoosh"], m["pop"]]}],
        "voice_sample": {"start": 10.0, "end": 18.0},
    }


def _write(outdir: Path, name: str, data) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / jf.INPUTS[name]).write_text(json.dumps(data))


# --- each schema: a valid file, and one invalid case ---------------------------------


def test_fixture_job_and_candidates_are_valid(job) -> None:
    for name in ("job", "candidates"):
        got = jf.load(job["dir"], name)
        assert got["status"] == "ok", got.get("error")
    assert [c["id"] for c in jf.load(job["dir"], "candidates")["data"]] == [
        "c1",
        "c2",
        "c3",
        "c4",
    ]


def test_fixture_looks_and_music_are_valid(job) -> None:
    looks, music = jf.load(job["dir"], "looks"), jf.load(job["dir"], "music")
    assert looks["status"] == "ok", looks.get("error")
    assert music["status"] == "ok", music.get("error")
    assert [s["name"] for s in looks["data"]["styles"]] == ["clean-tech", "flat-vector"]
    for s in looks["data"]["styles"]:  # each style's Example page is its sample
        assert Path(s["sample_html"]).read_text().startswith("<!doctype html>")
    assert [b["name"] for b in music["data"]["beds"]] == ["calm", "bright"]
    vs = music["data"]["voice_sample"]
    assert 0 < vs["end"] - vs["start"] <= 10


def test_fixture_without_offers_has_no_looks_or_music(tmp_path) -> None:
    job = make_job.build(tmp_path / "bare", video=False, offers=False)
    assert jf.load(job["dir"], "looks")["status"] == "missing"
    assert jf.load(job["dir"], "music")["status"] == "missing"


def test_looks_and_music_valid(tmp_path, media) -> None:
    _write(tmp_path, "looks", _looks(media))
    _write(tmp_path, "music", _music(media))
    assert jf.load(tmp_path, "looks")["status"] == "ok"
    assert jf.load(tmp_path, "music")["status"] == "ok"


def test_looks_and_music_lists_are_optional(tmp_path) -> None:
    _write(tmp_path, "looks", {})
    _write(tmp_path, "music", {"beds": []})
    assert jf.load(tmp_path, "looks")["status"] == "ok"
    assert jf.load(tmp_path, "music")["status"] == "ok"


def test_job_kind_must_be_reels_or_demo(job) -> None:
    data = json.loads(job["job"].read_text())
    data["kind"] = "podcast"
    job["job"].write_text(json.dumps(data))
    got = jf.load(job["dir"], "job")
    assert got["status"] == "invalid"
    assert got["error"] == '$.kind: must be one of "reels", "demo"'


def test_job_paths_must_be_absolute(job) -> None:
    data = json.loads(job["job"].read_text())
    data["source"] = "source.mp4"
    job["job"].write_text(json.dumps(data))
    assert jf.load(job["dir"], "job")["error"] == "$.source: must be an absolute path"


def test_job_names_a_missing_file(job) -> None:
    job["source"].unlink()
    got = jf.load(job["dir"], "job")
    assert got["status"] == "invalid"
    assert got["error"] == f"file not found: {job['source']}"


def test_candidate_score_out_of_range(job) -> None:
    data = json.loads(job["candidates"].read_text())
    data[1]["scores"]["hook"] = 6
    job["candidates"].write_text(json.dumps(data))
    assert (
        jf.load(job["dir"], "candidates")["error"] == "$[1].scores.hook: must be <= 5"
    )


def test_candidate_end_before_start_and_duplicate_ids(job) -> None:
    data = json.loads(job["candidates"].read_text())
    data[0]["end"] = data[0]["start"]
    data[2]["id"] = "c2"
    job["candidates"].write_text(json.dumps(data))
    assert jf.load(job["dir"], "candidates")["error"].split("\n") == [
        "$[0]: end must be after start",
        "$[2].id: 'c2' is used twice",
    ]


def test_looks_caption_values_have_their_types(tmp_path, media) -> None:
    looks = _looks(media)
    looks["captions"][0]["values"]["highlight"] = "yes"
    looks["captions"][0]["values"]["colour"] = "red"
    _write(tmp_path, "looks", looks)
    assert jf.load(tmp_path, "looks")["error"].split("\n") == [
        "$.captions[0].values.highlight: expected boolean",
        "$.captions[0].values: unknown field 'colour'",
    ]


def test_music_voice_sample_and_duplicate_names(tmp_path, media) -> None:
    music = _music(media)
    music["voice_sample"] = {"start": 5, "end": 5}
    music["beds"].append({"name": "calm", "file": media["bed"]})
    _write(tmp_path, "music", music)
    assert jf.load(tmp_path, "music")["error"].split("\n") == [
        "$.beds: name 'calm' is used twice",
        "$.voice_sample: end must be after start",
    ]


def test_music_sfx_set_needs_a_file(tmp_path, media) -> None:
    music = _music(media)
    music["sfx_sets"][0]["files"] = []
    _write(tmp_path, "music", music)
    assert (
        jf.load(tmp_path, "music")["error"]
        == "$.sfx_sets[0].files: needs at least 1 item(s)"
    )


# --- the checker -------------------------------------------------------------------------


def test_missing_file_and_bad_json(tmp_path) -> None:
    assert jf.load(tmp_path, "looks")["status"] == "missing"
    (tmp_path / "music.json").write_text("{not json")
    got = jf.load(tmp_path, "music")
    assert got["status"] == "invalid" and got["error"].startswith(
        "music.json is not valid JSON"
    )


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "3"])
def test_numbers_are_finite_and_not_booleans(value) -> None:
    assert jf.check(value, {"type": "number"}) == ["$: expected number"]


def test_integer_rejects_a_fraction_and_a_boolean() -> None:
    assert jf.check(2.5, {"type": "integer"}) == ["$: expected integer"]
    assert jf.check(False, {"type": "integer"}) == ["$: expected integer"]
    assert jf.check(3, {"type": "integer"}) == []


def test_errors_are_capped(tmp_path) -> None:
    _write(tmp_path, "candidates", [{}])
    lines = jf.load(tmp_path, "candidates")["error"].split("\n")
    assert len(lines) == jf.MAX_ERRORS + 1 and lines[-1] == "… and 3 more"


def test_an_unsupported_keyword_raises_instead_of_being_ignored() -> None:
    with pytest.raises(ValueError, match="pattern"):
        jf.check("x", {"type": "string", "pattern": "^a"})


@pytest.mark.parametrize("name", sorted(jf.INPUTS))
def test_every_shipped_schema_uses_only_supported_keywords(name) -> None:
    # Walk every node: check() raises on an unknown keyword only where it visits.
    def walk(node):
        if isinstance(node, dict):
            unknown = set(node) - jf._KNOWN
            assert not unknown, f"{name}: {unknown}"
            for key in ("properties", "$defs"):
                for child in node.get(key, {}).values():
                    walk(child)
            for key in ("items", "additionalProperties"):
                if isinstance(node.get(key), dict):
                    walk(node[key])

    walk(jf.schema(name))


def test_named_files_lists_only_what_valid_files_name(tmp_path, job, media) -> None:
    _write(job["dir"], "looks", _looks(media))
    _write(job["dir"], "music", _music(media))
    named = jf.named_files(job["dir"])
    assert named["source"] == {job["source"].resolve()}
    assert named["audio"] == {
        Path(media[k]).resolve() for k in ("bed", "whoosh", "pop")
    }
    assert named["sample"] == {Path(media["sample"]).resolve()}
    music = _music(media)
    music["beds"][0]["file"] = "relative.m4a"
    _write(job["dir"], "music", music)
    assert jf.named_files(job["dir"])["audio"] == set()
