"""The digest: ranking, the cap, honest gaps, and the AGENTS.md/JSON contract."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from lib.fleet import Agent, Collision, Fleet, fleet_json, render_agents_md
from lib.fleet_digest import MAX_ITEMS, WAITING_STALE_HOURS, rank, render_digest
from lib.fleet_sources.backlog import Backlog
from lib.fleet_sources.git_repos import RepoState
from lib.fleet_sources.jobs import BackgroundJob
from lib.fleet_sources.prs import PRScan, PullRequest

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def agent(status="waiting_input", minutes=5, **kw):
    kw.setdefault("session_id", f"s{minutes}")
    kw.setdefault("project", "proj")
    kw.setdefault("next_action", "answer the question")
    return Agent(status=status, last_ts=NOW - timedelta(minutes=minutes), **kw)


def pr(**kw):
    base = dict(repo="o/r", number=1, title="t", head="h", base="main")
    base.update(kw)
    return PullRequest(**base)


def fleet(agents=(), collisions=(), **kw):
    return Fleet(agents=list(agents), collisions=list(collisions), **kw)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def test_approved_pr_outranks_red_ci_outranks_waiting_session():
    """The whole product is this ordering: what is blocked on a human decision."""
    scan = PRScan(prs=[
        pr(number=7, ci="failing"),
        pr(number=9, review_decision="APPROVED", head="h9"),
    ])
    items, _ = rank(fleet([agent()], prs=scan), NOW)
    assert "approved" in items[0].text and "#9" in items[0].text
    assert "CI red" in items[1].text
    assert "answer the question" in items[2].text


def test_a_stack_is_one_line_not_four():
    """Four PRs that must land in order are one decision."""
    chain = [
        pr(number=1, head="a", base="main"),
        pr(number=2, head="b", base="a"),
        pr(number=3, head="c", base="b"),
        pr(number=4, head="d", base="c"),
    ]
    items, _ = rank(fleet(prs=PRScan(prs=chain)), NOW)
    assert len(items) == 1
    assert "#1→#2→#3→#4" in items[0].text and "4 PRs" in items[0].text


def test_waiting_sessions_are_ranked_youngest_first():
    """A two-minute-old question is a live conversation; a six-hour one is not."""
    items, _ = rank(fleet([agent(minutes=300, session_id="old"),
                           agent(minutes=2, session_id="new")]), NOW)
    assert "2m" in items[0].text and "just now" not in items[0].text
    ages = [i.sort_key[0] for i in items]
    assert ages == sorted(ages)


def test_a_session_without_a_project_is_named_once():
    """`hostname \\`hostname\\`` said the same thing twice and read like a bug."""
    items, _ = rank(fleet([agent(project="", hostname="box-7")]), NOW)
    assert items[0].text.startswith("`box-7` — ")


def test_a_session_with_a_project_names_both():
    items, _ = rank(fleet([agent(project="knowhere", hostname="box-7")]), NOW)
    assert items[0].text.startswith("knowhere `box-7` — ")


def test_the_pointer_names_the_command_that_exists():
    """`/fleet-status` is not invocable; the plugin-qualified form is."""
    assert "/multiplai-context:fleet-status --full" in render_digest(fleet(), NOW)


def test_waiting_past_the_cutoff_becomes_a_stale_count():
    """12h, confirmed with the user: past it, a prompt is an abandoned tab."""
    old = agent(minutes=int(WAITING_STALE_HOURS * 60) + 1, session_id="stale")
    items, stale = rank(fleet([old, agent(minutes=1)]), NOW)
    assert stale == 1 and len(items) == 1


def test_bot_prs_never_reach_the_ranked_list():
    scan = PRScan(prs=[pr(number=5, is_bot=True, review_decision="APPROVED")])
    items, _ = rank(fleet(prs=scan), NOW)
    assert items == []


def test_draft_prs_never_reach_the_ranked_list():
    scan = PRScan(prs=[pr(number=5, is_draft=True, review_decision="APPROVED")])
    assert rank(fleet(prs=scan), NOW)[0] == []


def test_approved_but_red_ranks_as_red():
    """Approved is only actionable if it can actually merge."""
    scan = PRScan(prs=[pr(number=5, review_decision="APPROVED", ci="failing")])
    (item,) = rank(fleet(prs=scan), NOW)[0]
    assert "CI red" in item.text


def test_stack_head_approved_but_red_reports_red():
    """Same rule as a single PR: "approved" over red CI invites a bouncing click."""
    chain = [
        pr(number=1, head="a", base="main", review_decision="APPROVED", ci="failing"),
        pr(number=2, head="b", base="a"),
    ]
    (item,) = rank(fleet(prs=PRScan(prs=chain)), NOW)[0]
    assert "CI failing" in item.text and "approved" not in item.text


def test_collisions_rank_last_but_always_appear():
    items, _ = rank(
        fleet([agent()], collisions=[Collision("x.py", ["a", "b"], ["p@a", "p@b"])]),
        NOW,
    )
    assert "collision" in items[-1].text


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------

def test_digest_caps_the_urgent_list_and_says_how_many_it_hid():
    agents = [agent(minutes=i + 1, session_id=f"s{i}") for i in range(MAX_ITEMS + 4)]
    text = render_digest(fleet(agents), NOW)
    assert f"NEEDS YOU ({MAX_ITEMS + 4})" in text
    assert "+4 more" in text
    assert len([ln for ln in text.splitlines() if ln.startswith(" ")]) == MAX_ITEMS + 1


def test_long_next_actions_are_clipped():
    """A checkpoint's next action runs to three lines; eight of those is a wall."""
    text = render_digest(fleet([agent(next_action="word " * 200)]), NOW)
    assert "…" in text
    assert max(len(ln) for ln in text.splitlines()) < 200


