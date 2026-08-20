"""Is this ticket too big for one branch? — the pure half of W14.

Three checks over the parsed `tasks.md` block set. All of them are pure
functions over ``list[BlockInfo]``: they read data buildme already parsed, they
return data, and they change nothing. Nothing here creates a ticket, moves a
card, or rewrites `tasks.md` — the offer to split is a proposal a human
approves.

**Block count is the wrong primary metric.** Twelve independent blocks are
twelve small reviews and one safe branch; six blocks where the sixth consumes
signatures from the first five are one atomic thing that cannot be reviewed or
reverted in pieces. Count is the *trigger*; the verdict comes from the other
two checks.

1. :func:`check_atomicity` — a migration (or payments, auth, a contract
   change, an external-service integration, or more than N files) **plus
   unrelated feature work** means split, at any size, including a two-block
   change. Not because the change is large, but because a migration should
   land on its own branch so it can be reverted on its own. Highest-value of
   the three, and the only one that is heuristic (see the docstring).

2. :func:`partition_blocks` — the separability partition, computed from
   ``BlockInfo.produces`` / ``BlockInfo.consumes``: the exact cross-block
   signatures parsed from each block's ``Interfaces:`` section
   (``models.py``). That is a dependency graph, already built, needing no new
   extraction. Two or more groups means each ships independently and the cut
   points are known. One group means the work is genuinely atomic — the caller
   says so and lets it through, with the reason.

3. :func:`package_spread` — the number of distinct top-level packages the
   block set touches. A change spanning several top-level packages (Django
   apps, say) is a different risk class from a deep change inside one, at
   identical block count.

**Thresholds are inputs, not constants.** The caller passes
``block_trigger`` and ``package_trigger`` (``config.plan_split_block_trigger``,
default 8, and ``config.plan_split_package_trigger``, default 3). The plan is
explicit that 8 is a placeholder: nobody has measured the block-count
distribution across real runs, so any number chosen today is a guess wearing a
decimal point. Replace it with a measured one — the block count per finished
run in the run archive, cut at the point where per-block review actually
started failing — rather than treating the default as a finding.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .models import BlockInfo

# --- Reviewable, overridable heuristics ---------------------------------------
#
# These drive :func:`check_atomicity`, which reads block prose. They are
# module-level so they can be read, argued with, and overridden by a caller
# (pass ``kinds=`` a modified copy) instead of being buried in a function body.
# Each entry is a lowercase substring matched against the block's name +
# description + its interface signatures. Substrings, not word boundaries, so
# "migrations" and "webhook" hit; that is deliberate and it is also why this
# check over-fires more readily than it under-fires.

MIGRATION_KEYWORDS: tuple[str, ...] = (
    "migration", "migrate", "alembic", "schema change", "backfill",
    "add column", "drop column", "alter table", "create table", "drop table",
    "reindex", "data migration",
)

PAYMENTS_KEYWORDS: tuple[str, ...] = (
    "payment", "billing", "invoice", "stripe", "paypal", "checkout",
    "subscription", "refund", "charge card", "credit card", "pricing plan",
    "payout",
)

# Deliberately not the bare substring "auth": it matches "author" and
# "authored", which appear in ordinary block prose.
AUTH_KEYWORDS: tuple[str, ...] = (
    "authenticat", "authoriz", "auth token", "auth flow", "oauth",
    "login", "logout", "signup", "sign-up", "password", "jwt",
    "session token", "permission", "access control", "rbac", "credential",
    "api key", "sso",
)

CONTRACT_KEYWORDS: tuple[str, ...] = (
    "api contract", "public api", "breaking change", "openapi", "swagger",
    "protobuf", "graphql schema", "rest endpoint", "endpoint signature",
    "response shape", "serializer", "wire format", "versioned api",
    "backwards incompatible", "backward incompatible",
)

# Deliberately not the bare substring "integrat": buildme blocks talk about
# "integration tests" constantly, and that word says nothing about a service.
EXTERNAL_SERVICE_KEYWORDS: tuple[str, ...] = (
    "third-party", "third party", "external service", "external api",
    "webhook", "sdk client", "http client", "api client",
    "integrate with", "integration with", "vendor api",
    "sendgrid", "twilio", "sentry", "datadog", "pubsub", "kafka",
    "rabbitmq", "redis", "smtp", "amazon s3",
)

#: kind name → keyword tuple. Five of the six high-risk kinds from programme
#: index item 26.
#:
#: The sixth ("more than N files") is deliberately absent. It shipped here as a
#: ``file_count``/``file_trigger`` pair on :func:`check_atomicity` and could
#: never fire: this phase runs before anything is built, so no caller has a
#: file count to pass, and the check's verdict is "high-risk work is mixed with
#: unrelated work" — a size number cannot answer that, and there is no cut to
#: propose from one. Size is what ``block_count`` and :func:`package_spread`
#: are for; wiring it back in means giving it a check of its own, not an
#: argument on this one.
HIGH_RISK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "migration": MIGRATION_KEYWORDS,
    "payments": PAYMENTS_KEYWORDS,
    "auth": AUTH_KEYWORDS,
    "contract-change": CONTRACT_KEYWORDS,
    "external-service": EXTERNAL_SERVICE_KEYWORDS,
}

#: First path segments that name no package — a filesystem root, or a scheme
#: word used without its ``://``. Filtered out of :func:`top_level_packages`.
#:
#: This set is smaller than it looks like it should be, and deliberately: the
#: head group of :data:`_PATH` cannot contain ``.``, ``/`` or ``~``, and its
#: lookbehind already refuses a head preceded by one — so ``https://host/p``,
#: ``./rel/path`` and ``/usr/local/bin`` produce no match to filter in the
#: first place. Only a head written bare in prose (``usr/local/bin``) reaches
#: here. Entries that could never match were removed rather than left in as
#: apparent protection.
NON_PACKAGE_SEGMENTS: frozenset[str] = frozenset({
    "http", "https", "file", "ftp", "ssh", "git", "mailto",
    "usr", "var", "etc", "tmp", "opt", "home", "dev", "proc",
})


# --- Result types -------------------------------------------------------------

class BlockGroup(BaseModel):
    """One ordered group of blocks that ships as its own branch."""

    #: 0-based position of the group in the proposed ship order.
    index: int
    #: `BlockInfo.number` of every block in the group, in tasks.md order.
    block_numbers: list[int] = Field(default_factory=list)
    #: Block names, parallel to ``block_numbers`` — a finding names blocks the
    #: way a human reviewer would, not by index.
    block_names: list[str] = Field(default_factory=list)
    #: Every signature the group's blocks produce / consume, de-duplicated,
    #: first-seen order. This is the group's public surface: what a reviewer
    #: reads to decide whether the ticket stands on its own.
    produces: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)


class CutBoundary(BaseModel):
    """The seam between two adjacent groups — one proposed cut."""

    #: Index of the group immediately before the cut; the cut sits between
    #: group ``after_group`` and group ``after_group + 1``.
    after_group: int
    #: Last block before the cut / first block after it, for a finding that
    #: has to name a location in tasks.md.
    last_block_before: int
    first_block_after: int
    #: Signatures produced on the earlier side and consumed on the later side.
    #: **Empty for every cut this module proposes** — the partition only cuts
    #: where nothing crosses, so an empty list here is the evidence the cut is
    #: clean, not a missing computation. It is computed (rather than asserted)
    #: so a looser partitioner can populate it without changing the shape.
    crossing_signatures: list[str] = Field(default_factory=list)
    #: The contract surface the cut separates: everything the earlier side
    #: produces, and everything the later side consumes — BOTH SIDES WHOLE, not
    #: just the two groups adjacent to the cut. A three-group partition cut
    #: after group 0 separates group 0 from groups 1 AND 2, and the prompt tells
    #: the reviewer an `oversized-plan` finding must "name the signature
    #: boundary this cut crosses" — quoting a partial one reads as the whole.
    #: This is what a finding quotes when it names that boundary.
    boundary_signatures: list[str] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when no signature crosses the cut in either direction."""
        return not self.crossing_signatures


