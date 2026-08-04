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

    def test_quiet_for_a_day_means_idle(self, tmp_path):
        make_session(tmp_path, "quiet", kind="stop", ago=timedelta(hours=30))

        f = fleet.collect(tmp_path, NOW)

        assert f.agents[0].status == "idle"
        assert "## Idle (1)" in fleet.render_agents_md(f, NOW)

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

    def test_an_idle_session_is_still_listed(self, tmp_path):
        """Not counted is not the same as not shown — the full read is where
        you go looking for the tab you forgot about."""
        make_session(tmp_path, "quiet", project="forgotten", ago=timedelta(days=3))

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "## Idle (1)" in md
        assert "forgotten" in md

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

    def test_the_gate_is_the_idle_window(self, tmp_path):
        """Just inside COLLISION_MAX_AGE_HOURS still collides — the cut is at
        the window, not somewhere vaguely near it."""
        inside = timedelta(hours=fleet.COLLISION_MAX_AGE_HOURS) - timedelta(minutes=1)
        make_session(tmp_path, "a", ago=inside)
        make_session(tmp_path, "b", ago=inside)
        make_checkpoint(tmp_path, "a", files=("/work/knowhere/src/shared.py",))
        make_checkpoint(tmp_path, "b", files=("/work/knowhere/src/shared.py",))

        assert len(fleet.collect(tmp_path, NOW).collisions) == 1

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
# fleet.txt is retired — the fronts count survives in `Fleet.fronts`
# ---------------------------------------------------------------------------

class TestFrontsCount:

    def test_idle_is_listed_but_is_not_a_front(self, tmp_path):
        for i in range(4):
            make_session(tmp_path, f"w{i}", ago=timedelta(hours=1))
        make_session(tmp_path, "n1", kind="notification")
        make_session(tmp_path, "n2", kind="notification")
        make_session(tmp_path, "old", ago=timedelta(days=3))

        f = fleet.collect(tmp_path, NOW)

        # Seven sessions, six fronts: `old` went quiet three days ago, so it
        # is listed in AGENTS.md as idle and excluded from the fronts.
        assert len(f.agents) == 7
        assert len(f.fronts) == 6
        assert len(f.in_group("Needs you")) == 2

    def test_a_leftover_fleet_txt_is_deleted_on_write(self, tmp_path):
        """A pre-digest release wrote it and the kit status line renders
        whatever it says; a frozen count in every tab is worse than none."""
        make_session(tmp_path, "a")
        (tmp_path / "fleet.txt").write_text("9 fronts · 4 need you\n")

        fleet.write_fleet_view(tmp_path, NOW)

        assert not (tmp_path / "fleet.txt").exists()

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

    def test_entries_carry_intent_next_action_and_files(self, tmp_path):
        make_session(tmp_path, "a", project="alpha")
        make_checkpoint(
            tmp_path, "a",
            intent="Rewiring the drain", nxt="Run the gates",
            files=("/work/knowhere/src/x.py",),
        )

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "**Doing:** Rewiring the drain" in md
        assert "**Next:** Run the gates" in md
        assert "`/work/knowhere/src/x.py`" in md

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