def test_empty_fleet_says_so_plainly():
    assert "NEEDS YOU (0)" in render_digest(fleet(), NOW)


# ---------------------------------------------------------------------------
# The in-flight line — one total, then its breakdown
# ---------------------------------------------------------------------------

def test_in_flight_counts_every_live_agent_not_just_the_working_ones():
    """The bug this replaces: a session that stops to ask a question leaves
    `Working` for `Needs you`, so four live containers rendered as
    `RUNNING (2)` and every reading of that line was low."""
    agents = [
        agent(status="waiting_input", minutes=1, session_id="w1"),
        agent(status="waiting_input", minutes=2, session_id="w2"),
        agent(status="working", minutes=3, session_id="k1"),
        Agent(session_id="p1", status="ended", disposition="parked",
              last_ts=NOW - timedelta(hours=3)),
    ]

    text = render_digest(fleet(agents), NOW)

    assert "IN FLIGHT (4)" in text
    assert "2 waiting on you" in text
    assert "1 working" in text
    assert "1 parked" in text


def test_the_breakdown_omits_empty_categories():
    text = render_digest(fleet([agent(status="working", session_id="k1")]), NOW)

    assert "IN FLIGHT (1)" in text
    assert "1 working" in text
    assert "waiting on you" not in text
    assert "parked" not in text


def test_idle_is_not_in_flight_but_is_still_counted():
    """Idle is a guess at death, so it must not inflate the fleet size — but a
    fleet that has gone entirely quiet still has to say so."""
    idle = agent(status="idle", minutes=60 * 30, session_id="old")

    text = render_digest(fleet([idle, agent(status="working", session_id="k")]), NOW)

    assert "IN FLIGHT (1)" in text
    assert "1 idle (oldest " in text


def test_an_empty_fleet_says_zero_in_flight():
    assert "IN FLIGHT (0)" in render_digest(fleet(), NOW)


# ---------------------------------------------------------------------------
# The unseen count — only when something is recording attention
# ---------------------------------------------------------------------------

def test_the_unseen_count_appears_when_attention_is_recorded():
    agents = [
        agent(status="working", minutes=1, session_id="k1", seen=True),
        agent(status="working", minutes=2, session_id="k2"),
        agent(status="waiting_input", minutes=3, session_id="w1"),
    ]

    text = render_digest(fleet(agents, viewed_known=True), NOW)

    assert "2 unseen" in text


def test_no_unseen_count_without_a_viewed_directory():
    """Unseen is the default when nobody is recording, so on a vanilla install
    this count would just restate the in-flight count while implying somebody
    watched you not look at them. `viewed_known` is the same not-collected /
    collected-empty distinction the rest of this module runs on."""
    text = render_digest(fleet([agent(status="working", session_id="k1")]), NOW)

    assert "unseen" not in text


