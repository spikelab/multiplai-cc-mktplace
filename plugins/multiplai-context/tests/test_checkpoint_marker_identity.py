"""The rebuild pointer is keyed by the session's identity, not the writer's.

Three defects, all found on 2026-08-10 while live-verifying 0.39.0. Each is the
same shape: half of the marker's filename was re-derived from whatever process
happened to be writing it, and for two callers that process is not the session.

* **#182 — hostname.** The host drain runs ``checkpoint_writer.py`` in a
  throwaway container after the session's own has exited, so ``$HOSTNAME``
  there is a random Docker id (``ab5d84093a24``) that no future session will
  ever have. Every closed tab left a permanent orphan.
* **#183 — project.** Claude Code's ``cwd`` follows shell navigation, so a
  workspace session that did some work inside a sub-repo filed its pointer
  under the SUB-REPO, and a ``/clear`` from the workspace root missed it and
  fell through to a legacy marker pointing at a *different* session.
* **#181 — collection.** Retirement is attempted once and refuses under a live
  marker, and nothing ever revisited a refusal: 216 directories, 3.5 MB, none
  collected since Jul 7.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from lib import checkpoint as cp
from lib import session_registry

VALID_CHECKPOINT = "\n".join(
    f"## {s}\n- state for {s.lower()}" for s in cp.CHECKPOINT_SECTIONS
)


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATA_DIR", str(data_dir))
    _reset_cache()
    yield data_dir
    _reset_cache()


def _register(data_dir, session_id, cwd, hostname):
    """Write the session's registry entry as its first hook would."""
    old = os.environ.get("HOSTNAME")
    os.environ["HOSTNAME"] = hostname
    try:
        session_registry.record_event(
            data_dir, {"session_id": session_id, "cwd": cwd}, "start"
        )
    finally:
        if old is None:
            os.environ.pop("HOSTNAME", None)
        else:
            os.environ["HOSTNAME"] = old


def _checkpoint(data_dir, session_id):
    cp.write_checkpoint_file(data_dir, session_id, VALID_CHECKPOINT)


# ---------------------------------------------------------------------------
# #182 — the writer's hostname is not the session's
# ---------------------------------------------------------------------------

class TestHostnameBelongsToTheSession:

    def test_a_marker_written_off_host_is_claimable_on_the_session_host(
        self, data_env, monkeypatch
    ):
        """The drain case, end to end. The existing TestPointer cases all
        write and read from the same fake hostname, which is exactly why this
        went unnoticed."""
        _register(data_env, "s1", "/work/proj", "cc-p-09233636")
        _checkpoint(data_env, "s1")

        # The host drain: a throwaway container, writing for a session that
        # lived somewhere else.
        monkeypatch.setenv("HOSTNAME", "ab5d84093a24")
        marker = cp.write_pending_marker(
            data_env, "/work/proj", "s1", 62_816, hostname="cc-p-09233636"
        )
        assert marker.name == "proj__cc-p-09233636.json"

        # A session back on the session's own host finds it.
        monkeypatch.setenv("HOSTNAME", "cc-p-09233636")
        claimed = cp.consume_pending_marker(
            data_env, "/work/proj", "s2", cp.load_config()
        )
        assert claimed is not None
        assert claimed["session_id"] == "s1"
        assert claimed["tokens"] == 62_816

    def test_without_an_explicit_hostname_the_registry_supplies_it(
        self, data_env, monkeypatch
    ):
        """A writer that was never taught to pass the hostname still keys the
        marker correctly, because the registry recorded the session's own."""
        _register(data_env, "s1", "/work/proj", "cc-p-09233636")

        monkeypatch.setenv("HOSTNAME", "ab5d84093a24")
        marker = cp.write_pending_marker(data_env, "/work/proj", "s1", 10)

        assert marker.name == "proj__cc-p-09233636.json"

    def test_with_no_registry_it_falls_back_to_this_process(
        self, data_env, monkeypatch
    ):
        """Vanilla Claude Code with no hub, or a session predating the entry:
        the old behaviour, unchanged."""
        monkeypatch.setenv("HOSTNAME", "laptop")

        marker = cp.write_pending_marker(data_env, "/work/proj", "s1", 10)

        assert marker.name == "proj__laptop.json"

    def test_the_payload_records_the_key_it_was_actually_written_under(
        self, data_env, monkeypatch
    ):
        """``hostname`` in the payload used to be ``$HOSTNAME`` while the
        filename said something else — two answers to one question."""
        _register(data_env, "s1", "/work/proj", "cc-p-09233636")
        monkeypatch.setenv("HOSTNAME", "ab5d84093a24")

        marker = cp.write_pending_marker(data_env, "/work/proj", "s1", 10)
        payload = json.loads(marker.read_text())

        assert payload["hostname"] == "cc-p-09233636"
        assert payload["project"] == "proj"
        assert marker.name == f"{payload['project']}__{payload['hostname']}.json"


