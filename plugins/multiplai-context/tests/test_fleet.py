"""Unit tests for lib/fleet.py — the AGENTS.md aggregator.

Everything here runs against synthetic registry entries and checkpoints in a
``tmp_path``. **Never against the real `.multiplai/`** — an agent contaminated
the live workspace that way on 2026-07-30.

The properties worth breaking a build over:

* the outputs are a **cache** — delete them, re-run, get the same bytes back
  (everything else in the plan rests on `sessions/` + `checkpoints/` staying
  the only source of truth);
* a session with no checkpoint still renders, because most sessions never
  cross a checkpoint token band and a fleet view that silently drops them is
  worse than none;
* "needs you" comes from the registry's contracted `waiting_input`, not from a
  second, parallel notion of the same thing;
* collisions are real set intersections — one shared file means exactly one
  line, and disjoint work means silence.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import SCRIPTS_DIR
from lib import checkpoint as cp
from lib import fleet


NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def make_session(
    data_dir,
    sid,
    *,
    kind="stop",
    ago=timedelta(minutes=5),
    project="knowhere",
    hostname="cc-abc123",
    cwd="/work/knowhere",
    now=NOW,
):
    """Write one registry entry, shaped as the lifecycle hooks write it."""
    d = data_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    ts = (now - ago).isoformat()
    (d / f"{sid}.json").write_text(json.dumps({
        "session_id": sid,
        "hostname": hostname,
        "cwd": cwd,
        "project": project,
        "workspace": "/work",
        "started_at": ts,
        "last_event": {"ts": ts, "kind": kind},
    }))
    return d / f"{sid}.json"


def make_checkpoint(data_dir, sid, *, intent="Doing a thing", nxt="Do the next thing",
                    files=("/work/knowhere/src/a.py",)):
    """Write a checkpoint with the three sections the fleet view reads.

    ``files`` should be **absolute** paths — that is the format the checkpoint
    writer mandates ('Involved files': absolute paths), and the format guard
    below pins. Relative-path fixtures once concealed that raw string
    intersection could not see the two-worktrees collision case.
    """
    d = data_dir / "checkpoints" / sid
    d.mkdir(parents=True, exist_ok=True)
    body = [f"## {s}\n\nplaceholder\n" for s in cp.CHECKPOINT_SECTIONS]
    text = "".join(body)
    text = text.replace("## Current intent\n\nplaceholder\n",
                        f"## Current intent\n\n{intent}\n")
    text = text.replace("## Next action\n\nplaceholder\n",
                        f"## Next action\n\n{nxt}\n")
    listing = "\n".join(f"- `{f}` — because reasons" for f in files)
    text = text.replace("## Involved files\n\nplaceholder\n",
                        f"## Involved files\n\n{listing}\n")
    (d / "checkpoint.md").write_text(text)
    return d / "checkpoint.md"


# ---------------------------------------------------------------------------
# The section names this module reads
# ---------------------------------------------------------------------------

def test_the_sections_read_are_sections_the_writer_emits():
    """fleet.py hardcodes three section names rather than importing the tuple,
    so the only thing stopping a rename from silently blanking every entry is
    this assertion."""
    for name in (fleet._INTENT, fleet._NEXT, fleet._FILES):
        assert name in cp.CHECKPOINT_SECTIONS


def test_the_writer_still_mandates_absolute_involved_files():
    """Collision detection normalizes per-agent absolute paths to
    repo-relative ones, and every collision fixture here uses absolute paths
    — both on the strength of `checkpoint_writer.py`'s prompt rule
    "'Involved files': absolute paths". This pins that rule the same way the
    section-name guard above pins the section names: if the prompt drops or
    rewords the mandate, the assumption breaks loudly here rather than the
    fixtures silently drifting from the writer again (which is exactly what
    concealed the two-worktrees collision gap until 2026-08)."""
    src = (SCRIPTS_DIR / "checkpoint_writer.py").read_text(encoding="utf-8")

    assert "'Involved files': absolute paths" in src


# ---------------------------------------------------------------------------
# Criterion 6 — a session with no checkpoint is a valid entry
# ---------------------------------------------------------------------------

class TestRegistryOnlyEntries:

    def test_session_without_a_checkpoint_still_renders(self, tmp_path):
        make_session(tmp_path, "sid-nocp", project="lonely")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "lonely" in md
        assert "No checkpoint" in md

    def test_it_is_not_silently_dropped(self, tmp_path):
        make_session(tmp_path, "sid-nocp")

        agents = fleet.collect(tmp_path, NOW).agents

        assert [a.session_id for a in agents] == ["sid-nocp"]
        assert agents[0].has_checkpoint is False
        assert agents[0].intent == ""

    def test_registry_fields_survive_the_missing_checkpoint(self, tmp_path):
        make_session(tmp_path, "sid-nocp", hostname="cc-zz", cwd="/work/thing")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "cc-zz" in md
        assert "/work/thing" in md

    def test_an_empty_data_dir_renders_an_empty_fleet(self, tmp_path):
        f = fleet.collect(tmp_path, NOW)

        assert f.agents == []
        assert f.fronts == []
        assert "# Agents" in fleet.render_agents_md(f, NOW)

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path):
        make_session(tmp_path, "good")
        (tmp_path / "sessions" / "junk.json").write_text("{not json")
        (tmp_path / "sessions" / "list.json").write_text("[1, 2, 3]")

        agents = fleet.collect(tmp_path, NOW).agents

        assert [a.session_id for a in agents] == ["good"]

    def test_a_truncated_checkpoint_yields_whatever_it_has(self, tmp_path):
        make_session(tmp_path, "sid")
        d = tmp_path / "checkpoints" / "sid"
        d.mkdir(parents=True)
        (d / "checkpoint.md").write_text("## Current intent\n\nHalf-written when")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.has_checkpoint is True
        assert agent.intent == "Half-written when"
        assert agent.next_action == ""


# ---------------------------------------------------------------------------
# Status — derived from the registry's contracted vocabulary
# ---------------------------------------------------------------------------

class TestStatus:

    def test_notification_means_needs_you(self, tmp_path):
        make_session(tmp_path, "waiting", kind="notification", project="blocked")

        f = fleet.collect(tmp_path, NOW)

        assert f.agents[0].status == "waiting_input"
        assert "## Needs you (1)" in fleet.render_agents_md(f, NOW)

    def test_a_two_week_old_notification_is_idle_not_needs_you(self, tmp_path):
        """Containers die without a SessionEnd (reboot, docker kill, OOM), so
        the registry holds entries frozen mid-notification for weeks. Counting
        those as "needs you" produced 24 of them against the real registry —
        a list nobody can act on, which is a list nobody reads."""
        make_session(tmp_path, "ghost", kind="notification", ago=timedelta(days=14))

        f = fleet.collect(tmp_path, NOW)

        assert f.agents[0].status == "idle"
        assert "## Needs you" not in fleet.render_agents_md(f, NOW)

    def test_recent_activity_means_working(self, tmp_path):
        make_session(tmp_path, "busy", kind="stop", ago=timedelta(hours=2))

        assert fleet.collect(tmp_path, NOW).agents[0].status == "working"

    def test_quiet_for_half_a_day_means_idle(self, tmp_path):
        """12h, not 24h: a day is long enough for every container from the
        previous evening to still be claiming a slot under "Working"."""
        make_session(tmp_path, "quiet", kind="stop", ago=timedelta(hours=13))

        f = fleet.collect(tmp_path, NOW)

        assert f.agents[0].status == "idle"
        assert "1 idle, not listed" in fleet.render_agents_md(f, NOW)

    def test_end_means_ended_regardless_of_recency(self, tmp_path):
        make_session(tmp_path, "over", kind="end", ago=timedelta(minutes=1))

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.status == "ended"
        assert agent.live is False

    def test_the_four_statuses_are_the_contracted_ones(self):
        """`working | waiting_input | idle | ended` is frozen in the
        multiplai-gui API contract. This view must not coin a fifth."""
        for kind, ago in (("notification", timedelta(minutes=1)),
                          ("stop", timedelta(minutes=1)),
                          ("start", timedelta(days=3)),
                          ("end", timedelta(minutes=1))):
            assert fleet._status_of(kind, NOW - ago, NOW) in {
                "working", "waiting_input", "idle", "ended"
            }


# ---------------------------------------------------------------------------
# Fronts vs. live — what the one-line reading counts
# ---------------------------------------------------------------------------

class TestFronts:
    """`AGENTS.md` lists everything on the board; `Fleet.fronts` counts only
    what has a claim on you. Folding idle tabs into that count is what turned
    one running session into "36 fronts" — the number appeared to answer "how
    many agents am I running" while answering "how many tabs have I opened
    lately"."""

    def test_an_idle_session_is_live_but_not_a_front(self, tmp_path):
        make_session(tmp_path, "quiet", ago=timedelta(days=3))

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.live is True
        assert agent.front is False

    def test_an_idle_session_is_counted_but_not_listed(self, tmp_path):
        """Idle is a guess at death, not a queue: past the quiet threshold
        these are overwhelmingly closed terminals. They were ten to one against
        the fronts, so listing them *was* the file. The count stays so that a
        fleet which has gone entirely quiet still says so."""
        make_session(tmp_path, "quiet", project="forgotten", ago=timedelta(days=3))

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "## Idle" not in md
        assert "forgotten" not in md
        assert "1 idle, not listed" in md

    def test_an_all_idle_fleet_has_no_fronts(self, tmp_path):
        for i in range(9):
            make_session(tmp_path, f"quiet{i}", ago=timedelta(days=2 + i))

        assert fleet.collect(tmp_path, NOW).fronts == []

    def test_working_notification_and_parked_all_count(self, tmp_path):
        make_session(tmp_path, "w", ago=timedelta(minutes=2))
        make_session(tmp_path, "n", kind="notification", ago=timedelta(minutes=2))
        make_session(tmp_path, "i", ago=timedelta(days=3))
        entry = tmp_path / "sessions" / "p.json"
        make_session(tmp_path, "p", kind="end")
        raw = json.loads(entry.read_text())
        raw["disposition"] = {"state": "parked", "reason": "back to it Monday"}
        entry.write_text(json.dumps(raw))

        f = fleet.collect(tmp_path, NOW)

        assert {a.session_id for a in f.fronts} == {"w", "n", "p"}

    def test_the_header_names_the_idle_ones_separately(self, tmp_path):
        make_session(tmp_path, "w", ago=timedelta(minutes=2))
        make_session(tmp_path, "i", ago=timedelta(days=3))

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "1 front(s)" in md
        assert "1 idle" in md