def test_a_fully_seen_fleet_prints_no_unseen_count():
    agents = [agent(status="working", minutes=1, session_id="k1", seen=True)]

    text = render_digest(fleet(agents, viewed_known=True), NOW)

    assert "unseen" not in text
    assert "IN FLIGHT (1)" in text


# ---------------------------------------------------------------------------
# Honest gaps — "not collected" must never print as "none"
# ---------------------------------------------------------------------------

def test_uncollected_sources_are_absent_not_zero():
    """The hook path collects none of these; silence is the only honest output."""
    text = render_digest(fleet([agent()]), NOW)
    assert "PRs open" not in text
    assert "Repos (" not in text
    assert "Backlog:" not in text


def test_scheduled_routines_report_not_tracked():
    """Server-side; a script cannot enumerate them, so it must not imply zero."""
    assert "Scheduled: not tracked" in render_digest(fleet(), NOW)


def test_unavailable_gh_reports_not_read_never_none():
    text = render_digest(fleet(prs=PRScan(available=False)), NOW)
    assert "gh unavailable" in text and "PRs open (0" not in text


def test_no_access_repos_are_reported_apart_from_errors():
    scan = PRScan(prs=[pr()], no_access=["o/x", "o/y"], errors={"o/z": "timed out"})
    text = render_digest(fleet(prs=scan), NOW)
    assert "2 not visible to this token" in text
    assert "1 repo(s) unreachable" in text


# ---------------------------------------------------------------------------
# One collection, three renderings
# ---------------------------------------------------------------------------

def _rich_fleet():
    return fleet(
        [agent(), agent(status="working", minutes=30, session_id="w")],
        collisions=[Collision("x.py", ["a", "b"], ["p@a", "p@b"])],
        prs=PRScan(prs=[pr(number=3, review_decision="APPROVED")]),
        repos=[RepoState(path="proj", slug="o/r", branch="main", dirty=2)],
        jobs=[BackgroundJob(short="aaa", state="running", updated=NOW)],
        backlog=Backlog(learnings_lines=12, inbox_items=3),
    )


@pytest.mark.parametrize("heading", [
    "Pull requests", "Repos", "Background jobs", "Backlog", "Collisions",
])
def test_every_digest_section_exists_in_the_full_report(heading):
    """The digest is a summary of AGENTS.md, never a second opinion about it."""
    assert f"## {heading}" in render_agents_md(_rich_fleet(), NOW)


def test_worktrees_are_listed_even_when_every_repo_is_clean():
    """A clean repo with a stale linked worktree is still work in flight."""
    clean = RepoState(path="proj", slug="o/r", branch="main",
                      worktrees=["/ws/.worktrees/feat-x"])
    text = render_agents_md(fleet(repos=[clean]), NOW)
    assert "All 1 checkout(s) clean" in text
    assert "1 linked worktree(s)" in text and ".worktrees/feat-x" in text


def test_agents_md_omits_sections_that_were_not_collected():
    """This is what keeps the hook-path output byte-identical to its old shape."""
    text = render_agents_md(fleet([agent()]), NOW)
    for heading in ("Pull requests", "Repos", "Background jobs", "Backlog"):
        assert f"## {heading}" not in text


def test_fleet_json_round_trips_and_carries_derived_rules():
    """The hub must not re-derive `front`/`group` and get them subtly different."""
    payload = json.loads(fleet_json(_rich_fleet(), NOW))
    assert payload["version"] == 1
    assert payload["counts"]["needs_you"] == 1
    entry = payload["agents"][0]
    assert entry["group"] in {"Needs you", "Working"}
    assert isinstance(entry["front"], bool) and isinstance(entry["age_seconds"], int)
    assert payload["prs"]["prs"][0]["number"] == 3
    assert payload["repos"][0]["dirty"] == 2


def test_fleet_json_preserves_null_for_uncollected_sources():
    payload = json.loads(fleet_json(fleet([agent()]), NOW))
    assert payload["prs"] is None
    assert payload["repos"] is None
    assert payload["scheduled"] is None