class SplitProposal(BaseModel):
    """The separability verdict — check 2's whole output."""

    groups: list[BlockGroup] = Field(default_factory=list)
    boundaries: list[CutBoundary] = Field(default_factory=list)
    #: Consumed signatures no block in the set produces. They point outside the
    #: plan (existing code), so they cannot join two blocks — carried because a
    #: dense dangling set is the sign the interface graph is too sparse to cut
    #: on, and the caller should fall back to another source.
    unresolved_consumes: list[str] = Field(default_factory=list)
    #: Human-readable verdict, written for a plan-PR comment.
    reason: str = ""

    @property
    def splittable(self) -> bool:
        """Two or more groups means each ships independently."""
        return len(self.groups) > 1

    @property
    def group_count(self) -> int:
        return len(self.groups)


class HighRiskBlock(BaseModel):
    """One block that matched one or more high-risk kinds."""

    number: int
    name: str
    kinds: list[str] = Field(default_factory=list)
    #: The literal keyword that matched, per kind — so a finding can quote the
    #: evidence rather than assert the classification.
    matched_keywords: dict[str, list[str]] = Field(default_factory=dict)


class AtomicityFinding(BaseModel):
    """Check 1's output: a high-risk kind plus unrelated feature work."""

    should_split: bool = False
    #: Kinds present anywhere in the block set, sorted.
    kinds_present: list[str] = Field(default_factory=list)
    high_risk_blocks: list[HighRiskBlock] = Field(default_factory=list)
    #: Blocks that matched no high-risk kind and share no signature path with
    #: any block that did — the "unrelated feature work" half of the rule.
    unrelated_block_numbers: list[int] = Field(default_factory=list)
    unrelated_block_names: list[str] = Field(default_factory=list)
    reason: str = ""