# ---------------------------------------------------------------------------
# #183 — the project is where the session started, not where its shell is
# ---------------------------------------------------------------------------

class TestProjectDoesNotFollowCwdDrift:

    def test_a_session_that_wandered_into_a_subrepo_still_finds_its_own(
        self, data_env, monkeypatch
    ):
        """Session 6ed7807b, 2026-08-10: a workspace session that reviewed a PR
        inside PROJECTS/multiplai-cc-mktplace filed its pointer under the
        sub-repo. The `/clear` from the workspace root looked for
        `workspace__<host>.json`, missed, and fell back to a legacy marker
        pointing at session 65b35e19 — a different session entirely."""
        monkeypatch.setenv("HOSTNAME", "cc-p-09233636")
        _register(data_env, "s1", "/work/knowhere", "cc-p-09233636")
        _checkpoint(data_env, "s1")

        # The hook fires with a drifted cwd — this is the write that used to
        # land under the wrong project.
        marker = cp.write_pending_marker(
            data_env, "/work/knowhere/PROJECTS/mktplace", "s1", 217_555
        )
        assert marker.name == "knowhere__cc-p-09233636.json"

        claimed = cp.consume_pending_marker(
            data_env, "/work/knowhere", "s2", cp.load_config()
        )
        assert claimed is not None and claimed["session_id"] == "s1"

    def test_the_claim_side_survives_drift_too(self, data_env, monkeypatch):
        """The compaction path claims mid-session, hours after the marker was
        written, so the claiming session's cwd can have drifted as well."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        _register(data_env, "s1", "/work/knowhere", "host-a")
        _register(data_env, "s2", "/work/knowhere", "host-a")
        cp.write_pending_marker(data_env, "/work/knowhere", "s1", 10)

        claimed = cp.consume_pending_marker(
            data_env, "/work/knowhere/PROJECTS/mktplace", "s2", cp.load_config()
        )

        assert claimed is not None and claimed["session_id"] == "s1"

    def test_two_genuinely_different_projects_still_get_different_markers(
        self, data_env, monkeypatch
    ):
        """Pinning must not collapse projects — that would reintroduce the
        clobber hostname keying exists to prevent."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        _register(data_env, "s1", "/work/alpha", "host-a")
        _register(data_env, "s2", "/work/beta", "host-a")

        first = cp.write_pending_marker(data_env, "/work/alpha", "s1", 10)
        second = cp.write_pending_marker(data_env, "/work/beta", "s2", 10)

        assert first != second
        assert first.exists() and second.exists()


# ---------------------------------------------------------------------------
# #181 — the store is collected
# ---------------------------------------------------------------------------

def _age(path, days):
    """Backdate every file in *path* (and *path* itself) by *days*."""
    when = time.time() - days * 86_400
    for p in sorted(path.rglob("*"), reverse=True):
        os.utime(p, (when, when))
    os.utime(path, (when, when))


