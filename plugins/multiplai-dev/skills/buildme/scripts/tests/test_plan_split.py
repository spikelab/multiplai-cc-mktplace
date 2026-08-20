"""Tests for plan_split — is this ticket too big for one branch? (W14)

The bar these tests defend is the plan's own argument: block count is the wrong
primary metric. Twelve independent blocks must come out *splittable* and six
chained blocks must come out *atomic*, and sizing on count alone would get both
backwards. The three named cases are the independent fan-out, the fully chained
set, and migration-plus-feature (which fires at any size); the empty and
single-block sets are here because a partitioner that crashes on them is
useless to a caller reading a half-parsed tasks.md.
"""

import pytest

from build_pipeline.models import BlockInfo
from build_pipeline.plan_split import (
    HIGH_RISK_KEYWORDS,
    assess_plan_split,
    check_atomicity,
    normalize_signature,
    package_spread,
    partition_blocks,
    signature_keys,
    top_level_packages,
)


def _block(number, name, description="", produces=(), consumes=()):
    return BlockInfo(
        number=number,
        name=name,
        description=description,
        produces=list(produces),
        consumes=list(consumes),
    )


def _fan_out(count=12):
    """`count` blocks that share no signature at all — the splittable case."""
    return [
        _block(
            i,
            f"Widget {i}",
            description=f"Render widget {i} in ui/widget_{i}.py",
            produces=[f"render_widget_{i}(data: dict) -> str"],
        )
        for i in range(1, count + 1)
    ]


def _chained(count=6):
    """Each block consumes the previous block's output — the atomic case."""
    blocks = [_block(1, "Parse", produces=["parse(raw: str) -> Doc"])]
    for i in range(2, count + 1):
        blocks.append(_block(
            i,
            f"Stage {i}",
            produces=[f"stage_{i}(doc: Doc) -> Doc"],
            consumes=[
                "parse(raw: str) -> Doc" if i == 2
                else f"stage_{i - 1}(doc: Doc) -> Doc"
            ],
        ))
    return blocks


# --- Case 1: independent fan-out ---------------------------------------------

class TestIndependentFanOut:
    def test_twelve_independent_blocks_split_into_twelve_groups(self):
        proposal = partition_blocks(_fan_out(12))
        assert proposal.group_count == 12
        assert proposal.splittable is True
        assert [g.block_numbers for g in proposal.groups] == [[i] for i in range(1, 13)]

    def test_every_proposed_cut_is_clean(self):
        proposal = partition_blocks(_fan_out(12))
        assert len(proposal.boundaries) == 11
        assert all(b.is_clean for b in proposal.boundaries)
        assert all(b.crossing_signatures == [] for b in proposal.boundaries)

    def test_boundary_names_the_blocks_and_the_signature_surface(self):
        proposal = partition_blocks(_fan_out(3))
        first = proposal.boundaries[0]
        assert first.after_group == 0
        assert first.last_block_before == 1
        assert first.first_block_after == 2
        assert "render_widget_1(data: dict) -> str" in first.boundary_signatures

    def test_reason_names_the_groups(self):
        proposal = partition_blocks(_fan_out(3))
        assert "3 groups" in proposal.reason
        assert "group 1 = blocks 1" in proposal.reason

    def test_partially_shared_signatures_make_two_groups_not_five(self):
        blocks = [
            _block(1, "A", produces=["a() -> int"]),
            _block(2, "B", consumes=["a() -> int"], produces=["b() -> int"]),
            _block(3, "C", consumes=["b() -> int"]),
            _block(4, "D", produces=["d() -> int"]),
            _block(5, "E", consumes=["d() -> int"]),
        ]
        proposal = partition_blocks(blocks)
        assert [g.block_numbers for g in proposal.groups] == [[1, 2, 3], [4, 5]]
        assert proposal.splittable is True
        assert proposal.boundaries[0].crossing_signatures == []
        assert proposal.groups[0].produces == ["a() -> int", "b() -> int"]
        assert proposal.groups[1].consumes == ["d() -> int"]


# --- Case 2: the fully chained set -------------------------------------------