# ---------------------------------------------------------------------------
# Criterion 5 — collisions
# ---------------------------------------------------------------------------

class TestCollisions:

    def test_two_sessions_sharing_one_file_produce_exactly_one_line(self, tmp_path):
        make_session(tmp_path, "a", project="alpha")
        make_session(tmp_path, "b", project="beta")
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/src/shared.py",
                                              "/work/knowhere/src/only-a.py"))
        make_checkpoint(tmp_path, "b", files=("/work/knowhere/src/shared.py",
                                              "/work/knowhere/src/only-b.py"))

        f = fleet.collect(tmp_path, NOW)
        md = fleet.render_agents_md(f, NOW)
        section = md.split("## Collisions", 1)[1]
        lines = [ln for ln in section.splitlines() if ln.startswith("- ")]

        assert len(f.collisions) == 1
        assert f.collisions[0].path == "/work/knowhere/src/shared.py"
        assert sorted(f.collisions[0].labels) == ["alpha@a", "beta@b"]
        assert len(lines) == 1

    def test_two_disjoint_sessions_produce_none(self, tmp_path):
        make_session(tmp_path, "a")
        make_session(tmp_path, "b")
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/src/only-a.py",))
        make_checkpoint(tmp_path, "b", files=("/work/knowhere/src/only-b.py",))

        f = fleet.collect(tmp_path, NOW)
        md = fleet.render_agents_md(f, NOW)

        assert f.collisions == []
        assert "_None — no file is held by two agents still in play._" in md

    def test_an_ended_session_does_not_collide(self, tmp_path):
        """Two finished sessions that touched the same file is history, not a
        thing to go look at."""
        make_session(tmp_path, "a", kind="end")
        make_session(tmp_path, "b")
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/src/shared.py",))
        make_checkpoint(tmp_path, "b", files=("/work/knowhere/src/shared.py",))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_two_idle_sessions_do_not_collide(self, tmp_path):
        """The whole of the real 2026-08-03 reading: all eight reported
        collisions were between pairs of sessions quiet for three to eighteen
        days. A warning that is wrong every time is one you stop reading,
        which costs more than not having it."""
        make_session(tmp_path, "a", ago=timedelta(days=3))
        make_session(tmp_path, "b", ago=timedelta(days=18))
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/src/shared.py",))
        make_checkpoint(tmp_path, "b", files=("/work/knowhere/src/shared.py",))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_one_stale_holder_is_enough_to_drop_it(self, tmp_path):
        """It takes two agents that might *both* write the file. One of them
        working right now does not make the other one's week-old claim live."""
        make_session(tmp_path, "live", ago=timedelta(minutes=2))
        make_session(tmp_path, "stale", ago=timedelta(days=7))
        make_checkpoint(tmp_path, "live", files=("/work/knowhere/src/shared.py",))
        make_checkpoint(tmp_path, "stale", files=("/work/knowhere/src/shared.py",))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_the_gate_is_the_collision_window_for_parked_work(self, tmp_path):
        """Just inside COLLISION_MAX_AGE_HOURS still collides — the cut is at
        the window, not somewhere vaguely near it.

        Parked, because that is the only group the full window can still reach:
        the two constants were decoupled when IDLE_AFTER_HOURS dropped to 12,
        and unparked work leaves `Working` at that shorter threshold (see the
        test below). Parked work keeps the longer tenure deliberately — nobody
        is watching it and its edits sit there uncommitted."""
        inside = timedelta(hours=fleet.COLLISION_MAX_AGE_HOURS) - timedelta(minutes=1)
        for sid in ("a", "b"):
            entry = make_session(tmp_path, sid, ago=inside)
            raw = json.loads(entry.read_text())
            raw["disposition"] = {"state": "parked", "reason": "back Monday"}
            entry.write_text(json.dumps(raw))
            make_checkpoint(tmp_path, sid, files=("/work/knowhere/src/shared.py",))

        assert len(fleet.collect(tmp_path, NOW).collisions) == 1

    def test_unparked_work_stops_colliding_at_the_idle_window(self, tmp_path):
        """The effective gate on unparked work is the *shorter* of the two: it
        leaves `Working` at IDLE_AFTER_HOURS and only `Working`/`Parked` can
        hold a file. Pinned because the two constants used to be one, and
        decoupling them without saying which one bites here is how a reader
        concludes a 20h-old tab still collides."""
        just_idle = timedelta(hours=fleet.IDLE_AFTER_HOURS) + timedelta(minutes=1)
        assert just_idle < timedelta(hours=fleet.COLLISION_MAX_AGE_HOURS)
        for sid in ("a", "b"):
            make_session(tmp_path, sid, ago=just_idle)
            make_checkpoint(tmp_path, sid, files=("/work/knowhere/src/shared.py",))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_one_session_listing_a_file_twice_is_not_a_collision(self, tmp_path):
        make_session(tmp_path, "a")
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/src/x.py",
                                              "/work/knowhere/src/x.py"))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_three_sessions_on_one_file_is_still_one_line(self, tmp_path):
        for sid in ("a", "b", "c"):
            make_session(tmp_path, sid, project=sid)
            make_checkpoint(tmp_path, sid, files=("/work/knowhere/src/hot.py",))

        f = fleet.collect(tmp_path, NOW)

        assert len(f.collisions) == 1
        assert sorted(f.collisions[0].labels) == ["a@a", "b@b", "c@c"]

    def _two_worktrees(self, tmp_path):
        """Two worktrees of one repo, each holding the same logical file
        under its own absolute prefix — the format real checkpoints carry."""
        for sid, wt in (("a", "feat-one"), ("b", "feat-two")):
            gitdir = tmp_path / "repo" / ".git" / "worktrees" / wt
            gitdir.mkdir(parents=True)
            (gitdir / "HEAD").write_text(f"ref: refs/heads/{wt}\n")
            checkout = tmp_path / wt
            (checkout / "src").mkdir(parents=True)
            (checkout / ".git").write_text(f"gitdir: {gitdir}\n")
            make_session(tmp_path, sid, project="workspace", cwd=str(checkout))
            make_checkpoint(tmp_path, sid, files=(str(checkout / "src" / "hot.py"),))

    def test_absolute_paths_in_two_worktrees_still_collide(self, tmp_path):
        """The headline case. The two checkpoints record
        ``…/feat-one/src/hot.py`` and ``…/feat-two/src/hot.py`` — absolute
        paths that never string-match — so the intersection must run on
        repo-relative paths keyed by the shared main ``.git`` dir."""
        self._two_worktrees(tmp_path)

        f = fleet.collect(tmp_path, NOW)

        assert len(f.collisions) == 1
        assert f.collisions[0].path == "src/hot.py"

    def test_two_worktrees_of_one_project_are_told_apart(self, tmp_path):
        """The common real collision: same project, two tabs. Labelling both
        "workspace" identifies nobody, which is the same as no collision line."""
        self._two_worktrees(tmp_path)

        labels = fleet.collect(tmp_path, NOW).collisions[0].labels

        assert sorted(labels) == ["workspace@feat-one", "workspace@feat-two"]

    def test_the_same_relative_path_in_unrelated_repos_is_not_a_collision(self, tmp_path):
        """Repo-relative normalization must not manufacture collisions:
        every Python repo has a ``src/main.py``."""
        for sid, name in (("a", "alpha"), ("b", "beta")):
            repo = tmp_path / name
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            make_session(tmp_path, sid, project=name, cwd=str(repo))
            make_checkpoint(tmp_path, sid, files=(str(repo / "src" / "main.py"),))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_two_sessions_waiting_on_you_do_not_collide(self, tmp_path):
        """Fourteen of the sixteen collisions reported on 2026-08-04 were pairs
        of sessions stopped at a prompt, sharing a document both had merely
        read. An agent waiting on an answer cannot write anything until it gets
        one, so it is not holding the file against anybody."""
        make_session(tmp_path, "a", kind="notification", ago=timedelta(minutes=2))
        make_session(tmp_path, "b", kind="notification", ago=timedelta(minutes=2))
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/notes/plan.md",))
        make_checkpoint(tmp_path, "b", files=("/work/knowhere/notes/plan.md",))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_a_parked_session_still_holds_its_files(self, tmp_path):
        """The deliberate asymmetry with the test above, pinned here so the two
        readings do not get collapsed by a later tidy-up. ``waiting_input`` is
        excluded because it *cannot* write until answered; parked work can and
        does hold its files — arguably harder than running work, since nobody is
        watching it and the edits are sitting there uncommitted.

        Also asserted in `test_disposition.py::test_a_parked_session_still_collides`,
        from the disposition side."""
        make_session(tmp_path, "working", project="alpha", ago=timedelta(minutes=2))
        entry = make_session(tmp_path, "parked", project="beta", ago=timedelta(minutes=2))
        raw = json.loads(entry.read_text())
        raw["disposition"] = {"state": "parked", "reason": "back to it Monday"}
        entry.write_text(json.dumps(raw))
        for sid in ("working", "parked"):
            make_checkpoint(tmp_path, sid, files=("/work/knowhere/src/shared.py",))

        assert len(fleet.collect(tmp_path, NOW).collisions) == 1

    def test_the_shared_checkout_root_is_not_a_collision(self, tmp_path):
        """The 2026-08-04 phantom: two sessions in one repo each list the
        checkout root as an involved entry, stripping the prefix leaves ``""``
        for both, and two empty strings intersect — so every pair of agents in
        a shared repo reported a collision on a blank path."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        for sid in ("a", "b"):
            make_session(tmp_path, sid, project="workspace", cwd=str(repo))
            make_checkpoint(tmp_path, sid, files=(str(repo) + "/",))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_a_shared_directory_is_not_a_collision(self, tmp_path):
        """A directory in common says "same neighbourhood"; the warning claims
        "same file". Checkpoints list directories freely, so taking them at
        face value manufactures lines nobody can act on."""
        make_session(tmp_path, "a", project="alpha")
        make_session(tmp_path, "b", project="beta")
        for sid in ("a", "b"):
            make_checkpoint(tmp_path, sid, files=("/work/knowhere/src/",))

        assert fleet.collect(tmp_path, NOW).collisions == []

    def test_a_real_shared_file_still_collides_under_a_shared_root(self, tmp_path):
        """The guard against over-correcting: dropping directories and the
        root must not cost the actual signal sitting next to them."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        for sid in ("aaaa1111-x", "bbbb2222-y"):
            make_session(tmp_path, sid, project="workspace", cwd=str(repo))
            make_checkpoint(tmp_path, sid, files=(str(repo) + "/",
                                                  str(repo / "src" / "hot.py")))

        f = fleet.collect(tmp_path, NOW)

        assert len(f.collisions) == 1
        assert f.collisions[0].path == "src/hot.py"

    def test_identical_labels_fall_back_to_the_session_id(self, tmp_path):
        """Two tabs in the same worktree. Nothing but the id distinguishes
        them, so the label must carry it rather than repeat itself."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        for sid in ("aaaa1111-x", "bbbb2222-y"):
            make_session(tmp_path, sid, project="workspace", cwd=str(repo))
            make_checkpoint(tmp_path, sid, files=(str(repo / "src" / "hot.py"),))

        labels = sorted(fleet.collect(tmp_path, NOW).collisions[0].labels)

        assert labels == ["workspace@main (aaaa1111)", "workspace@main (bbbb2222)"]


class TestEndedSessions:
    """Ended sessions are counted, not listed — 105 of them to 35 live against
    the real registry, which turned the file into a graveyard."""

    def test_they_do_not_get_an_entry(self, tmp_path):
        make_session(tmp_path, "over", kind="end", project="finished")
        make_checkpoint(tmp_path, "over", intent="Something long since done")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "Something long since done" not in md
        assert "## Ended" not in md

    def test_they_are_counted_in_the_header(self, tmp_path):
        make_session(tmp_path, "live")
        for i in range(3):
            make_session(tmp_path, f"over{i}", kind="end")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "1 front(s)" in md
        assert "3 finished, not listed" in md

    def test_no_ended_sessions_means_no_mention_of_them(self, tmp_path):
        make_session(tmp_path, "live")

        assert "finished" not in fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)


# ---------------------------------------------------------------------------
# Parsing "Involved files"
# ---------------------------------------------------------------------------

class TestFirstLine:

    def test_a_bullet_marker_is_stripped(self):
        assert fleet._first_line("- Fix the drain\n") == "Fix the drain"
        assert fleet._first_line("* Fix the drain\n") == "Fix the drain"

    def test_markdown_bold_survives(self):
        """`lstrip("-*")` used to eat the opening ``**``, rendering a bold
        intent as ``Refactor** the drain`` in AGENTS.md."""
        assert fleet._first_line("**Refactor** the drain") == "**Refactor** the drain"
        assert fleet._first_line("- **Refactor** the drain") == "**Refactor** the drain"


class TestInvolvedFilesParsing:

    def test_backticked_paths_with_prose(self):
        body = "- `src/a.py` — the thing\n- `docs/b.md` — the other thing\n"

        assert fleet.parse_involved_files(body) == ["src/a.py", "docs/b.md"]

    def test_an_unterminated_backtick_yields_the_path_not_the_prose(self):
        """A truncated bullet like ``- `src/a.py — why it matters`` must not
        push a garbage "path" with embedded spaces into the Files list and
        the collision key space."""
        body = "- `src/a.py — why it matters\n"

        assert fleet.parse_involved_files(body) == ["src/a.py"]

    def test_bare_paths_with_an_em_dash_description(self):
        body = "- src/a.py — the thing\n- docs/b.md - other\n"

        assert fleet.parse_involved_files(body) == ["src/a.py", "docs/b.md"]

    def test_prose_that_is_not_a_path_is_not_counted(self):
        """Counting a sentence as a filename would invent collisions."""
        body = "- none yet\n- TBD\n- `src/real.py` — this one is real\n"

        assert fleet.parse_involved_files(body) == ["src/real.py"]

    def test_duplicates_within_one_checkpoint_collapse(self):
        body = "- `src/a.py` — x\n- `src/a.py` — y\n"

        assert fleet.parse_involved_files(body) == ["src/a.py"]

    def test_an_empty_section_yields_nothing(self):
        assert fleet.parse_involved_files("") == []
        assert fleet.parse_involved_files("_None._") == []


