"""Tests for the re-learn report — the count the pipeline is told to discard.

The property under test is not "does it group text". It is that a rule memory
already held, and that a session derived again, survives long enough to be
read — and that the report never claims more about *why* than the telemetry
can carry.
"""

import json

import pytest

from lib import relearns


def drop(target="git-policy.md", title="Worktree location convention",
         text="Worktrees live under $WORKSPACE/.worktrees.", kind="RULE",
         source="2026-08-06.md:12", ts="2026-08-10T20:28:34+00:00",
         reason="redundant", judge_reason="restates existing entry"):
    return {
        "target": target, "title": title, "text": text, "kind": kind,
        "source": source, "ts": ts, "reason": reason,
        "judge_reason": judge_reason,
    }


def session(day="2026-08-06", injected=("git-policy.md",), used=()):
    return {
        "ts": f"{day}T09:00:00Z",
        "injected": [{"file": f, "section": None} for f in injected],
        "judge": [{"file": f, "used": f in used} for f in injected],
    }


class TestWhatCounts:
    def test_only_redundant_drops_are_a_retention_signal(self):
        # judge-drop means the judge thought the item wrong or thin. That is a
        # drafting problem and belongs in a different report.
        records = [drop(), drop(reason="judge-drop")]
        assert len(relearns.redundant_records(records)) == 1

    def test_rules_only_by_default(self):
        records = [drop(), drop(kind="FACT")]
        assert len(relearns.redundant_records(records)) == 1

    def test_all_kinds_is_available_and_opt_in(self):
        records = [drop(), drop(kind="FACT"), drop(kind="DECISION")]
        assert len(relearns.redundant_records(records, kinds=None)) == 3

    def test_kind_matching_is_case_insensitive(self):
        assert len(relearns.redundant_records([drop(kind="rule")])) == 1


class TestLearningDate:
    def test_it_reads_the_date_from_the_cited_filename(self):
        # The learnings FILE is deleted by /dream-remember step 5; the date in
        # the citation is the only thing left to join on.
        item = relearns.redundant_records([drop(source="2026-08-06.md:442")])[0]
        assert item.learning_date == "2026-08-06"

    def test_a_line_range_still_yields_the_date(self):
        item = relearns.redundant_records([drop(source="2026-08-06.md:278-279")])[0]
        assert item.learning_date == "2026-08-06"

    def test_an_unparseable_source_is_blank_not_a_guess(self):
        item = relearns.redundant_records([drop(source="")])[0]
        assert item.learning_date == ""


class TestGrouping:
    def test_the_same_rule_reworded_is_one_group_with_a_count(self):
        # Both are the worktree-location rule; neither is a copy of the other.
        # Titles alone score 0.0 here — under MIN_CONTENT_WORDS — which is
        # exactly why the clustering scores title and text together.
        records = [
            drop(title="Worktree location convention",
                 text="All worktrees live under $WORKSPACE/.worktrees, created "
                      "with git worktree add, never scattered inside a project "
                      "directory."),
            drop(title="Worktrees belong outside the repo tree",
                 text="Every worktree lives under $WORKSPACE/.worktrees and is "
                      "created with git worktree add; never place one inside a "
                      "project directory."),
        ]
        groups = relearns.group_relearns(relearns.redundant_records(records))
        assert len(groups) == 1
        assert groups[0].count == 2

    def test_unrelated_rules_in_one_file_stay_apart(self):
        records = [
            drop(title="Container git remotes must be HTTPS",
                 text="Remotes configured inside the container must use HTTPS "
                      "URLs; an SSH remote cannot authenticate from there."),
            drop(title="Never bypass a pre-commit hook",
                 text="Fix whatever the hook is complaining about instead of "
                      "passing --no-verify to get the commit through."),
        ]
        groups = relearns.group_relearns(relearns.redundant_records(records))
        assert len(groups) == 2

    def test_identical_text_in_different_files_is_not_merged(self):
        # Same words in two files is two rules with a duplication problem, not
        # one rule. Merging them would hide which file to fix.
        records = [drop(target="git-policy.md"), drop(target="dev.md")]
        groups = relearns.group_relearns(relearns.redundant_records(records))
        assert len(groups) == 2

    def test_most_relearned_sorts_first(self):
        records = [
            drop(title="Container git remotes must be HTTPS",
                 text="Remotes configured inside the container must use HTTPS "
                      "URLs; an SSH remote cannot authenticate from there."),
            drop(title="Worktree location convention",
                 text="All worktrees live under $WORKSPACE/.worktrees, created "
                      "with git worktree add, never inside a project."),
            drop(title="Worktrees belong outside the repo tree",
                 text="Every worktree lives under $WORKSPACE/.worktrees and is "
                      "created with git worktree add, never inside a project."),
        ]
        groups = relearns.group_relearns(relearns.redundant_records(records))
        assert groups[0].count == 2