class PackageSpread(BaseModel):
    """Check 3's output: how wide the block set reaches."""

    #: Distinct top-level packages, sorted.
    packages: list[str] = Field(default_factory=list)
    #: package → block numbers that mention it, for a finding that has to say
    #: which block pulled the plan into which package.
    blocks_by_package: dict[str, list[int]] = Field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.packages)


class PlanSplitAssessment(BaseModel):
    """All three checks plus the trigger arithmetic, in one object.

    Convenience for the caller that renders the `oversized-plan` finding; every
    part is independently reachable through the three functions below.
    """

    block_count: int = 0
    block_trigger: int = 0
    package_trigger: int = 0
    size_triggered: bool = False
    package_triggered: bool = False
    atomicity: AtomicityFinding = Field(default_factory=AtomicityFinding)
    split: SplitProposal = Field(default_factory=SplitProposal)
    spread: PackageSpread = Field(default_factory=PackageSpread)

    @property
    def should_split(self) -> bool:
        """Split when atomicity says so at any size, or when a trigger fired
        *and* the interface graph shows the work actually comes apart."""
        if self.atomicity.should_split:
            return True
        return (self.size_triggered or self.package_triggered) and self.split.splittable


# --- Signature matching -------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*")

# Leading tokens that declare a signature rather than name it. `def`,
# `class`, `POST` and friends are the same word in every block that uses that
# style, so taking one as the fallback key makes every such block look like it
# produces the same thing — which unions the whole plan into one component and
# silences both split checks. Skipped when picking the fallback symbol.
_DECLARATION_LEADS = frozenset({
    "def", "async", "class", "func", "function", "fn", "proc", "sub",
    "struct", "enum", "protocol", "interface", "trait", "impl", "type",
    "const", "let", "var", "val", "public", "private", "internal",
    "protected", "static", "final", "export", "default", "extension",
    "abstract", "override", "new", "get", "set",
    "post", "put", "patch", "delete", "head", "options",
})


def normalize_signature(signature: str) -> str:
    """Canonical form of one `Interfaces:` signature.

    Backticks, surrounding punctuation and whitespace runs carry no meaning in
    a signature, and blocks are written by an LLM that varies all three.
    """
    text = signature
    previous = None
    while previous != text:
        previous = text
        text = text.strip().strip("`").rstrip(";.,").strip()
    return _WHITESPACE.sub(" ", text)


def signature_keys(signature: str) -> frozenset[str]:
    """Keys a produced and a consumed signature are matched on.

    Two keys, because agents do not always copy a signature verbatim: the whole
    normalized string, and the leading dotted symbol (``parse_tasks`` out of
    ``parse_tasks(path: Path) -> list[BlockInfo]``). Matching on the symbol
    alone would join two blocks that happen to both mention ``save``, so it is
    a fallback, not the primary key — but it is the reason this matcher is
    heuristic rather than exact.
    """
    normalized = normalize_signature(signature)
    if not normalized:
        return frozenset()
    keys = {normalized.lower()}
    symbol = _leading_symbol(normalized)
    if symbol:
        keys.add(symbol)
    return frozenset(keys)