# ---------------------------------------------------------------------------
# The container roster — evidence beats the clock
# ---------------------------------------------------------------------------

def make_roster(data_dir, *names, ago=timedelta(0), kind="container",
                observer="host", now=NOW):
    """Write a live-container roster as the kit launcher writes it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / fleet.ROSTER_FILENAME).write_text(json.dumps({
        "version": 1,
        "observed_at": (now - ago).isoformat(),
        "observer": observer,
        "kind": kind,
        "ids": list(names),
    }))


def make_contained_session(data_dir, sid, *, host, **kw):
    """A registry entry that records running inside a container."""
    entry = make_session(data_dir, sid, hostname=host, **kw)
    raw = json.loads(entry.read_text())
    raw["in_container"] = True
    entry.write_text(json.dumps(raw))
    return entry


class TestContainerRoster:
    """A session cannot observe its own death — a hook is code running inside
    it. The host can just look. When it has looked *since* the entry last
    spoke, that reading replaces the quiet heuristic outright."""

    def test_a_missing_container_is_proof_the_session_ended(self, tmp_path):
        make_contained_session(tmp_path, "gone", host="claude-personal-01",
                               ago=timedelta(minutes=2))
        make_roster(tmp_path, "claude-personal-99")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.status == "ended"
        assert agent.live is False

    def test_a_live_container_beats_the_quiet_window(self, tmp_path):
        """The other half, and the one that fixes a real error: a session
        thinking for longer than IDLE_AFTER_HOURS used to drift to `idle`."""
        old = timedelta(hours=fleet.IDLE_AFTER_HOURS + 5)
        make_contained_session(tmp_path, "busy", host="claude-personal-01", ago=old)
        make_roster(tmp_path, "claude-personal-01")

        assert fleet.collect(tmp_path, NOW).agents[0].status == "working"

    def test_a_live_container_at_a_prompt_still_needs_you(self, tmp_path):
        old = timedelta(hours=fleet.IDLE_AFTER_HOURS + 5)
        make_contained_session(tmp_path, "ask", host="claude-personal-01",
                               kind="notification", ago=old)
        make_roster(tmp_path, "claude-personal-01")

        assert fleet.collect(tmp_path, NOW).agents[0].status == "waiting_input"

    def test_a_roster_older_than_the_entry_decides_nothing(self, tmp_path):
        """It proves nothing: the session may have started in the gap. Falling
        back is what makes a stale roster harmless rather than wrong."""
        make_contained_session(tmp_path, "new", host="claude-personal-01",
                               ago=timedelta(minutes=2))
        make_roster(tmp_path, ago=timedelta(hours=3))

        assert fleet.collect(tmp_path, NOW).agents[0].status == "working"

    def test_no_roster_at_all_changes_nothing(self, tmp_path):
        """Vanilla Claude Code has no launcher to write one. The degradation
        contract is satisfied by the ordinary path, not a special case."""
        make_contained_session(tmp_path, "quiet", host="claude-personal-01",
                               ago=timedelta(days=3))

        assert fleet.collect(tmp_path, NOW).agents[0].status == "idle"

    def test_a_bare_session_is_never_judged_against_container_names(self, tmp_path):
        """`--local` mode and claude-wrapped run on the Mac, where `hostname`
        is the machine name. It will never be in `docker ps`, and declaring a
        live session dead is the one direction this must not fail in."""
        make_session(tmp_path, "bare", hostname="Spikes-MacBook",
                     ago=timedelta(minutes=2))
        make_roster(tmp_path, "claude-personal-01")

        assert fleet.collect(tmp_path, NOW).agents[0].status == "working"

    def test_an_entry_predating_the_field_falls_back(self, tmp_path):
        """`in_container` absent means "unknown", which must not be read as
        `False` *or* as `True` — hence the explicit tri-state."""
        make_session(tmp_path, "old", hostname="claude-personal-01",
                     ago=timedelta(minutes=2))
        make_roster(tmp_path, "claude-personal-99")

        agents = fleet.collect(tmp_path, NOW).agents
        assert agents[0].in_container is None
        assert agents[0].status == "working"

    def test_a_clean_quit_is_still_ended_without_any_roster_reading(self, tmp_path):
        make_contained_session(tmp_path, "done", host="claude-personal-01",
                               kind="end", ago=timedelta(minutes=2))
        make_roster(tmp_path, "claude-personal-01")

        assert fleet.collect(tmp_path, NOW).agents[0].status == "ended"

    def test_parking_survives_a_dead_container(self, tmp_path):
        """The whole point of parking: "I am coming back to this" outlives the
        process. Disposition beats liveness, and a proven death is still
        liveness."""
        entry = make_contained_session(tmp_path, "parked", host="claude-personal-01",
                                       ago=timedelta(minutes=2))
        raw = json.loads(entry.read_text())
        raw["disposition"] = {"state": "parked", "reason": "back Monday"}
        entry.write_text(json.dumps(raw))
        make_roster(tmp_path, "claude-personal-99")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.status == "ended"
        assert agent.group == "Parked"
        assert agent.live is True

    def test_a_pid_roster_is_refused(self, tmp_path):
        """`fleet_sources/jobs.py` carries this scar already: a pid means
        something only in the namespace that observed it. When session identity
        moves to a pid under the SDK, a reader expecting containers must refuse
        rather than match — confidently reporting a dead session as running is
        the failure mode `kind`/`observer` exist to prevent."""
        make_contained_session(tmp_path, "s", host="claude-personal-01",
                               ago=timedelta(days=3))
        make_roster(tmp_path, "claude-personal-01", kind="pid", observer="local")

        assert fleet.collect(tmp_path, NOW).agents[0].status == "idle"

    def test_a_corrupt_roster_is_no_roster(self, tmp_path):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / fleet.ROSTER_FILENAME).write_text("{not json")
        make_contained_session(tmp_path, "s", host="claude-personal-01",
                               ago=timedelta(days=3))

        assert fleet.load_roster(tmp_path) is None
        assert fleet.collect(tmp_path, NOW).agents[0].status == "idle"

    def test_the_status_vocabulary_is_still_the_contracted_four(self, tmp_path):
        """No fifth value is coined for "container gone" — it is `ended`."""
        make_roster(tmp_path, "claude-personal-01")
        roster = fleet.load_roster(tmp_path)
        for kind in ("start", "stop", "notification", "end"):
            for host in ("claude-personal-01", "claude-personal-99"):
                assert fleet._status_of(
                    kind, NOW - timedelta(days=3), NOW, roster, host, True,
                ) in {"working", "waiting_input", "idle", "ended"}


# ---------------------------------------------------------------------------
# Roster-confirmed-dead sessions, for the registry collector
# ---------------------------------------------------------------------------

class TestRosterDeadSids:
    """The two age windows in the registry GC (7 days ended / 30 days
    anything-else) are not really about age — they are guesses standing in for
    "a session cannot report its own death". Where the host has looked, the
    guess is not needed, and a fortnight of "might still be alive" is a
    graveyard with a countdown."""

    OLD = timedelta(hours=fleet.ROSTER_DEAD_GRACE_HOURS + 1)

    def test_a_session_whose_container_is_gone_is_collectable(self, tmp_path):
        make_contained_session(tmp_path, "gone", host="cc-01", ago=self.OLD)
        make_roster(tmp_path, "cc-99")

        assert fleet.roster_dead_sids(tmp_path, NOW) == {"gone"}

    def test_a_session_still_on_the_roster_is_not(self, tmp_path):
        make_contained_session(tmp_path, "alive", host="cc-01", ago=self.OLD)
        make_roster(tmp_path, "cc-01")

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()

    def test_no_roster_means_no_opinion(self, tmp_path):
        """Vanilla Claude Code writes no roster; the age windows are all there
        is, exactly as before."""
        make_contained_session(tmp_path, "gone", host="cc-01", ago=self.OLD)

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()

    def test_a_roster_older_than_the_entry_decides_nothing(self, tmp_path):
        """The entry may have started in the gap — the same monotonicity rule
        the status reader applies."""
        make_contained_session(tmp_path, "gone", host="cc-01", ago=self.OLD)
        make_roster(tmp_path, "cc-99", ago=timedelta(hours=6))

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()

    def test_a_session_outside_a_container_is_never_judged(self, tmp_path):
        """`hostname` is a machine name there, and a machine is not in any
        `docker ps`. Declaring a live `--local` session dead is the one
        direction this must never fail in."""
        make_session(tmp_path, "local", hostname="spikes-mac", ago=self.OLD)

        make_roster(tmp_path, "cc-01")

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()

    def test_the_grace_period_keeps_a_just_exited_session(self, tmp_path):
        """The deferred extraction that writes `disposition` runs minutes after
        exit, and only its marker protects an entry. A session killed hard never
        wrote one, so the grace is what stops GC racing the drain."""
        make_contained_session(tmp_path, "fresh", host="cc-01",
                               ago=timedelta(minutes=2))
        make_roster(tmp_path, "cc-99")

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()

    def test_a_parked_session_is_never_collectable(self, tmp_path):
        """Its container being gone is the normal state of a parked session —
        that is what parking is. Disposition outranks liveness here."""
        entry = make_contained_session(tmp_path, "parked", host="cc-01",
                                       ago=self.OLD)
        raw = json.loads(entry.read_text())
        raw["disposition"] = {"state": "parked", "reason": "back on Monday"}
        entry.write_text(json.dumps(raw))
        make_roster(tmp_path, "cc-99")

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()

    def test_an_unreadable_entry_is_skipped_not_collected(self, tmp_path):
        (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
        (tmp_path / "sessions" / "junk.json").write_text("{not json")
        make_roster(tmp_path, "cc-99")

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()

    def test_a_roster_this_reader_cannot_interpret_is_refused(self, tmp_path):
        """A pid means something only in the namespace that observed it."""
        make_contained_session(tmp_path, "gone", host="cc-01", ago=self.OLD)
        make_roster(tmp_path, "cc-99", kind="pid")

        assert fleet.roster_dead_sids(tmp_path, NOW) == set()


# ---------------------------------------------------------------------------
# The involved-files bullet is gone from the render — and only from the render
# ---------------------------------------------------------------------------

class TestFilesAreNotRendered:
    """`AGENTS.md` no longer prints an agent's involved files. The paths
    themselves are load-bearing elsewhere, so what these pin is the seam: the
    *display* went, the data did not."""

    def _agent(self, *files):
        return fleet.Agent(
            session_id="s", project="app", cwd="/work/knowhere",
            repo_root="/work/knowhere", status="working",
            files=list(files), has_checkpoint=True,
            intent="do the thing", next_action="ship it",
        )

    def test_the_render_has_no_files_bullet(self):
        agent = self._agent("/work/knowhere/src/x.py", "/work/knowhere/src/y.py")

        rendered = "\n".join(fleet._render_agent(agent, NOW))

        assert "Files:" not in rendered
        assert "src/x.py" not in rendered

    def test_what_the_entry_is_for_survives(self):
        """Dropping the paths must not drop the two lines a reader acts on."""
        agent = self._agent("/work/knowhere/src/x.py")

        rendered = "\n".join(fleet._render_agent(agent, NOW))

        assert "**Doing:** do the thing" in rendered
        assert "**Next:** ship it" in rendered

    def test_an_agent_with_files_is_not_marked_checkpointless(self):
        """The `_No checkpoint_` line is about the checkpoint, not about
        whether anything is left to print after the files bullet went."""
        agent = self._agent("/work/knowhere/src/x.py")

        assert "No checkpoint" not in "\n".join(fleet._render_agent(agent, NOW))

    def test_the_paths_stay_absolute_on_the_agent(self):
        """`fleet.json` ships these and `find_collisions` reads them; both
        break on a relative path."""
        agent = self._agent("/work/knowhere/src/x.py")

        fleet._render_agent(agent, NOW)

        assert agent.files == ["/work/knowhere/src/x.py"]

    def test_collisions_still_come_out_of_the_same_files(self, tmp_path):
        """The point of the removal: the line was a display of data that is
        read for collisions independently. Two working agents holding one file
        must still collide with nothing rendering that file."""
        for sid in ("a", "b"):
            make_session(tmp_path, sid, ago=timedelta(minutes=5))
            make_checkpoint(tmp_path, sid, files=["/work/knowhere/src/x.py"])

        f = fleet.collect(tmp_path, NOW)

        assert [c.path for c in f.collisions] == ["/work/knowhere/src/x.py"]
        assert "Files:" not in fleet.render_agents_md(f, NOW)


# ---------------------------------------------------------------------------
# The status-line count is retired — the fronts count survives in `Fleet.fronts`
# ---------------------------------------------------------------------------

class TestFrontsCount:

    def test_idle_is_counted_but_is_not_a_front(self, tmp_path):
        for i in range(4):
            make_session(tmp_path, f"w{i}", ago=timedelta(hours=1))
        make_session(tmp_path, "n1", kind="notification")
        make_session(tmp_path, "n2", kind="notification")
        make_session(tmp_path, "old", ago=timedelta(days=3))

        f = fleet.collect(tmp_path, NOW)

        # Seven sessions, six fronts: `old` went quiet three days ago, so it
        # is counted in the AGENTS.md header as idle and excluded from both
        # the fronts and the body.
        assert len(f.agents) == 7
        assert len(f.fronts) == 6
        assert len(f.in_group("Needs you")) == 2

    def test_render_fleet_line_is_gone(self):
        """The status-line rendering must not linger as dead code a caller
        could quietly resurrect."""
        assert not hasattr(fleet, "render_fleet_line")

    @pytest.mark.parametrize("delta,expected", [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=12), "12m"),
        (timedelta(hours=5), "5h"),
        (timedelta(days=3, hours=4), "3d"),
    ])
    def test_age_formatting(self, delta, expected):
        assert fleet.format_age(delta) == expected


# ---------------------------------------------------------------------------
# Criterion 4 — the cache property
# ---------------------------------------------------------------------------

class TestCacheProperty:

    def _fleet_dir(self, tmp_path):
        make_session(tmp_path, "a", project="alpha", kind="notification")
        make_session(tmp_path, "b", project="beta", ago=timedelta(hours=30))
        make_session(tmp_path, "c", project="gamma", kind="end")
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/src/shared.py",))
        make_checkpoint(tmp_path, "b", files=("/work/knowhere/src/shared.py",
                                              "/work/knowhere/src/b.py"))
        return tmp_path

    def test_delete_the_output_and_it_comes_back_identical(self, tmp_path):
        data = self._fleet_dir(tmp_path)
        agents_path = fleet.write_fleet_view(data, NOW)
        before = agents_path.read_text()

        agents_path.unlink()
        fleet.write_fleet_view(data, NOW)

        assert agents_path.read_text() == before

    def test_only_the_generation_stamp_differs_across_runs(self, tmp_path):
        """Proven at the render layer with two `now` values a second apart —
        close enough that no age bucket or status threshold is crossed.
        Between such boundaries, everything except the stamp is a function of
        the two stores alone."""
        data = self._fleet_dir(tmp_path)
        first = fleet.render_agents_md(fleet.collect(data, NOW), NOW)
        later = NOW + timedelta(seconds=1)
        second = fleet.render_agents_md(fleet.collect(data, later), later)

        differing = [
            (a, b) for a, b in zip(first.splitlines(), second.splitlines()) if a != b
        ]

        assert len(differing) == 1
        assert differing[0][0].startswith("_Generated ")

    def test_nothing_written_into_agents_md_survives_a_rerun(self, tmp_path):
        """AGENTS.md must never become a fourth store. If someone edits it, the
        next run overwrites — that is the property, not a bug."""
        data = self._fleet_dir(tmp_path)
        agents_path = fleet.write_fleet_view(data, NOW)
        expected = agents_path.read_text()

        agents_path.write_text(expected + "\n## My own notes\n\nremember this\n")
        fleet.write_fleet_view(data, NOW)

        assert agents_path.read_text() == expected

    def test_ordering_is_stable_when_timestamps_tie(self, tmp_path):
        """Without a tiebreak, dict/glob ordering would make the cache property
        flap on filesystems that don't sort."""
        for sid in ("zzz", "aaa", "mmm"):
            make_session(tmp_path, sid, ago=timedelta(hours=1))

        once = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)
        twice = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert once == twice
        assert re.findall(r"session `(\w+)`", once) == ["aaa", "mmm", "zzz"]