class TestFullyChained:
    def test_chained_blocks_are_one_group(self):
        proposal = partition_blocks(_chained(6))
        assert proposal.group_count == 1
        assert proposal.splittable is False
        assert proposal.groups[0].block_numbers == [1, 2, 3, 4, 5, 6]
        assert proposal.boundaries == []

    def test_reason_says_why_it_is_atomic(self):
        proposal = partition_blocks(_chained(6))
        assert "one atomic change" in proposal.reason
        assert "6 blocks" in proposal.reason

    def test_transitive_chain_holds_even_when_written_out_of_order(self):
        blocks = list(reversed(_chained(4)))
        proposal = partition_blocks(blocks)
        assert proposal.group_count == 1
        assert proposal.groups[0].block_numbers == [1, 2, 3, 4]

    def test_def_style_signatures_do_not_collapse_the_whole_plan(self):
        """Blocks written as `def name(...)` share a leading token, not work.
        Keying on it unioned every block into one component, which reported
        the plan atomic and silenced both split checks."""
        blocks = [
            _block(1, "Export", produces=["def export_job(x: int) -> None"]),
            _block(2, "Report", produces=["def send_report(y: str) -> None"]),
        ]
        assert partition_blocks(blocks).group_count == 2

    def test_two_blocks_producing_the_same_signature_are_one_group(self):
        blocks = [
            _block(1, "A", produces=["save(x: int) -> None"]),
            _block(2, "B", produces=["save(x: int) -> None"]),
        ]
        assert partition_blocks(blocks).group_count == 1

    def test_a_cut_boundary_covers_both_whole_sides(self):
        """A three-group partition cut after group 0 separates group 0 from
        groups 1 AND 2. Building the boundary from the two adjacent groups
        dropped everything the third one consumes, and the prompt quotes this
        list as "the signature boundary this cut crosses"."""
        blocks = [
            _block(1, "A", produces=["a() -> int"], consumes=["outside_a() -> int"]),
            _block(2, "B", produces=["b() -> int"], consumes=["outside_b() -> int"]),
            _block(3, "C", produces=["c() -> int"], consumes=["outside_c() -> int"]),
        ]
        proposal = partition_blocks(blocks)
        assert proposal.group_count == 3
        first_cut = proposal.boundaries[0]
        assert first_cut.after_group == 0
        assert "a() -> int" in first_cut.boundary_signatures
        # Group 2 is on the later side of this cut; what it consumes belongs to
        # the boundary just as much as group 1's does.
        assert "outside_b() -> int" in first_cut.boundary_signatures
        assert "outside_c() -> int" in first_cut.boundary_signatures

    def test_consumes_nothing_in_the_plan_produces_is_reported_unresolved(self):
        blocks = [
            _block(1, "A", consumes=["legacy_helper(x: int) -> None"]),
            _block(2, "B", produces=["b() -> int"]),
        ]
        proposal = partition_blocks(blocks)
        assert proposal.unresolved_consumes == ["legacy_helper(x: int) -> None"]
        assert proposal.group_count == 2
        assert "produced outside this plan" in proposal.reason


# --- Case 3: migration plus unrelated feature work ---------------------------