class TestInjectionIndex:
    def test_it_maps_day_to_files_and_whether_they_were_used(self):
        index = relearns.injection_index([
            session(day="2026-08-06", injected=("git-policy.md", "dev.md"),
                    used=("dev.md",)),
        ])
        assert index["2026-08-06"]["git-policy.md"] is False
        assert index["2026-08-06"]["dev.md"] is True

    def test_used_anywhere_that_day_wins(self):
        # OR over the day is the direction that avoids accusing a rule of being
        # ignored when some session did use its file.
        index = relearns.injection_index([
            session(day="2026-08-06", injected=("git-policy.md",)),
            session(day="2026-08-06", injected=("git-policy.md",),
                    used=("git-policy.md",)),
        ])
        assert index["2026-08-06"]["git-policy.md"] is True

    def test_missing_or_null_lists_do_not_raise(self):
        index = relearns.injection_index([
            {"ts": "2026-08-06T09:00:00Z", "injected": None, "judge": None},
            {"ts": "", "injected": [{"file": "x.md"}]},
        ])
        assert index == {"2026-08-06": {}}


class TestDiagnosis:
    def test_never_injected_is_a_routing_hole(self):
        groups = relearns.analyse(
            [drop()],
            [session(day="2026-08-06", injected=("dev.md",))],
        )
        assert groups[0].verdict == relearns.NOT_ROUTED

    def test_injected_and_unused_is_the_rule_being_ignored(self):
        groups = relearns.analyse(
            [drop()],
            [session(day="2026-08-06", injected=("git-policy.md",))],
        )
        assert groups[0].verdict == relearns.ROUTED_IGNORED

    def test_injected_and_used_declines_to_answer(self):
        # File-level "used" cannot say whether THIS rule fired. Inventing a
        # verdict from it is the failure the pipeline exists to prevent.
        groups = relearns.analyse(
            [drop()],
            [session(day="2026-08-06", injected=("git-policy.md",),
                     used=("git-policy.md",))],
        )
        assert groups[0].verdict == relearns.INCONCLUSIVE

    def test_no_telemetry_for_that_day_is_inconclusive_and_says_so(self):
        groups = relearns.analyse([drop()], [session(day="2026-07-01")])
        assert groups[0].verdict == relearns.INCONCLUSIVE
        assert groups[0].evidence == relearns.NO_TELEMETRY

    def test_a_routing_hole_on_any_occasion_outranks_the_rest(self):
        records = [
            drop(source="2026-08-06.md:1",
                 text="All worktrees live under $WORKSPACE/.worktrees, created "
                      "with git worktree add, never inside a project."),
            drop(source="2026-08-07.md:1",
                 text="Every worktree lives under $WORKSPACE/.worktrees and is "
                      "created with git worktree add, never inside a project."),
        ]
        groups = relearns.analyse(records, [
            session(day="2026-08-06", injected=("git-policy.md",)),
            session(day="2026-08-07", injected=("dev.md",)),
        ])
        assert groups[0].count == 2
        assert groups[0].verdict == relearns.NOT_ROUTED

    def test_evidence_is_labelled_same_day_never_the_session(self):
        groups = relearns.analyse(
            [drop()], [session(day="2026-08-06", injected=("git-policy.md",))]
        )
        assert groups[0].evidence == relearns.SAME_DAY