def _leading_symbol(normalized: str) -> str | None:
    """The first token of ``normalized`` that actually names something.

    Declaration keywords are skipped, so ``def export_job(...)`` keys on
    ``export_job`` and ``POST /jobs`` — whose next token is not an identifier —
    gets no fallback key at all rather than keying on ``post``.
    """
    remainder = normalized
    while True:
        match = _SYMBOL.match(remainder)
        if not match:
            return None
        token = match.group(0)
        if token.lower() not in _DECLARATION_LEADS:
            return token.lower()
        remainder = remainder[match.end():].lstrip()
        if not remainder:
            return None


def _keys_for(signatures: list[str]) -> frozenset[str]:
    keys: set[str] = set()
    for signature in signatures:
        keys |= signature_keys(signature)
    return frozenset(keys)


# --- Check 2: separability ----------------------------------------------------

def _dedup(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = normalize_signature(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _connected_components(blocks: list[BlockInfo]) -> list[list[BlockInfo]]:
    """Group blocks that share any signature, transitively.

    Union-find over the produce/consume graph, treated as undirected. A chain
    (block 2 consumes block 1's output, block 3 consumes block 2's) collapses
    to a single group, which is the point: a chain cannot be reviewed or
    reverted in pieces, so it is one ticket however long it is.
    """
    parent = list(range(len(blocks)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    produced_by: dict[str, list[int]] = {}
    for i, block in enumerate(blocks):
        for key in _keys_for(block.produces):
            produced_by.setdefault(key, []).append(i)

    for i, block in enumerate(blocks):
        for key in _keys_for(block.consumes):
            for j in produced_by.get(key, ()):
                if i != j:
                    union(i, j)
    # Two blocks producing the same signature are the same work split in two.
    for producers in produced_by.values():
        for other in producers[1:]:
            union(producers[0], other)

    groups: dict[int, list[BlockInfo]] = {}
    for i, block in enumerate(blocks):
        groups.setdefault(find(i), []).append(block)
    # Order groups by their earliest block, and blocks within a group by number.
    ordered = sorted(groups.values(), key=lambda g: min(b.number for b in g))
    return [sorted(g, key=lambda b: b.number) for g in ordered]


def partition_blocks(blocks: list[BlockInfo]) -> SplitProposal:
    """Check 2 — partition blocks into groups that ship independently.

    The partition is the set of connected components of the cross-block
    signature graph (``produces`` / ``consumes``), ordered by earliest block
    number. No group consumes a signature produced by any other group, in
    either direction, so every cut is clean and every group is a branch that
    can be reviewed and reverted on its own.

    Two or more groups → the work is splittable and the cut points are known.
    One group → the work is genuinely atomic; the caller says so, with the
    reason, and lets it through.

    Pure: reads ``blocks``, returns a :class:`SplitProposal`, mutates nothing.
    """
    if not blocks:
        return SplitProposal(reason="No blocks parsed — nothing to partition.")

    ordered = sorted(blocks, key=lambda b: b.number)
    components = _connected_components(ordered)

    groups = [
        BlockGroup(
            index=i,
            block_numbers=[b.number for b in component],
            block_names=[b.name for b in component],
            produces=_dedup([s for b in component for s in b.produces]),
            consumes=_dedup([s for b in component for s in b.consumes]),
        )
        for i, component in enumerate(components)
    ]

    boundaries: list[CutBoundary] = []
    for i in range(len(groups) - 1):
        earlier = groups[: i + 1]
        later = groups[i + 1:]
        produced_before = _keys_for([s for g in earlier for s in g.produces])
        produced_after = _keys_for([s for g in later for s in g.produces])
        crossing = [
            s for g in later for s in g.consumes
            if signature_keys(s) & produced_before
        ] + [
            s for g in earlier for s in g.consumes
            if signature_keys(s) & produced_after
        ]
        boundaries.append(CutBoundary(
            after_group=i,
            last_block_before=groups[i].block_numbers[-1],
            first_block_after=groups[i + 1].block_numbers[0],
            crossing_signatures=_dedup(crossing),
            boundary_signatures=_dedup(
                [sig for g in earlier for sig in g.produces]
                + [sig for g in later for sig in g.consumes]
            ),
        ))

    all_produced = _keys_for([s for b in ordered for s in b.produces])
    unresolved = _dedup([
        s for b in ordered for s in b.consumes
        if not (signature_keys(s) & all_produced)
    ])

    if len(groups) > 1:
        reason = (
            f"{len(ordered)} blocks fall into {len(groups)} groups that share no "
            f"interface signature: "
            + "; ".join(
                f"group {g.index + 1} = blocks "
                + ", ".join(str(n) for n in g.block_numbers)
                for g in groups
            )
            + ". Each group can ship, be reviewed, and be reverted on its own."
        )
    else:
        reason = (
            f"All {len(ordered)} blocks are joined by shared interface "
            "signatures, directly or transitively — the set is one atomic "
            "change and cannot be reviewed or reverted in pieces."
        )
    if unresolved:
        reason += (
            f" {len(unresolved)} consumed signature(s) are produced outside this "
            "plan, so they join no blocks; if the interface graph looks too "
            "sparse to cut on, use the change DAG instead."
        )

    return SplitProposal(
        groups=groups,
        boundaries=boundaries,
        unresolved_consumes=unresolved,
        reason=reason,
    )


# --- Check 1: atomicity -------------------------------------------------------

def _block_text(block: BlockInfo) -> str:
    parts = [block.name, block.description]
    parts.extend(block.produces)
    parts.extend(block.consumes)
    return "\n".join(parts).lower()


def check_atomicity(
    blocks: list[BlockInfo],
    *,
    kinds: dict[str, tuple[str, ...]] | None = None,
) -> AtomicityFinding:
    """Check 1 — a high-risk kind plus unrelated feature work means split.

    Fires at **any** size, including a two-block change: not because the change
    is large, but because a migration should land on its own branch so it can
    be reverted on its own. Highest-value of the three checks.

    Five machine-checkable high-risk kinds (programme index item 26), all
    detected by keyword: a migration, payments, auth, a contract change, an
    external-service integration. Item 26's sixth ("more than N files") is not
    checked here — see :data:`HIGH_RISK_KEYWORDS` for why a size number cannot
    answer this check's question.

    **The detection is heuristic and it is a keyword match over block prose.**
    It reads each block's name, description and interface signatures, and looks
    for substrings from :data:`HIGH_RISK_KEYWORDS`. That means it will call a
    block that merely *mentions* a migration a migration, and it will miss one
    that describes a schema change without using any of the listed words. It
    tolerates false positives more readily than false negatives, on the view
    that a wrong "consider splitting this" costs a human one sentence of reply
    and a missed migration costs a revert that takes the feature with it. The
    keyword sets are module-level constants precisely so they can be read,
    argued with and overridden (pass ``kinds=``) rather than trusted.

    "Unrelated" is the one part that is not guesswork: a block counts as
    unrelated feature work when it matched no high-risk kind **and** shares no
    interface signature path with any block that did — the same connected-
    component graph :func:`partition_blocks` uses. Feature work chained to the
    migration is related work, and this check stays quiet about it.

    Pure: returns an :class:`AtomicityFinding`, changes nothing.
    """
    keyword_sets = HIGH_RISK_KEYWORDS if kinds is None else kinds
    if not blocks:
        return AtomicityFinding(reason="No blocks parsed — nothing to check.")

    ordered = sorted(blocks, key=lambda b: b.number)

    high_risk: list[HighRiskBlock] = []
    for block in ordered:
        text = _block_text(block)
        matched: dict[str, list[str]] = {}
        for kind, keywords in keyword_sets.items():
            hits = [kw for kw in keywords if kw in text]
            if hits:
                matched[kind] = hits
        if matched:
            high_risk.append(HighRiskBlock(
                number=block.number,
                name=block.name,
                kinds=sorted(matched),
                matched_keywords=matched,
            ))

    kinds_present = sorted({k for hr in high_risk for k in hr.kinds})

    if not high_risk:
        return AtomicityFinding(
            should_split=False,
            kinds_present=kinds_present,
            reason=(
                "No block matches a high-risk kind (migration, payments, auth, "
                "contract change, external service) — nothing to isolate."
            ),
        )

    # Which blocks share a signature path with a high-risk one?
    high_risk_numbers = {hr.number for hr in high_risk}
    related: set[int] = set()
    for component in _connected_components(ordered):
        numbers = {b.number for b in component}
        if numbers & high_risk_numbers:
            related |= numbers

    unrelated = [
        b for b in ordered
        if b.number not in high_risk_numbers and b.number not in related
    ]

    kind_list = ", ".join(kinds_present)
    if unrelated:
        risky = ", ".join(
            f"block {hr.number} ({hr.name}: {'/'.join(hr.kinds)})"
            for hr in high_risk
        )
        others = ", ".join(f"block {b.number} ({b.name})" for b in unrelated)
        reason = (
            f"High-risk work ({kind_list}) — {risky} — ships alongside "
            f"unrelated feature work: {others}. They share no interface "
            "signature, so the high-risk part should land on its own branch "
            "and be revertible on its own."
        )
    else:
        reason = (
            f"High-risk work present ({kind_list}), but every other block is "
            "joined to it through the interface graph — there is no unrelated "
            "feature work to separate out."
        )

    return AtomicityFinding(
        should_split=bool(unrelated),
        kinds_present=kinds_present,
        high_risk_blocks=high_risk,
        unrelated_block_numbers=[b.number for b in unrelated],
        unrelated_block_names=[b.name for b in unrelated],
        reason=reason,
    )


# --- Check 3: package spread --------------------------------------------------

_PATH = re.compile(r"(?<![\w./-])([A-Za-z_][\w-]*)/((?:[\w.-]+/)*[\w.-]+)")


def top_level_packages(text: str) -> list[str]:
    """Top-level path segments named in ``text``, first-seen order.

    Heuristic, and only as good as the paths a block's prose names: it matches
    slash-separated path-looking tokens (``apps/orders/views.py``,
    ``build_pipeline/models.py``) and takes the first segment. Filesystem roots
    and bare scheme words are dropped (:data:`NON_PACKAGE_SEGMENTS`). A block
    that names no path contributes nothing rather than guessing.

    A `word/word` token is not enough on its own — English writes "read/write
    access", "red/green cycle", "pass/fail gate", and counting those as
    packages fired the spread trigger on plans that named no path at all. A
    match must additionally look like a path: carry a dot in its tail (an
    extension), name three or more segments, or be written inside backticks.
    """
    packages: list[str] = []
    for match in _PATH.finditer(text):
        head = match.group(1)
        if head.lower() in NON_PACKAGE_SEGMENTS:
            continue
        if not _looks_like_path(match, text):
            continue
        if head not in packages:
            packages.append(head)
    return packages


def _looks_like_path(match: re.Match[str], text: str) -> bool:
    """Whether a `word/word…` match is a path rather than an English slash.

    Three independent signals, any one of which is enough: an extension in the
    tail, a third segment, or backticks around the token — the way a block's
    prose marks a path it means literally.
    """
    tail = match.group(2)
    if "." in tail:
        return True
    if "/" in tail:
        return True
    start, end = match.span()
    return (start > 0 and text[start - 1] == "`") or (end < len(text) and text[end] == "`")


def package_spread(blocks: list[BlockInfo]) -> PackageSpread:
    """Check 3 — how many distinct top-level packages the block set touches.

    A change spanning several top-level packages (Django apps, Go modules,
    workspace crates) is a different risk class from a deep change inside one,
    at identical block count. ``.count`` is the number the caller compares to
    ``plan_split_package_trigger``.

    Pure: reads ``blocks``, returns a :class:`PackageSpread`.
    """
    blocks_by_package: dict[str, list[int]] = {}
    for block in sorted(blocks, key=lambda b: b.number):
        text = "\n".join([block.name, block.description, *block.produces, *block.consumes])
        for package in top_level_packages(text):
            numbers = blocks_by_package.setdefault(package, [])
            if block.number not in numbers:
                numbers.append(block.number)
    return PackageSpread(
        packages=sorted(blocks_by_package),
        blocks_by_package={k: blocks_by_package[k] for k in sorted(blocks_by_package)},
    )


# --- All three, with the caller's thresholds ----------------------------------

def assess_plan_split(
    blocks: list[BlockInfo],
    *,
    block_trigger: int,
    package_trigger: int,
) -> PlanSplitAssessment:
    """Run all three checks with the caller's thresholds.

    ``block_trigger`` and ``package_trigger`` come from config
    (``plan_split_block_trigger``, default 8; ``plan_split_package_trigger``,
    default 3) and are passed in, never read here — 8 in particular is an
    unmeasured placeholder, not a finding, and this module must not hard-code
    it. The size and package triggers only decide whether the separability
    partition gets a say; :func:`check_atomicity` is consulted at every size.

    Pure: no I/O, no config read, no ticket, no card, no edit to `tasks.md`.
    """
    ordered = sorted(blocks, key=lambda b: b.number)
    spread = package_spread(ordered)
    return PlanSplitAssessment(
        block_count=len(ordered),
        block_trigger=block_trigger,
        package_trigger=package_trigger,
        size_triggered=len(ordered) > block_trigger,
        package_triggered=spread.count > package_trigger,
        atomicity=check_atomicity(ordered),
        split=partition_blocks(ordered),
        spread=spread,
    )