class TestAtomicity:
    def test_migration_plus_unrelated_feature_fires_at_two_blocks(self):
        blocks = [
            _block(1, "Add email column",
                   description="Django migration adding users.email to accounts/models.py"),
            _block(2, "Sparkline widget",
                   description="Render a sparkline in ui/widgets.py"),
        ]
        finding = check_atomicity(blocks)
        assert finding.should_split is True
        assert finding.kinds_present == ["migration"]
        assert finding.unrelated_block_numbers == [2]
        assert finding.unrelated_block_names == ["Sparkline widget"]
        assert finding.high_risk_blocks[0].number == 1
        assert "migration" in finding.high_risk_blocks[0].matched_keywords

    def test_reason_quotes_both_sides_of_the_split(self):
        blocks = [
            _block(1, "Schema", description="alter table orders — a data migration"),
            _block(2, "Chart", description="Draw the chart in ui/chart.py"),
        ]
        reason = check_atomicity(blocks).reason
        assert "block 1 (Schema" in reason
        assert "block 2 (Chart)" in reason
        assert "own branch" in reason

    def test_migration_chained_to_the_feature_is_related_work_and_stays_quiet(self):
        blocks = [
            _block(1, "Add email column",
                   description="Django migration adding users.email",
                   produces=["User.email"]),
            _block(2, "Show email",
                   description="Render the address in ui/profile.py",
                   consumes=["User.email"]),
        ]
        finding = check_atomicity(blocks)
        assert finding.should_split is False
        assert finding.kinds_present == ["migration"]
        assert finding.unrelated_block_numbers == []
        assert "no unrelated feature work" in finding.reason

    def test_no_high_risk_kind_does_not_fire_however_many_blocks(self):
        finding = check_atomicity(_fan_out(12))
        assert finding.should_split is False
        assert finding.kinds_present == []
        assert "No block matches a high-risk kind" in finding.reason

    @pytest.mark.parametrize("kind,description", [
        ("migration", "Add an alembic migration for the orders table"),
        ("payments", "Wire the Stripe checkout flow for invoices"),
        ("auth", "Add oauth login with a session token"),
        ("contract-change", "A breaking change to the public API response shape"),
        ("external-service", "Integrate with the Twilio webhook"),
    ])
    def test_each_keyword_kind_is_detected(self, kind, description):
        blocks = [
            _block(1, "Risky", description=description),
            _block(2, "Widget", description="Render a widget in ui/widgets.py"),
        ]
        finding = check_atomicity(blocks)
        assert kind in finding.kinds_present
        assert finding.should_split is True

    def test_the_check_is_keyword_only_and_takes_no_file_count(self):
        """Item 26's sixth kind ("more than N files") is not checked here. It
        shipped as a `file_count`/`file_trigger` pair that no caller could
        supply — the phase runs before anything is built — and that could not
        have changed the verdict if one had, because "high-risk work is mixed
        with unrelated work" is not a question a size number answers."""
        import inspect
        params = inspect.signature(check_atomicity).parameters
        assert "file_count" not in params
        assert "file_trigger" not in params
        assert "file_count" not in inspect.signature(assess_plan_split).parameters
        blocks = [
            _block(1, "Rename", description="Rename the helper across the tree"),
            _block(2, "Widget", description="Render a widget"),
        ]
        finding = check_atomicity(blocks)
        assert finding.kinds_present == []
        assert finding.should_split is False

    def test_keyword_sets_are_overridable(self):
        blocks = [
            _block(1, "Frobnicate", description="Frobnicate the widget"),
            _block(2, "Chart", description="Draw a chart"),
        ]
        assert check_atomicity(blocks).should_split is False
        finding = check_atomicity(blocks, kinds={"custom": ("frobnicate",)})
        assert finding.should_split is True
        assert finding.kinds_present == ["custom"]

    def test_the_five_keyword_kinds_are_the_documented_ones(self):
        assert sorted(HIGH_RISK_KEYWORDS) == [
            "auth", "contract-change", "external-service", "migration", "payments",
        ]

    def test_integration_tests_do_not_count_as_an_external_service(self):
        blocks = [_block(1, "Suite", description="Add integration tests for the parser")]
        assert check_atomicity(blocks).kinds_present == []

    def test_the_word_author_does_not_count_as_auth(self):
        blocks = [_block(1, "Byline", description="Show the author name on the post")]
        assert check_atomicity(blocks).kinds_present == []


# --- Check 3: package spread --------------------------------------------------

class TestPackageSpread:
    def test_counts_distinct_top_level_packages(self):
        blocks = [
            _block(1, "A", description="Edit accounts/models.py and accounts/views.py"),
            _block(2, "B", description="Edit billing/tasks.py"),
            _block(3, "C", description="Edit ui/widgets.py and accounts/forms.py"),
        ]
        spread = package_spread(blocks)
        assert spread.count == 3
        assert spread.packages == ["accounts", "billing", "ui"]
        assert spread.blocks_by_package["accounts"] == [1, 3]

    def test_deep_change_in_one_package_counts_one(self):
        blocks = [
            _block(1, "A", description="Edit engine/core/parse.py"),
            _block(2, "B", description="Edit engine/core/emit.py"),
        ]
        assert package_spread(blocks).count == 1

    def test_blocks_naming_no_path_contribute_nothing(self):
        assert package_spread([_block(1, "A", description="Tidy the prose")]).count == 0

    def test_urls_and_filesystem_roots_are_not_packages(self):
        text = "see https://example.com/docs/x.html and /usr/local/bin/tool"
        assert top_level_packages(text) == []

    def test_signatures_count_toward_the_spread(self):
        blocks = [_block(1, "A", produces=["read(path='conf/app.yml') -> dict"])]
        assert package_spread(blocks).packages == ["conf"]

    def test_prose_slashes_are_not_packages(self):
        """English writes "read/write access". Counting those as packages fired
        the spread trigger on plans naming no path at all."""
        text = "read/write access, a red/green cycle and a pass/fail gate"
        assert top_level_packages(text) == []

    def test_a_two_segment_path_still_counts_when_it_is_marked_as_one(self):
        """Backticks are how a block's prose says it means a literal path."""
        assert top_level_packages("touch `apps/orders` next") == ["apps"]

    def test_three_segments_are_enough_without_an_extension(self):
        assert top_level_packages("edit engine/core/parse") == ["engine"]

    def test_prose_slashes_do_not_trigger_the_spread(self):
        blocks = [
            _block(1, "A", description="Gate on pass/fail"),
            _block(2, "B", description="Document read/write access"),
            _block(3, "C", description="Run the red/green cycle"),
        ]
        assert package_spread(blocks).count == 0


# --- Signature matching -------------------------------------------------------

