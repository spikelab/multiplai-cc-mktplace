"""Tests for the vendored-prompt staleness check.

Everything here runs against a temporary fixture manifest and a stubbed fetch.
Nothing touches the network and nothing reads the real SOURCES.json — a test
that needs GitHub is a test that fails on a plane, and one that reads the real
manifest goes red the day upstream legitimately changes.
"""

import json
import subprocess

import pytest

from check_vendored_prompts import (
    FetchError,
    ManifestError,
    VendoredSource,
    check_sources,
    exit_code_for,
    fetch_blob_sha,
    load_manifest,
    main,
)

REPO = "anthropics/claude-plugins-official"
CODE_REVIEWER = "plugins/pr-review-toolkit/agents/code-reviewer.md"
HUNTER = "plugins/pr-review-toolkit/agents/silent-failure-hunter.md"

SHA_CODE_REVIEWER = "834b70c21f1f1bd4d01b8025bc830bf00887f2e7"
SHA_HUNTER = "b8a8dfa41e18ef6ac801ae64be38b2508aa04f44"
SHA_TREE = "d409052c02b7f3a894ae315a665b88df3d8a677c"


def _entry(path: str, blob_sha: str, used_by: list[str], **overrides) -> dict:
    entry = {
        "repo": REPO,
        "path": path,
        "blob_sha": blob_sha,
        "tree_sha": SHA_TREE,
        "licence": "Apache-2.0",
        "modified": True,
        "used_by": used_by,
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def manifest(tmp_path):
    """A two-entry fixture manifest shaped exactly like the real SOURCES.json."""
    path = tmp_path / "SOURCES.json"
    path.write_text(
        json.dumps(
            [
                _entry(CODE_REVIEWER, SHA_CODE_REVIEWER, ["CODE_REVIEW_PROMPT"]),
                _entry(HUNTER, SHA_HUNTER, ["SILENT_FAILURE_PROMPT"]),
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _fetch_returning(shas: dict[str, str]):
    """Stub fetch: hand back whatever SHA the test says upstream has now."""

    def fetch(source: VendoredSource) -> tuple[str, int]:
        return shas[source.path], 1

    return fetch


def _fetch_failing(errors: dict[str, FetchError], shas: dict[str, str] | None = None):
    def fetch(source: VendoredSource) -> tuple[str, int]:
        if source.path in errors:
            raise errors[source.path]
        return (shas or {})[source.path], 1

    return fetch


class TestCleanRun:
    def test_every_sha_matching_exits_zero(self, manifest, capsys):
        code = main(
            ["--manifest", str(manifest)],
            fetch=_fetch_returning({CODE_REVIEWER: SHA_CODE_REVIEWER, HUNTER: SHA_HUNTER}),
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "2 unchanged, 0 drifted, 0 not checked" in out
        assert "DRIFT" not in out

    def test_json_mode_reports_clean(self, manifest, capsys):
        code = main(
            ["--manifest", str(manifest), "--json"],
            fetch=_fetch_returning({CODE_REVIEWER: SHA_CODE_REVIEWER, HUNTER: SHA_HUNTER}),
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["checked"] == 2
        assert payload["drifted"] == 0
        assert payload["exit_code"] == 0
        assert {r["status"] for r in payload["results"]} == {"ok"}


class TestDriftDetected:
    def test_changed_sha_exits_nonzero_and_names_the_path(self, manifest, capsys):
        code = main(
            ["--manifest", str(manifest)],
            fetch=_fetch_returning({CODE_REVIEWER: "0" * 40, HUNTER: SHA_HUNTER}),
        )
        assert code == 1
        out = capsys.readouterr().out
        assert "DRIFT" in out
        assert CODE_REVIEWER in out
        assert SHA_CODE_REVIEWER in out  # the recorded sha
        assert "0" * 40 in out  # what upstream has now
        assert "1 unchanged, 1 drifted, 0 not checked" in out

    def test_drift_names_the_prompts_that_use_it(self, manifest, capsys):
        main(
            ["--manifest", str(manifest)],
            fetch=_fetch_returning({CODE_REVIEWER: "0" * 40, HUNTER: SHA_HUNTER}),
        )
        out = capsys.readouterr().out
        assert "CODE_REVIEW_PROMPT" in out
        assert "local copy is modified" in out

    def test_json_mode_reports_drift(self, manifest, capsys):
        code = main(
            ["--manifest", str(manifest), "--json"],
            fetch=_fetch_returning({CODE_REVIEWER: "0" * 40, HUNTER: SHA_HUNTER}),
        )
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["drifted"] == 1
        drifted = [r for r in payload["results"] if r["status"] == "drift"]
        assert len(drifted) == 1
        assert drifted[0]["path"] == CODE_REVIEWER
        assert drifted[0]["expected_blob_sha"] == SHA_CODE_REVIEWER
        assert drifted[0]["actual_blob_sha"] == "0" * 40
        assert drifted[0]["used_by"] == ["CODE_REVIEW_PROMPT"]


class TestManifestProblems:
    def test_missing_manifest_is_a_clear_error_not_a_traceback(self, tmp_path, capsys):
        missing = tmp_path / "nope" / "SOURCES.json"
        code = main(["--manifest", str(missing)], fetch=_fetch_returning({}))
        assert code == 2
        captured = capsys.readouterr()
        assert "manifest not found" in captured.err
        assert str(missing) in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""

    def test_missing_manifest_in_json_mode(self, tmp_path, capsys):
        missing = tmp_path / "SOURCES.json"
        code = main(["--manifest", str(missing), "--json"], fetch=_fetch_returning({}))
        assert code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["exit_code"] == 2
        assert "manifest not found" in payload["error"]

    def test_malformed_json_is_reported(self, tmp_path, capsys):
        bad = tmp_path / "SOURCES.json"
        bad.write_text("[{oops}]", encoding="utf-8")
        assert main(["--manifest", str(bad)], fetch=_fetch_returning({})) == 2
        assert "not valid JSON" in capsys.readouterr().err

    def test_entry_missing_a_contract_key_is_reported(self, tmp_path):
        bad = tmp_path / "SOURCES.json"
        entry = _entry(CODE_REVIEWER, SHA_CODE_REVIEWER, ["X"])
        del entry["blob_sha"]
        del entry["used_by"]
        bad.write_text(json.dumps([entry]), encoding="utf-8")
        with pytest.raises(ManifestError) as exc:
            load_manifest(bad)
        assert "blob_sha" in str(exc.value)
        assert "used_by" in str(exc.value)

    def test_empty_manifest_is_reported(self, tmp_path):
        empty = tmp_path / "SOURCES.json"
        empty.write_text("[]", encoding="utf-8")
        with pytest.raises(ManifestError):
            load_manifest(empty)

    def test_manifest_round_trips_the_contract(self, manifest):
        sources = load_manifest(manifest)
        assert [s.path for s in sources] == [CODE_REVIEWER, HUNTER]
        assert sources[0].repo == REPO
        assert sources[0].blob_sha == SHA_CODE_REVIEWER
        assert sources[0].tree_sha == SHA_TREE
        assert sources[0].licence == "Apache-2.0"
        assert sources[0].modified is True
        assert sources[0].used_by == ("CODE_REVIEW_PROMPT",)


class TestFetchFailure:
    def test_failed_fetch_is_reported_not_treated_as_unchanged(self, manifest, capsys):
        code = main(
            ["--manifest", str(manifest)],
            fetch=_fetch_failing(
                {CODE_REVIEWER: FetchError(status=404, message="gh: Not Found (HTTP 404)")},
                {HUNTER: SHA_HUNTER},
            ),
        )
        assert code == 3
        out = capsys.readouterr().out
        assert "ERROR" in out
        assert CODE_REVIEWER in out
        assert "HTTP 404" in out
        assert "moved or was deleted" in out
        assert "1 unchanged, 0 drifted, 1 not checked" in out
        assert "NOT 'unchanged'" in out

    def test_failed_fetch_never_counts_as_ok_in_json(self, manifest, capsys):
        main(
            ["--manifest", str(manifest), "--json"],
            fetch=_fetch_failing(
                {CODE_REVIEWER: FetchError(status=403, message="gh: Forbidden (HTTP 403)")},
                {HUNTER: SHA_HUNTER},
            ),
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["errors"] == 1
        assert payload["unchanged"] == 1
        errored = [r for r in payload["results"] if r["status"] == "error"]
        assert errored[0]["actual_blob_sha"] is None
        assert "bot wall" in errored[0]["hint"]

    def test_drift_outranks_a_fetch_failure_in_the_exit_code(self, manifest):
        results = check_sources(
            load_manifest(manifest),
            fetch=_fetch_failing(
                {HUNTER: FetchError(status=500, message="gh: (HTTP 500)")},
                {CODE_REVIEWER: "0" * 40},
            ),
        )
        assert exit_code_for(results) == 1


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestFetchMechanics:
    def _source(self) -> VendoredSource:
        return VendoredSource(
            repo=REPO,
            path=CODE_REVIEWER,
            blob_sha=SHA_CODE_REVIEWER,
            tree_sha=SHA_TREE,
            licence="Apache-2.0",
            modified=True,
            used_by=("CODE_REVIEW_PROMPT",),
        )

    def test_reads_the_sha_from_gh_api(self):
        seen: list[list[str]] = []

        def runner(argv):
            seen.append(argv)
            return _proc(0, stdout=SHA_CODE_REVIEWER + "\n")

        sha, attempts = fetch_blob_sha(self._source(), runner=runner, sleep=lambda _: None)
        assert sha == SHA_CODE_REVIEWER
        assert attempts == 1
        assert seen[0][0:2] == ["gh", "api"]
        assert f"repos/{REPO}/contents/{CODE_REVIEWER}" in seen[0]
        # No token is ever assembled into the argv.
        assert not any("token" in part.lower() for part in seen[0])

    def test_404_is_not_retried(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return _proc(1, stderr="gh: Not Found (HTTP 404)\n")

        with pytest.raises(FetchError) as exc:
            fetch_blob_sha(self._source(), runner=runner, retries=2, sleep=lambda _: None)
        assert exc.value.status == 404
        assert len(calls) == 1

    def test_403_is_not_retried(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return _proc(1, stderr="gh: Forbidden (HTTP 403)\n")

        with pytest.raises(FetchError) as exc:
            fetch_blob_sha(self._source(), runner=runner, retries=2, sleep=lambda _: None)
        assert exc.value.status == 403
        assert len(calls) == 1

    def test_5xx_is_retried_verbatim(self):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            if len(calls) < 3:
                return _proc(1, stderr="gh: Server Error (HTTP 503)\n")
            return _proc(0, stdout=SHA_CODE_REVIEWER)

        sha, attempts = fetch_blob_sha(self._source(), runner=runner, retries=2, sleep=lambda _: None)
        assert sha == SHA_CODE_REVIEWER
        assert attempts == 3
        assert calls[0] == calls[1] == calls[2]  # verbatim, not reshaped

    def test_dns_or_timeout_is_retried_then_reported(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return _proc(1, stderr="dial tcp: lookup api.github.com: no such host\n")

        with pytest.raises(FetchError) as exc:
            fetch_blob_sha(self._source(), runner=runner, retries=2, sleep=lambda _: None)
        assert exc.value.status is None
        assert len(calls) == 3

    def test_missing_gh_binary_is_a_fetch_error(self):
        def runner(argv):
            raise FileNotFoundError("gh")

        with pytest.raises(FetchError) as exc:
            fetch_blob_sha(self._source(), runner=runner, sleep=lambda _: None)
        assert "not found on PATH" in exc.value.message

    def test_empty_sha_is_a_fetch_error_not_a_match(self):
        with pytest.raises(FetchError):
            fetch_blob_sha(self._source(), runner=lambda argv: _proc(0, stdout="\n"), sleep=lambda _: None)