# ---------------------------------------------------------------------------
# Rendering shape
# ---------------------------------------------------------------------------

class TestRendering:

    def test_entries_carry_intent_and_next_action(self, tmp_path):
        make_session(tmp_path, "a", project="alpha")
        make_checkpoint(
            tmp_path, "a",
            intent="Rewiring the drain", nxt="Run the gates",
            files=("/work/knowhere/src/x.py",),
        )

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "**Doing:** Rewiring the drain" in md
        assert "**Next:** Run the gates" in md
        # Involved files are collected but never rendered — see
        # TestFilesAreNotRendered.
        assert "Files:" not in md

    def test_groups_appear_only_when_populated(self, tmp_path):
        make_session(tmp_path, "a", kind="notification")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "## Needs you (1)" in md
        assert "## Working" not in md
        assert "## Idle" not in md
        assert "## Ended" not in md

    def test_the_header_says_what_the_file_is(self, tmp_path):
        make_session(tmp_path, "a")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "a cache of" in md
        assert "delete it and it comes back" in md

    def test_branch_is_read_off_disk_for_an_ordinary_repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/thing\n")
        make_session(tmp_path, "a", cwd=str(repo))

        assert fleet.collect(tmp_path, NOW).agents[0].branch == "feat/thing"

    def test_a_worktree_says_so(self, tmp_path):
        """Six worktrees of one repo is the normal case here; the branch alone
        wouldn't tell him which tab is which."""
        gitdir = tmp_path / "repo" / ".git" / "worktrees" / "mkt-agents"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/feat/agents\n")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        make_session(tmp_path, "a", cwd=str(wt))

        branch = fleet.collect(tmp_path, NOW).agents[0].branch

        assert branch == "feat/agents (worktree: mkt-agents)"

    def test_a_submodule_is_not_labelled_a_worktree(self, tmp_path):
        """A submodule checkout *also* has a ``.git`` file pointer, but it
        targets ``<parent>/.git/modules/<name>`` — only a gitdir under
        ``worktrees/`` is a worktree."""
        gitdir = tmp_path / "parent" / ".git" / "modules" / "sub"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
        sub = tmp_path / "parent" / "sub"
        sub.mkdir()
        # Submodules record the pointer relative to the checkout.
        (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
        make_session(tmp_path, "a", cwd=str(sub))

        assert fleet.collect(tmp_path, NOW).agents[0].branch == "main"

    def test_a_detached_head_shows_the_short_sha(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("4afbf7ecafe0123456789\n")
        make_session(tmp_path, "a", cwd=str(repo))

        assert fleet.collect(tmp_path, NOW).agents[0].branch == "4afbf7ec"

    def test_a_cwd_that_is_not_a_repo_is_blank_not_an_error(self, tmp_path):
        make_session(tmp_path, "a", cwd=str(tmp_path / "nowhere"))

        assert fleet.collect(tmp_path, NOW).agents[0].branch == ""


# ---------------------------------------------------------------------------
# Criterion 3 — where the files land
# ---------------------------------------------------------------------------

class TestOutputLocation:

    def test_the_file_lands_in_the_data_dir(self, tmp_path):
        make_session(tmp_path, "a")

        agents_path = fleet.write_fleet_view(tmp_path, NOW)

        assert agents_path == tmp_path / "AGENTS.md"
        assert agents_path.exists()

    def test_nothing_is_written_to_now(self, tmp_path):
        """`now/*.md` is globbed by the hub into one NowCard per filename, so an
        AGENTS.md there would surface as a bogus project called "AGENTS"."""
        data = tmp_path / "data"
        now_dir = tmp_path / "now"
        now_dir.mkdir()
        make_session(data, "a")

        fleet.write_fleet_view(data, NOW)

        assert list(now_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# fleet.json on the hook path — and the clobber that had to be solved first
# ---------------------------------------------------------------------------

class TestFleetJsonIsWrittenOnEveryRender:
    """`write_fleet_view` writes both files.

    It used not to. `fleet_status.py` was the sole writer of `fleet.json`,
    which meant the JSON was refreshed only when a human ran `/fleet-status`
    by hand — on 2026-08-06 `AGENTS.md` was stamped 21:23 while `fleet.json`
    still carried the previous day. Two renderings of one truth, a day apart.
    """

    def test_both_files_appear(self, tmp_path):
        make_session(tmp_path, "a", project="alpha")

        fleet.write_fleet_view(tmp_path, NOW)

        assert (tmp_path / "AGENTS.md").exists()
        assert (tmp_path / "fleet.json").exists()

    def test_the_json_parses_and_declares_its_shape(self, tmp_path):
        make_session(tmp_path, "a", project="alpha")

        fleet.write_fleet_view(tmp_path, NOW)
        payload = json.loads((tmp_path / "fleet.json").read_text())

        assert payload["version"] == 1
        assert [a["session_id"] for a in payload["agents"]] == ["a"]

    def test_the_version_did_not_move(self):
        """`FLEET_JSON_VERSION` is bumped when a field changes meaning or
        disappears. `collected_at` is purely additive, and every existing field
        still means what it did, so a consumer holding version 1 is not
        holding a different shape."""
        assert fleet.FLEET_JSON_VERSION == 1

    def test_sections_this_path_cannot_collect_are_null_not_empty(self, tmp_path):
        """The hook path reads local files only; PRs and repo state need `git`
        and `gh`. `null` says "I didn't look", which is a different statement
        from `[]`, "I looked and there was nothing"."""
        make_session(tmp_path, "a")

        fleet.write_fleet_view(tmp_path, NOW)
        payload = json.loads((tmp_path / "fleet.json").read_text())

        for section in ("prs", "repos", "jobs", "backlog", "scheduled"):
            assert payload[section] is None
        assert payload["collected_at"] == {}

    def test_the_json_is_a_cache_like_the_markdown(self, tmp_path):
        """Delete both, re-run, get both back — the property everything else
        rests on. Nothing may write into either as primary state."""
        make_session(tmp_path, "a", project="alpha", kind="notification")
        make_checkpoint(tmp_path, "a")
        fleet.write_fleet_view(tmp_path, NOW)
        before_md = (tmp_path / "AGENTS.md").read_text()
        before_json = (tmp_path / "fleet.json").read_text()

        (tmp_path / "AGENTS.md").unlink()
        (tmp_path / "fleet.json").unlink()
        fleet.write_fleet_view(tmp_path, NOW)

        assert (tmp_path / "AGENTS.md").read_text() == before_md
        assert (tmp_path / "fleet.json").read_text() == before_json

    def test_agents_md_is_unchanged_by_the_json_writer(self, tmp_path):
        """The other half of the same claim: adding a second output must not
        move a byte of the first."""
        make_session(tmp_path, "a", project="alpha")
        make_checkpoint(tmp_path, "a")
        f = fleet.collect(tmp_path, NOW)
        expected = fleet.render_agents_md(f, NOW)

        fleet.write_fleet_view(tmp_path, NOW)

        assert (tmp_path / "AGENTS.md").read_text() == expected


class TestCarryForward:
    """A `None` means *this pass did not look*, and the honest response is to
    keep what the last pass saw with the time it saw it — not to overwrite a
    fact with an absence.

    Without this, wiring `fleet.json` into the hook path would be a
    regression: run `/fleet-status`, get a payload with your PRs in it, and the
    next session start (seconds later, with ten tabs open) blanks it. A board
    would flip from "PRs 3 open (1 red)" to "not collected" through no action
    of yours.
    """

    def _existing(self, tmp_path, *, prs, stamp, section="prs"):
        (tmp_path / "fleet.json").write_text(json.dumps({
            "version": 1,
            "generated_at": stamp,
            "counts": {},
            "agents": [{"session_id": "stale-one"}],
            "collisions": [],
            "prs": None, "repos": None, "jobs": None,
            "backlog": None, "scheduled": None,
            section: prs,
            "collected_at": {section: stamp},
        }, indent=2))

    def test_a_recent_pr_scan_survives_a_session_start(self, tmp_path):
        make_session(tmp_path, "a")
        stamp = (NOW - timedelta(minutes=14)).isoformat()
        self._existing(tmp_path, prs={"open": 3, "red": 1}, stamp=stamp)

        fleet.write_fleet_view(tmp_path, NOW)
        payload = json.loads((tmp_path / "fleet.json").read_text())

        assert payload["prs"] == {"open": 3, "red": 1}
        # With its own stamp, so a consumer can render "PRs 3 open · 14m ago"
        # rather than implying it looked just now.
        assert payload["collected_at"]["prs"] == stamp

    def test_agents_and_collisions_are_replaced_not_carried(self, tmp_path):
        """They are what this pass *did* collect. Carrying them would make the
        file a record of every session that ever existed."""
        make_session(tmp_path, "a")
        self._existing(tmp_path, prs={"open": 3},
                       stamp=(NOW - timedelta(minutes=1)).isoformat())

        fleet.write_fleet_view(tmp_path, NOW)
        payload = json.loads((tmp_path / "fleet.json").read_text())

        assert [a["session_id"] for a in payload["agents"]] == ["a"]

    def test_a_section_older_than_an_hour_reverts_to_not_collected(self, tmp_path):
        """The expiry is what stops the board showing yesterday's PR state
        forever while looking confident about it — the exact failure the
        null/empty distinction exists to prevent."""
        make_session(tmp_path, "a")
        self._existing(tmp_path, prs={"open": 3},
                       stamp=(NOW - timedelta(hours=1, minutes=1)).isoformat())

        fleet.write_fleet_view(tmp_path, NOW)
        payload = json.loads((tmp_path / "fleet.json").read_text())

        assert payload["prs"] is None
        assert "prs" not in payload["collected_at"]

    def test_an_empty_list_is_carried_as_an_empty_list(self, tmp_path):
        """"Collected, nothing found" and "I didn't look" must never print the
        same. Turning `[]` into `null` here would erase exactly that."""
        make_session(tmp_path, "a")
        self._existing(tmp_path, prs=[], stamp=(NOW - timedelta(minutes=5)).isoformat(),
                       section="repos")

        fleet.write_fleet_view(tmp_path, NOW)
        payload = json.loads((tmp_path / "fleet.json").read_text())

        assert payload["repos"] == []
        assert payload["prs"] is None

    def test_a_section_with_no_stamp_is_not_carried(self, tmp_path):
        """An unknown age cannot be rendered honestly, and showing it undated
        would be the board claiming freshness it cannot support."""
        make_session(tmp_path, "a")
        (tmp_path / "fleet.json").write_text(json.dumps({
            "version": 1, "agents": [], "collisions": [],
            "prs": {"open": 3}, "repos": None, "jobs": None,
            "backlog": None, "scheduled": None,
        }))

        fleet.write_fleet_view(tmp_path, NOW)

        assert json.loads((tmp_path / "fleet.json").read_text())["prs"] is None

    def test_a_malformed_existing_file_is_overwritten_not_fatal(self, tmp_path):
        """Same rule as the roster: absent and unreadable are one answer, and
        a status view never raises."""
        make_session(tmp_path, "a")
        (tmp_path / "fleet.json").write_text("{not json")

        fleet.write_fleet_view(tmp_path, NOW)

        assert json.loads((tmp_path / "fleet.json").read_text())["prs"] is None

    def test_a_collected_section_beats_a_carried_one(self, tmp_path):
        """`/fleet-status` stays the deliberate refresh: what it actually
        looked at always wins over what a previous pass remembered."""
        f = fleet.Fleet(agents=[], collisions=[], repos=[])
        existing = {"repos": ["stale"],
                    "collected_at": {"repos": (NOW - timedelta(minutes=1)).isoformat()}}

        payload = json.loads(fleet.fleet_json(f, NOW, existing=existing))

        assert payload["repos"] == []
        assert payload["collected_at"]["repos"] == payload["generated_at"]

    def test_fleet_status_carries_forward_too(self, tmp_path, monkeypatch):
        """The other writer. `write_fleet_view` is not the only path to
        `fleet.json` — `fleet_status.py` writes it as well, and it was passing
        no *existing* at all.

        Reaching a section is not collecting it: `--offline` skips GitHub
        outright, and any source that errors returns `None`. So a single
        offline run erased a PR reading a run ten minutes earlier had taken,
        turning "3 open, 14m ago" into "nobody looked" — the one distinction
        the whole null/empty discipline exists to keep. That it survived
        `write_fleet_view` and died in the CLI is exactly why this is pinned
        against the CLI and not the library.
        """
        import importlib.util

        make_session(tmp_path, "a")
        # `main()` takes its own `datetime.now`, so the stamp has to be recent
        # in real time or the one-hour expiry drops it before carry-forward
        # ever gets a say — and the test would pass for the wrong reason.
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=14)).isoformat()
        self._existing(tmp_path, prs={"open": 3, "red": 1}, stamp=stamp)

        spec = importlib.util.spec_from_file_location(
            "fleet_status_under_test", SCRIPTS_DIR / "fleet_status.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        monkeypatch.setattr(
            "sys.argv",
            ["fleet_status.py", "--offline", "--data-dir", str(tmp_path)])
        module.main()

        payload = json.loads((tmp_path / "fleet.json").read_text())

        assert payload["prs"] == {"open": 3, "red": 1}
        assert payload["collected_at"]["prs"] == stamp


# ---------------------------------------------------------------------------
# The tmux pane map — labelling a session with the tab the user named
# ---------------------------------------------------------------------------

def make_pane_map(data_dir, *entries, server="/private/tmp/tmux-501/default",
                  kind="tmux", observer="host"):
    """Write `tmux/panes.json` as the kit launcher writes it.

    ``entries`` are ``(container, pane, window)`` triples, or
    ``(container, pane, window, server)`` quadruples when the entry sits on a
    tmux server other than the document's — which is the ordinary state of a
    merged map, not an exotic one.
    """
    d = data_dir / "tmux"
    d.mkdir(parents=True, exist_ok=True)
    panes = {}
    for entry in entries:
        name, pane, window = entry[:3]
        body = {"pane": pane, "window": window, "session": "work",
                "at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if len(entry) > 3:
            body["server"] = entry[3]
        elif server is not None:
            body["server"] = server
        panes[name] = body
    (d / "panes.json").write_text(json.dumps({
        "version": 1,
        "observed_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "observer": observer,
        "kind": kind,
        "server": server,
        "panes": panes,
    }, indent=2))
    return d / "panes.json"


def make_legacy_pane_map(data_dir, *entries, server="/private/tmp/tmux-501/default"):
    """A map from before `server` was per entry — document level only."""
    path = make_pane_map(data_dir, *entries, server=server)
    raw = json.loads(path.read_text())
    for body in raw["panes"].values():
        body.pop("server", None)
    path.write_text(json.dumps(raw, indent=2))
    return path


class TestPaneMap:
    """The join that lets the fleet view say `pi-eval` instead of
    `claude-personal-06175625`.

    Nothing inside a container can produce this. `record_event()` runs in the
    container and tmux runs on the Mac, so `$TMUX_PANE` there is not missing —
    it is unknowable. The launcher writes it host-side and this module joins it
    on the container name, which is `Agent.hostname` and the only key that
    survives a `/clear`.
    """

    def test_a_matching_container_gets_its_tab(self, tmp_path):
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_pane == "%12"
        assert agent.tmux_window == "pi-eval"
        assert agent.tmux_server == "/private/tmp/tmux-501/default"

    def test_the_server_comes_from_the_entry_not_the_document(self, tmp_path):
        """The map merges across tabs, and tabs can be attached to different
        tmux servers — the document's `server` is whichever launch wrote the
        file last. Reading it for every entry stamps a carried-forward tab with
        a socket it was never on, and a socket is what tells `%12` here from
        `%12` there. The wrong one is worse than none."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(
            tmp_path,
            ("claude-a-01", "%3", "kit", "/private/tmp/tmux-501/second"),
            server="/private/tmp/tmux-501/default",
        )

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_server == "/private/tmp/tmux-501/second"

    def test_a_map_without_per_entry_servers_falls_back_to_the_document(self, tmp_path):
        """Written by a launcher older than the field. The document value is
        the only answer available, and it is right for the one entry that
        launch wrote — which on an older map is the common case."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_legacy_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_server == "/private/tmp/tmux-501/default"

    def test_a_container_not_in_the_map_is_left_empty(self, tmp_path):
        """The ordinary case for anyone who does not use tmux, and for a
        session that started before the current map was written."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-other-02", "%3", "kit"))

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert (agent.tmux_pane, agent.tmux_window, agent.tmux_server) == ("", "", "")

    def test_a_payload_of_the_wrong_kind_is_refused_wholesale(self, tmp_path):
        """Same contract as the roster's, for the same reason: a reader that
        shrugged at a payload it cannot interpret would join a pid to a pane
        id. Refused means `None`, not "use it anyway"."""
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"), kind="pid")

        assert fleet.load_pane_map(tmp_path) is None

    def test_a_payload_from_the_wrong_observer_is_refused_too(self, tmp_path):
        make_pane_map(tmp_path, ("claude-a-01", "%12", "x"), observer="container")

        assert fleet.load_pane_map(tmp_path) is None

    def test_a_missing_map_is_none_not_an_error(self, tmp_path):
        assert fleet.load_pane_map(tmp_path) is None

    def test_a_malformed_map_is_none_not_an_error(self, tmp_path):
        """Labels are an enrichment over a working default, so there is never a
        reason to raise — the same rule `load_roster` follows."""
        (tmp_path / "tmux").mkdir()
        (tmp_path / "tmux" / "panes.json").write_text("{not json")

        assert fleet.load_pane_map(tmp_path) is None

    def test_an_entry_with_no_pane_id_is_dropped(self, tmp_path):
        """The map exists to answer "which pane". An entry that cannot is worse
        than a missing one — it would join to whatever a blank id matched."""
        (tmp_path / "tmux").mkdir()
        (tmp_path / "tmux" / "panes.json").write_text(json.dumps({
            "version": 1, "observer": "host", "kind": "tmux", "server": "/s",
            "panes": {"claude-a-01": {"pane": "", "window": "x"}},
        }))

        assert fleet.load_pane_map(tmp_path).panes == {}

    def test_the_server_is_carried_so_a_pane_id_can_be_disambiguated(self, tmp_path):
        """tmux recycles pane ids per server, so `%12` means nothing on its
        own. Anything joining to a pane id has to compare servers first and
        degrade to "unknown" rather than to the wrong session."""
        make_pane_map(tmp_path, ("claude-a-01", "%12", "x"), server="/tmp/other")

        assert fleet.load_pane_map(tmp_path).server == "/tmp/other"


class TestPaneMapRendering:

    def test_agents_md_shows_the_tab_name(self, tmp_path):
        make_session(tmp_path, "a", hostname="claude-a-01", project="mktplace")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "tab `pi-eval`" in md

    def test_a_collision_label_prefers_the_tab_over_the_branch(self, tmp_path):
        """The label exists to identify *which tab* to switch to. A name the
        user chose mid-session, once they knew what the work was, beats a
        branch name or a session-id prefix at that job."""
        agent = fleet.Agent(session_id="abcdef12", project="mktplace",
                            branch="feat/x", tmux_window="pi-eval")

        assert fleet._label(agent) == "mktplace@pi-eval"

    def test_without_a_tab_the_label_is_exactly_what_it_was(self, tmp_path):
        agent = fleet.Agent(session_id="abcdef12", project="mktplace",
                            branch="feat/x")

        assert fleet._label(agent) == "mktplace@feat/x"

    def test_the_new_fields_reach_fleet_json_with_no_serialization_code(self, tmp_path):
        """`_agent_json` is `asdict(agent)`, so a field added to the dataclass
        is shipped automatically. Asserted rather than hand-wired, because
        hand-wiring is how the two drift."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))

        payload = json.loads(fleet.fleet_json(fleet.collect(tmp_path, NOW), NOW))
        entry = payload["agents"][0]

        assert entry["tmux_pane"] == "%12"
        assert entry["tmux_window"] == "pi-eval"
        assert entry["tmux_server"] == "/private/tmp/tmux-501/default"

    def test_agent_json_did_not_grow_a_special_case(self):
        src = (SCRIPTS_DIR / "lib" / "fleet.py").read_text(encoding="utf-8")
        body = src.split("def _agent_json(", 1)[1].split("\ndef ", 1)[0]

        assert "tmux" not in body


# ---------------------------------------------------------------------------
# The seen/unseen axis
# ---------------------------------------------------------------------------

def make_viewed(data_dir, pane, *, ago=timedelta(minutes=1), window="pi-eval",
                server="/private/tmp/tmux-501/default", now=NOW, lines=None):
    """Write a `tmux/viewed/<n>` marker as `fleet-viewed.sh` writes it.

    Three lines: timestamp, window name, tmux socket. *lines* overrides the
    whole body, for the malformed cases.
    """
    d = data_dir / "tmux" / "viewed"
    d.mkdir(parents=True, exist_ok=True)
    body = lines if lines is not None else [
        (now - ago).strftime("%Y-%m-%dT%H:%M:%SZ"), window, server,
    ]
    path = d / pane.lstrip("%")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _one(data_dir, *, viewed_ago, last_ago=timedelta(minutes=5), **kw):
    """One agent in one pane, with a marker at a chosen distance from its work."""
    make_session(data_dir, "a", hostname="claude-a-01", ago=last_ago)
    make_pane_map(data_dir, ("claude-a-01", "%12", "pi-eval"))
    make_viewed(data_dir, "%12", ago=viewed_ago, **kw)
    return fleet.collect(data_dir, NOW).agents[0]


class TestSeenAxis:
    """Have you looked at this tab since it last did anything?

    The question the whole fleet board exists to answer with six tabs open, and
    the one thing the container genuinely cannot know: tmux runs on the Mac.
    Two host-written files carry the halves — `tmux/panes.json` says which pane
    holds which container, `tmux/viewed/<n>` says when that pane was last on
    screen — and this module is where they meet.

    `seen` is derived at render time and stored nowhere. Both output files stay
    caches: an agent that acts after you looked at it goes unseen again on the
    next render, with nothing to invalidate.
    """

    def test_a_marker_newer_than_the_last_event_is_seen(self, tmp_path):
        agent = _one(tmp_path, last_ago=timedelta(minutes=5),
                     viewed_ago=timedelta(minutes=1))

        assert agent.seen is True
        assert agent.seen_at is not None

    def test_a_marker_older_than_the_last_event_is_not_seen(self, tmp_path):
        """The mechanism, stated as a test: the agent did something after you
        looked, so it is unseen again."""
        agent = _one(tmp_path, last_ago=timedelta(minutes=1),
                     viewed_ago=timedelta(minutes=5))

        assert agent.seen is False
        assert agent.seen_at is not None, "we know when you looked; it was just earlier"

    def test_a_marker_from_a_different_tmux_server_is_ignored(self, tmp_path):
        """tmux recycles pane ids per server. `%12` on yesterday's server is an
        unrelated pane, and crediting its attention here would make the board
        confidently wrong — worse than saying nothing."""
        agent = _one(tmp_path, viewed_ago=timedelta(minutes=1),
                     server="/private/tmp/tmux-501/other")

        assert agent.seen is False
        assert agent.seen_at is None

    def test_the_marker_is_checked_against_the_pane_s_own_server(self, tmp_path):
        """Not the map's. `panes.json` merges across tabs, so its top-level
        socket is whichever launch wrote the file last — and comparing every
        entry against that one is wrong in both directions at once. Here the
        agent sits on a second tmux server and its marker was written by that
        same server, so it *has* been looked at; checking the document value
        would deny it."""
        make_session(tmp_path, "a", hostname="claude-a-01",
                     ago=timedelta(minutes=5))
        make_pane_map(
            tmp_path,
            ("claude-a-01", "%12", "kit", "/private/tmp/tmux-501/second"),
            server="/private/tmp/tmux-501/default",
        )
        make_viewed(tmp_path, "%12", ago=timedelta(minutes=1),
                    server="/private/tmp/tmux-501/second")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.seen is True

    def test_a_marker_matching_only_the_documents_server_is_not_credited(self, tmp_path):
        """The other direction. The pane is on the second server; a marker for
        `%12` on the *default* server is a different pane entirely, and the
        document-level match is a coincidence of who wrote the file last."""
        make_session(tmp_path, "a", hostname="claude-a-01",
                     ago=timedelta(minutes=5))
        make_pane_map(
            tmp_path,
            ("claude-a-01", "%12", "kit", "/private/tmp/tmux-501/second"),
            server="/private/tmp/tmux-501/default",
        )
        make_viewed(tmp_path, "%12", ago=timedelta(minutes=1),
                    server="/private/tmp/tmux-501/default")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.seen is False
        assert agent.seen_at is None

    def test_an_agent_with_no_pane_is_never_seen(self, tmp_path):
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_viewed(tmp_path, "%12")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.seen is False
        assert agent.seen_at is None

    def test_no_marker_for_this_pane_is_not_seen(self, tmp_path):
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))
        make_viewed(tmp_path, "%99")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.seen is False

    def test_a_marker_with_no_server_line_is_dropped(self, tmp_path):
        """Without line 3 there is nothing to check the pane id against, which
        is precisely the mis-attribution the server field exists to prevent."""
        agent = _one(tmp_path, viewed_ago=timedelta(minutes=1),
                     lines=["2026-08-01T11:59:00Z", "pi-eval"])

        assert agent.seen is False

    def test_a_marker_with_an_unparseable_timestamp_is_dropped(self, tmp_path):
        agent = _one(tmp_path, viewed_ago=timedelta(minutes=1),
                     lines=["not a date", "pi-eval", "/private/tmp/tmux-501/default"])

        assert agent.seen is False

    def test_the_marker_supplies_the_tab_name(self, tmp_path):
        """`panes.json` records what the tab was called at **launch**, and only
        when the launcher could tell a human had named it. The marker records
        what it is called **now** — the `after-rename-window` hook rewrites it
        on every rename — so it is both fresher and available in cases the map
        is not.

        The map here carries the empty string the launcher wrote for a year:
        it read `automatic-rename` window-locally, so a global
        `set -g automatic-rename off` returned "" and the name was never
        recorded (kit `tmux_capture_window`, fixed separately). Every board
        fell back to `claude-a-01` for every agent. The marker was carrying the
        answer the whole time.
        """
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", ""))
        make_viewed(tmp_path, "%12", window="compactions")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_window == "compactions"

    def test_the_marker_wins_over_a_stale_pane_map_name(self, tmp_path):
        """Renaming a tab mid-session relabels it on the next render, rather
        than at the next launch."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "old-name"))
        make_viewed(tmp_path, "%12", window="new-name")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_window == "new-name"

    def test_a_marker_from_another_server_supplies_no_name_either(self, tmp_path):
        """The same check `seen` makes, for the same reason: pane ids are
        recycled per server, so this marker describes an unrelated tab. A
        label taken from one pane while attention is credited to another would
        be worse than either being absent — which is why both read through one
        function."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "from-the-map"))
        make_viewed(tmp_path, "%12", window="from-another-server",
                    server="/private/tmp/tmux-501/other")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_window == "from-the-map"
        assert agent.seen is False

    def test_the_pane_map_still_supplies_the_name_with_no_marker(self, tmp_path):
        """No tmux hooks wired is the ordinary case, not a broken one."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_window == "pi-eval"

    def test_an_empty_marker_name_falls_back_rather_than_blanking(self, tmp_path):
        """`fleet-viewed.sh` writes whatever tmux reports, and a window can be
        called the empty string. Preferring it would *lose* a label the map
        already had."""
        make_session(tmp_path, "a", hostname="claude-a-01")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))
        make_viewed(tmp_path, "%12", window="")

        agent = fleet.collect(tmp_path, NOW).agents[0]

        assert agent.tmux_window == "pi-eval"

    def test_the_tab_name_reaches_the_label_every_reader_prints(self, tmp_path):
        """The point of fixing it here rather than in one board: `_label` feeds
        `AGENTS.md`, `/fleet-status` and `fleet.json` alike."""
        make_session(tmp_path, "a", hostname="claude-a-01", project="mktplace")
        make_pane_map(tmp_path, ("claude-a-01", "%12", ""))
        make_viewed(tmp_path, "%12", window="compactions")

        payload = json.loads(fleet.fleet_json(fleet.collect(tmp_path, NOW), NOW))

        assert payload["agents"][0]["tmux_window"] == "compactions"

    def test_no_viewed_directory_is_none_not_empty(self, tmp_path):
        """The not-collected/collected-empty distinction this module runs on.
        `None` is "nobody is recording attention"; `{}` is "the hooks are wired
        and you have looked at nothing" — and only the second licenses printing
        an unseen count."""
        assert fleet.load_viewed(tmp_path) is None

        (tmp_path / "tmux" / "viewed").mkdir(parents=True)
        assert fleet.load_viewed(tmp_path) == {}

    def test_seen_reaches_fleet_json_with_no_serialization_code(self, tmp_path):
        _one(tmp_path, viewed_ago=timedelta(minutes=1))

        payload = json.loads(fleet.fleet_json(fleet.collect(tmp_path, NOW), NOW))
        entry = payload["agents"][0]

        assert entry["seen"] is True
        assert entry["seen_at"].startswith("2026-08-01T11:59:00")


class TestSeenIsAnAxisNotAStatus:
    """`status` is frozen at `working | waiting_input | idle | ended` by the
    hub's API contract, and `disposition` is how the user left a session.
    Attention is neither. Folding it into either would mean an agent's state
    changed because someone glanced at a tab.
    """

    def test_the_group_of_a_seen_agent_is_unchanged(self, tmp_path):
        make_session(tmp_path, "a", hostname="claude-a-01", kind="notification")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))
        before = fleet.collect(tmp_path, NOW).agents[0]
        make_viewed(tmp_path, "%12", ago=timedelta(minutes=1))

        after = fleet.collect(tmp_path, NOW).agents[0]

        assert after.seen is True
        assert after.group == before.group == "Needs you"
        assert after.status == before.status == "waiting_input"

    def test_status_is_still_exactly_the_four_contract_values(self, tmp_path):
        for kind, ago in (("notification", timedelta(minutes=1)),
                          ("stop", timedelta(minutes=1)),
                          ("stop", timedelta(days=2))):
            d = tmp_path / f"{kind}{ago.days}"
            make_session(d, "a", hostname="claude-a-01", kind=kind, ago=ago)
            make_pane_map(d, ("claude-a-01", "%12", "pi-eval"))
            make_viewed(d, "%12", ago=timedelta(seconds=1))

            agent = fleet.collect(d, NOW).agents[0]

            assert agent.status in {"working", "waiting_input", "idle", "ended"}

    def test_the_seen_code_path_never_touches_status(self):
        """Read from the source, not inferred: the two functions that compute
        `seen` must not mention `status` at all, in either direction."""
        src = (SCRIPTS_DIR / "lib" / "fleet.py").read_text(encoding="utf-8")
        for name in ("_seen_at(", "load_viewed("):
            # Two blank lines end a top-level definition, which is what
            # bounds the body — splitting on the next `def` would swallow the
            # module constants that sit between two functions.
            body = src.split(f"def {name}", 1)[1].split("\n\n\n", 1)[0]
            assert "status" not in body
            assert "disposition" not in body


class TestSeenOrdersButNeverHides:

    def test_unseen_sorts_before_seen_within_a_group(self, tmp_path):
        """Two agents, the *seen* one more recent — so recency alone would put
        it first. Unseen wins."""
        make_session(tmp_path, "seen-one", hostname="claude-a-01",
                     ago=timedelta(minutes=1))
        make_session(tmp_path, "unseen-one", hostname="claude-b-02",
                     ago=timedelta(minutes=9))
        make_pane_map(tmp_path,
                      ("claude-a-01", "%12", "one"),
                      ("claude-b-02", "%13", "two"))
        make_viewed(tmp_path, "%12", ago=timedelta(seconds=30))

        order = [a.session_id for a in fleet.collect(tmp_path, NOW).in_group("Working")]

        assert order == ["unseen-one", "seen-one"]

    def test_being_seen_hides_nothing(self, tmp_path):
        """Hiding is what `Idle` already does. Doing it twice, for two
        unrelated reasons, is how a board starts lying."""
        make_session(tmp_path, "a", hostname="claude-a-01", project="mktplace")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))
        make_viewed(tmp_path, "%12", ago=timedelta(seconds=30))

        f = fleet.collect(tmp_path, NOW)

        assert len(f.in_group("Working")) == 1
        assert "mktplace" in fleet.render_agents_md(f, NOW)

    def test_recency_still_decides_between_two_unseen_agents(self, tmp_path):
        make_session(tmp_path, "older", hostname="claude-a-01",
                     ago=timedelta(minutes=9))
        make_session(tmp_path, "newer", hostname="claude-b-02",
                     ago=timedelta(minutes=1))

        order = [a.session_id for a in fleet.collect(tmp_path, NOW).in_group("Working")]

        assert order == ["newer", "older"]


class TestSeenRendering:

    def test_agents_md_marks_a_seen_agent(self, tmp_path):
        make_session(tmp_path, "a", hostname="claude-a-01", project="mktplace")
        make_pane_map(tmp_path, ("claude-a-01", "%12", "pi-eval"))
        make_viewed(tmp_path, "%12", ago=timedelta(minutes=2))

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "· seen 2m ago" in md

    def test_it_marks_seen_rather_than_unseen(self, tmp_path):
        """Unseen is the default wherever nobody records attention, so marking
        it would badge every row of a vanilla board with something that means
        nothing there. Marking *seen* states what only evidence can state."""
        make_session(tmp_path, "a", hostname="claude-a-01", project="mktplace")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "seen" not in md
        assert "unseen" not in md


# ---------------------------------------------------------------------------
# Vanilla degradation — no kit, no tmux, no map
# ---------------------------------------------------------------------------

VANILLA_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
VANILLA_STAMP = "2026-08-01T12:00:00+00:00"
VANILLA_DIR = Path(__file__).resolve().parent / "fixtures" / "fleet_vanilla"


def build_vanilla_data_dir(data_dir):
    """A fixed fleet exercising every branch the renderers take.

    A working session, one waiting on the user, one with no checkpoint at all,
    and two agents holding the same file. `cwd` points at paths that do not
    exist, so `_git_info` returns empty strings on any machine and the render
    is a function of this function alone.
    """
    sessions = data_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)

    def entry(sid, *, kind, ago_minutes, project, hostname, cwd):
        ts = (VANILLA_NOW - timedelta(minutes=ago_minutes)).isoformat()
        (sessions / f"{sid}.json").write_text(json.dumps({
            "session_id": sid,
            "hostname": hostname,
            "cwd": cwd,
            "project": project,
            "workspace": "/vanilla",
            "started_at": ts,
            "in_container": True,
            "last_event": {"ts": ts, "kind": kind},
        }, indent=2), encoding="utf-8")

    def checkpoint(sid, intent, nxt, files):
        d = data_dir / "checkpoints" / sid
        d.mkdir(parents=True, exist_ok=True)
        text = "".join(f"## {s}\n\nplaceholder\n" for s in cp.CHECKPOINT_SECTIONS)
        text = text.replace("## Current intent\n\nplaceholder\n",
                            f"## Current intent\n\n{intent}\n")
        text = text.replace("## Next action\n\nplaceholder\n",
                            f"## Next action\n\n{nxt}\n")
        listing = "\n".join(f"- `{f}` — because reasons" for f in files)
        text = text.replace("## Involved files\n\nplaceholder\n",
                            f"## Involved files\n\n{listing}\n")
        (d / "checkpoint.md").write_text(text, encoding="utf-8")

    entry("vanilla-working", kind="stop", ago_minutes=3,
          project="mktplace", hostname="claude-a-01", cwd="/vanilla/mktplace")
    checkpoint("vanilla-working", "Wiring fleet.json into the hook path",
               "Run the mktplace gates", ["/vanilla/mktplace/lib/fleet.py"])

    entry("vanilla-waiting", kind="notification", ago_minutes=18,
          project="kit", hostname="claude-b-02", cwd="/vanilla/kit")
    checkpoint("vanilla-waiting", "Writing write_pane_map",
               "Approve the edit to lib/fleet.py",
               ["/vanilla/kit/lib/fleet.py", "/vanilla/kit/claude.sh"])

    entry("vanilla-collide", kind="stop", ago_minutes=9,
          project="mktplace", hostname="claude-d-04", cwd="/vanilla/mktplace-wt")
    checkpoint("vanilla-collide", "Adding the seen axis",
               "Extend fleet_digest", ["/vanilla/mktplace/lib/fleet.py"])

    entry("vanilla-bare", kind="stop", ago_minutes=41,
          project="workspace", hostname="claude-c-03", cwd="/vanilla/workspace")

    (data_dir / "live_containers.json").write_text(json.dumps({
        "version": 1,
        "observed_at": (VANILLA_NOW - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "observer": "host",
        "kind": "container",
        "ids": ["claude-a-01", "claude-b-02", "claude-c-03", "claude-d-04"],
    }, indent=2), encoding="utf-8")
    return data_dir


class TestVanillaDegradation:
    """With no `tmux/panes.json` and no `tmux/viewed/`, both renderings must be
    what they were before any of this landed.

    The fixtures under `fixtures/fleet_vanilla/` were generated on `main`
    **before the first change of this plan**, which is the only way a
    byte-comparison proves anything: a golden captured afterwards would pin
    whatever the code happened to do, including the bug.
    """

    def test_agents_md_is_byte_identical_to_the_pre_change_capture(self, tmp_path):
        build_vanilla_data_dir(tmp_path)

        rendered = fleet.render_agents_md(
            fleet.collect(tmp_path, VANILLA_NOW), VANILLA_NOW,
            generated_at=VANILLA_STAMP)

        assert rendered == (VANILLA_DIR / "AGENTS.md").read_text(encoding="utf-8")

    def test_fleet_json_differs_from_the_pre_change_capture_by_one_added_key(
            self, tmp_path):
        """`collected_at` is what the previous work item added, deliberately and
        additively. Nothing else may have moved — so this asserts the exact
        shape of the difference rather than allowing any difference at all."""
        build_vanilla_data_dir(tmp_path)

        payload = json.loads(fleet.fleet_json(
            fleet.collect(tmp_path, VANILLA_NOW), VANILLA_NOW,
            generated_at=VANILLA_STAMP))
        golden = json.loads((VANILLA_DIR / "fleet.json").read_text(encoding="utf-8"))

        assert set(payload) - set(golden) == {"collected_at"}
        assert payload.pop("collected_at") == {}
        # Every agent grew the three tmux fields and the seen pair. All five
        # are inert with no map and no markers, which is the claim: the
        # additions are visible to a consumer and change nothing for one that
        # ignores them.
        for entry in payload["agents"]:
            assert entry.pop("tmux_pane") == ""
            assert entry.pop("tmux_window") == ""
            assert entry.pop("tmux_server") == ""
            assert entry.pop("seen") is False
            assert entry.pop("seen_at") is None

        assert payload == golden

    def test_an_absent_map_changes_nothing_a_present_one_would(self, tmp_path):
        """Belt and braces on the same claim, without a golden: rendering with
        no map at all and rendering with an empty map agree."""
        build_vanilla_data_dir(tmp_path)
        without = fleet.render_agents_md(
            fleet.collect(tmp_path, VANILLA_NOW), VANILLA_NOW,
            generated_at=VANILLA_STAMP)
        make_pane_map(tmp_path)

        with_empty = fleet.render_agents_md(
            fleet.collect(tmp_path, VANILLA_NOW), VANILLA_NOW,
            generated_at=VANILLA_STAMP)

        assert with_empty == without