class TestSignatureMatching:
    def test_backticks_and_whitespace_are_normalized_away(self):
        assert normalize_signature("`  parse(x:  str) -> Doc `;") == "parse(x: str) -> Doc"

    def test_symbol_is_a_fallback_key(self):
        assert "parse" in signature_keys("parse(x: str) -> Doc")

    def test_a_declaration_keyword_is_never_the_fallback_key(self):
        """`def` leads every signature written that way, so keying on it made
        every such block look like it produced the same thing."""
        assert signature_keys("def export_job(x) -> None") == frozenset(
            {"def export_job(x) -> none", "export_job"})
        assert "def" not in signature_keys("def export_job(x) -> None")
        assert "class" not in signature_keys("class UserService")
        assert "userservice" in signature_keys("class UserService")

    def test_an_http_verb_leaves_no_fallback_key(self):
        """`POST /jobs` has no identifier after the verb — better no fallback
        key than one shared by every route in the plan."""
        assert signature_keys("POST /api/jobs") == frozenset({"post /api/jobs"})
        assert "parse(x: str) -> doc" in signature_keys("`parse(x: str) -> Doc`")

    def test_a_reworded_consume_still_links_the_blocks(self):
        blocks = [
            _block(1, "A", produces=["parse(raw: str) -> Doc"]),
            _block(2, "B", consumes=["parse(text) -> Doc"]),
        ]
        assert partition_blocks(blocks).group_count == 1


# --- Degenerate sets ----------------------------------------------------------

class TestDegenerateSets:
    def test_empty_block_list(self):
        proposal = partition_blocks([])
        assert proposal.groups == []
        assert proposal.boundaries == []
        assert proposal.splittable is False
        assert "No blocks parsed" in proposal.reason

        finding = check_atomicity([])
        assert finding.should_split is False
        assert "No blocks parsed" in finding.reason

        assert package_spread([]).count == 0

    def test_single_block_is_one_group_with_no_cut(self):
        proposal = partition_blocks([_block(1, "Only", produces=["only() -> None"])])
        assert proposal.group_count == 1
        assert proposal.splittable is False
        assert proposal.boundaries == []
        assert proposal.groups[0].block_numbers == [1]


# --- All three, with the caller's thresholds ---------------------------------

class TestAssessPlanSplit:
    def test_thresholds_come_from_the_caller(self):
        blocks = _fan_out(12)
        assessment = assess_plan_split(blocks, block_trigger=8, package_trigger=3)
        assert assessment.block_count == 12
        assert assessment.block_trigger == 8
        assert assessment.size_triggered is True
        assert assessment.should_split is True

        relaxed = assess_plan_split(blocks, block_trigger=20, package_trigger=30)
        assert relaxed.size_triggered is False
        assert relaxed.package_triggered is False
        assert relaxed.should_split is False
        # The partition still ran and still says the work comes apart.
        assert relaxed.split.splittable is True

    def test_package_trigger_fires_independently_of_block_count(self):
        blocks = [
            _block(1, "A", description="Edit accounts/models.py"),
            _block(2, "B", description="Edit billing/tasks.py"),
            _block(3, "C", description="Edit ui/widgets.py"),
            _block(4, "D", description="Edit search/index.py"),
        ]
        assessment = assess_plan_split(blocks, block_trigger=8, package_trigger=3)
        assert assessment.size_triggered is False
        assert assessment.package_triggered is True
        assert assessment.spread.count == 4
        assert assessment.should_split is True

    def test_chained_set_over_the_trigger_is_still_let_through(self):
        assessment = assess_plan_split(_chained(12), block_trigger=8, package_trigger=3)
        assert assessment.size_triggered is True
        assert assessment.split.splittable is False
        assert assessment.should_split is False
        assert "one atomic change" in assessment.split.reason

    def test_atomicity_overrides_the_size_trigger_at_two_blocks(self):
        blocks = [
            _block(1, "Migrate", description="A schema change migration in db/versions/"),
            _block(2, "Widget", description="Render a widget in ui/widgets.py"),
        ]
        assessment = assess_plan_split(blocks, block_trigger=8, package_trigger=3)
        assert assessment.size_triggered is False
        assert assessment.package_triggered is False
        assert assessment.should_split is True
        assert assessment.atomicity.should_split is True


# --- Purity -------------------------------------------------------------------

def test_functions_do_not_mutate_their_input():
    blocks = _chained(4) + [_block(9, "Migrate", description="run the migration")]
    before = [b.model_dump() for b in blocks]
    assess_plan_split(blocks, block_trigger=2, package_trigger=1)
    partition_blocks(blocks)
    check_atomicity(blocks)
    package_spread(blocks)
    assert [b.model_dump() for b in blocks] == before