class TestSweep:

    def test_an_expired_marker_stops_pinning_its_checkpoint(
        self, data_env, monkeypatch
    ):
        """The whole mechanism in one case: the walk-away session. It crossed
        the handoff threshold, the tab was closed instead of cleared, and the
        unconsumed marker refused retirement forever."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        marker = cp.write_pending_marker(data_env, "/work/proj", "s1", 214_000)
        _checkpoint(data_env, "s1")
        payload = json.loads(marker.read_text())
        payload["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).isoformat()
        marker.write_text(json.dumps(payload))
        _age(cp.session_dir(data_env, "s1"), 30)

        expired, collected = cp.sweep_checkpoints(data_env, cp.load_config())

        assert (expired, collected) == (1, 1)
        assert not marker.exists()
        assert not cp.session_dir(data_env, "s1").exists()

    def test_a_live_marker_still_protects_its_checkpoint(
        self, data_env, monkeypatch
    ):
        """Explicitly out of scope: ``pending_marker_owner`` is not relaxed.
        Inside ``ttl_hours`` the checkpoint survives however old it is."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        cp.write_pending_marker(data_env, "/work/proj", "s1", 214_000)
        _checkpoint(data_env, "s1")
        _age(cp.session_dir(data_env, "s1"), 30)

        expired, collected = cp.sweep_checkpoints(data_env, cp.load_config())

        assert (expired, collected) == (0, 0)
        assert cp.session_dir(data_env, "s1").exists()

    def test_a_recent_checkpoint_is_left_alone(self, data_env, monkeypatch):
        monkeypatch.setenv("HOSTNAME", "host-a")
        _checkpoint(data_env, "s1")

        assert cp.sweep_checkpoints(data_env, cp.load_config()) == (0, 0)
        assert cp.session_dir(data_env, "s1").exists()

    def test_a_quiet_but_live_session_keeps_its_checkpoint(
        self, data_env, monkeypatch
    ):
        """A session can write one checkpoint and then go a long time without
        another. Its registry ``last_event`` is restamped on every Stop, so
        that — not the file mtime alone — is what says it is still alive."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        _checkpoint(data_env, "s1")
        _age(cp.session_dir(data_env, "s1"), 30)
        _register(data_env, "s1", "/work/proj", "host-a")

        assert cp.sweep_checkpoints(data_env, cp.load_config()) == (0, 0)
        assert cp.session_dir(data_env, "s1").exists()

    def test_a_marker_with_an_unreadable_payload_still_ages_out(
        self, data_env, monkeypatch
    ):
        """A truncated write must not pin a checkpoint forever — mtime is the
        fallback clock."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        marker = cp.write_pending_marker(data_env, "/work/proj", "s1", 10)
        marker.write_text("{not json")
        _age(marker.parent, 2)

        expired, _ = cp.sweep_checkpoints(data_env, cp.load_config())

        assert expired == 1 and not marker.exists()

    def test_gc_days_zero_keeps_everything(self, data_env, monkeypatch):
        """The pre-0.40 behaviour is still reachable — collection deletes
        session working state, so it has to be switchable off."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_GC_DAYS", "0")
        _checkpoint(data_env, "s1")
        _age(cp.session_dir(data_env, "s1"), 365)

        assert cp.sweep_checkpoints(data_env, cp.load_config()) == (0, 0)
        assert cp.session_dir(data_env, "s1").exists()

    def test_the_pending_directory_is_never_mistaken_for_a_session(
        self, data_env, monkeypatch
    ):
        monkeypatch.setenv("HOSTNAME", "host-a")
        cp.write_pending_marker(data_env, "/work/proj", "s1", 10)
        _age(cp.checkpoints_root(data_env) / "pending", 365)

        cp.sweep_checkpoints(data_env, cp.load_config())

        assert (cp.checkpoints_root(data_env) / "pending").is_dir()

    def test_the_sweep_never_raises_on_an_empty_store(self, data_env):
        assert cp.sweep_checkpoints(data_env, cp.load_config()) == (0, 0)