class TestRendering:
    def test_an_empty_report_says_so_rather_than_rendering_nothing(self):
        # A missing section reads as "not run". Those are different facts.
        out = relearns.render_section([])
        assert "## Rules Re-learned" in out
        assert "No rule in memory was re-derived" in out

    def test_the_count_survives_into_the_table(self):
        groups = relearns.analyse(
            [drop(text="All worktrees live under $WORKSPACE/.worktrees, "
                       "created with git worktree add, never in a project."),
             drop(text="Every worktree lives under $WORKSPACE/.worktrees and "
                       "is created with git worktree add, never in a project.")],
            [session(day="2026-08-06", injected=("git-policy.md",))],
        )
        out = relearns.render_section(groups)
        assert "| 2 |" in out
        assert "routed-but-ignored" in out

    def test_a_pipe_in_a_title_cannot_break_the_table(self):
        groups = relearns.analyse([drop(title="use A | not B")], [])
        out = relearns.render_section(groups)
        row = [ln for ln in out.splitlines() if "not B" in ln][0]
        # The pipe is escaped, so markdown renders it as text rather than as a
        # sixth column.
        assert "use A \\| not B" in row
        assert row.count("|") - row.count("\\|") == 6

    def test_the_limit_is_reported_when_it_truncates(self):
        records = [
            drop(title="Container git remotes must be HTTPS",
                 text="Remotes configured inside the container use HTTPS URLs; "
                      "an SSH remote cannot authenticate from there."),
            drop(title="Never bypass a pre-commit hook",
                 text="Fix whatever the hook complains about instead of passing "
                      "--no-verify to force the commit through."),
            drop(title="Absolute paths for git worktree add",
                 text="Pass an absolute destination to git worktree add; a "
                      "relative one resolves against the wrong directory."),
            drop(title="Security commits stay unbundled",
                 text="A commit fixing a vulnerability carries nothing else, so "
                      "it can be cherry-picked and audited on its own."),
            drop(title="Tag only after verifying the version string",
                 text="Read the version out of the target commit before cutting "
                      "an annotated tag; never trust the working tree."),
        ]
        out = relearns.render_section(relearns.analyse(records, []), limit=2)
        assert "3 more not shown" in out


class TestUtilisationReader:
    def test_a_torn_line_costs_that_row_and_not_the_report(self, tmp_path):
        path = tmp_path / "utilisation.jsonl"
        path.write_text(
            json.dumps(session()) + "\n" + '{"ts": "2026-08-06T0\n',
            encoding="utf-8",
        )
        assert len(relearns.read_utilisation(path)) == 1

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert relearns.read_utilisation(tmp_path / "nope.jsonl") == []


class TestDreamWiring:
    """The section must be wired into dream.py's proposal generation, fail-open."""

    def setup_method(self):
        from pathlib import Path
        plugin_root = Path(__file__).parent.parent
        self.source = (plugin_root / "scripts" / "dream.py").read_text()
        self.skill = (plugin_root / "skills" / "dream" / "SKILL.md").read_text()

    def test_both_proposal_paths_append_the_section(self):
        # dream.py drafts in one shot and in chunks; a section wired into only
        # one of them is absent from half the runs.
        assert self.source.count("_with_relearns(_with_routing_warnings(") == 2

    def test_it_is_fail_open_and_loud(self):
        # A diagnostic crash must never lose a generated proposal.
        assert "WITHOUT a Rules Re-learned section" in self.source

    def test_it_is_registered_as_regenerated(self):
        # Derived from current state each run, so a folded proposal carrying
        # yesterday's copy would put two of these in one document.
        marker = self.source.split("_REGENERATED_SECTIONS = ")[1][:200]
        assert '"## Rules Re-learned"' in marker

    def test_the_skill_tells_the_reader_what_it_is(self):
        assert "## Rules Re-learned" in self.skill
        assert "relearn_report.py" in self.skill

    def test_the_skill_does_not_ask_for_approval_of_diagnostics(self):
        # These are not proposal items; folding them into the disposition
        # counts would invite a reviewer to approve or reject a measurement.
        assert "Do not fold them into the disposition counts." in self.skill


class TestCliSurface:
    def test_it_runs_read_only_against_fixture_logs(self, tmp_path):
        import subprocess
        import sys
        from pathlib import Path

        scripts = Path(__file__).parent.parent / "scripts"
        rejections_path = tmp_path / "rejections.jsonl"
        rejections_path.write_text(json.dumps(drop()) + "\n", encoding="utf-8")
        utilisation_path = tmp_path / "utilisation.jsonl"
        utilisation_path.write_text(json.dumps(session()) + "\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(scripts / "relearn_report.py"),
             "--rejections", str(rejections_path),
             "--utilisation", str(utilisation_path), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload[0]["verdict"] == relearns.ROUTED_IGNORED
        # Read-only: the logs it was pointed at are untouched.
        assert json.loads(rejections_path.read_text()) == drop()
