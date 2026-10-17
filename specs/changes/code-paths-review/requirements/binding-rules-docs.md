## ADDED Requirements

### Requirement: Linked Markdown docs are added to the conventions finder
The conventions finder SHALL receive the content of every repository-local Markdown file linked from a `CLAUDE.md` or `coding-standards.md` that `conventions_blocks` already reads. A linked doc is added in its own block. The block is labelled with the doc's repository-relative path. Docs are read from the review snapshot, not from the working tree.

#### Scenario: Doc linked from CLAUDE.md is included
- **WHEN** a `CLAUDE.md` read by the conventions finder contains `[rules](docs/untrusted-content.md)`
- **AND** `docs/untrusted-content.md` exists in the snapshot
- **THEN** the conventions finder prompt contains a block labelled `docs/untrusted-content.md`
- **AND** that block holds the file's full content

#### Scenario: Doc linked from coding-standards.md is included
- **WHEN** a `coding-standards.md` read by the conventions finder links `docs/degradation-contract.md`
- **THEN** the conventions finder prompt contains a block labelled `docs/degradation-contract.md`

#### Scenario: Files the finder does not already read are not link sources
- **WHEN** a Markdown file that is neither a `CLAUDE.md` nor a `coding-standards.md` read by the conventions finder links `docs/other.md`
- **AND** no file the finder reads links `docs/other.md`
- **THEN** `docs/other.md` does not appear in the conventions finder prompt

#### Scenario: Snapshot content is used, not the working tree
- **WHEN** a linked doc differs between the review snapshot at head and the working tree
- **THEN** the block holds the snapshot content at head

### Requirement: Links are followed one level only
Only links that appear directly in a `CLAUDE.md` or `coding-standards.md` the finder reads SHALL be followed. Links inside a linked doc SHALL NOT be followed.

#### Scenario: Transitive link is not followed
- **WHEN** `CLAUDE.md` links `docs/a.md`
- **AND** `docs/a.md` links `docs/b.md`
- **AND** no `CLAUDE.md` or `coding-standards.md` read by the finder links `docs/b.md`
- **THEN** `docs/a.md` is included
- **AND** `docs/b.md` is not included

### Requirement: Link resolution
Link targets SHALL be resolved as follows:

- Inline Markdown links (`[text](target)`) and reference-style link definitions (`[id]: target`) are both recognised.
- A relative target resolves against the directory of the linking file.
- A target starting with `/` resolves against the repository root.
- A `#fragment` or `?query` suffix is removed before resolution.
- Percent-encoded characters in the target are decoded.

#### Scenario: Relative link from a nested CLAUDE.md
- **WHEN** `plugins/x/CLAUDE.md` links `../../docs/rules.md`
- **THEN** the resolved target is `docs/rules.md`

#### Scenario: Root-relative link
- **WHEN** `plugins/x/CLAUDE.md` links `/docs/rules.md`
- **THEN** the resolved target is `docs/rules.md`

#### Scenario: Fragment is stripped
- **WHEN** `CLAUDE.md` links `docs/rules.md#fencing`
- **THEN** the whole of `docs/rules.md` is included once

#### Scenario: Reference-style link
- **WHEN** `CLAUDE.md` contains the definition `[contract]: docs/degradation-contract.md`
- **THEN** `docs/degradation-contract.md` is included

### Requirement: Only repository-local Markdown targets are followed
A link target SHALL be followed only when all of the following hold:

- It is not a URL. A URL is a target with a scheme such as `http:`, `https:` or `mailto:`.
- It ends in `.md` or `.markdown`, compared case-insensitively.
- It resolves to a path inside the repository root.
- It names a regular file in the snapshot.
- It is not a symlink whose target lies outside the repository.

Targets that fail any of these checks SHALL NOT be read.

#### Scenario: External URL is ignored
- **WHEN** `CLAUDE.md` links `https://example.com/rules.md`
- **THEN** no network request is made
- **AND** nothing is added for that link

#### Scenario: Non-Markdown target is ignored
- **WHEN** `CLAUDE.md` links `scripts/check.py`
- **THEN** `scripts/check.py` is not added to the conventions finder prompt

#### Scenario: Target escaping the repository is refused
- **WHEN** `CLAUDE.md` links `../../outside/secrets.md`, which resolves outside the repository root
- **THEN** the file is not read
- **AND** it is not added to the prompt

#### Scenario: Symlink pointing outside the repository is refused
- **WHEN** `CLAUDE.md` links `docs/link.md`
- **AND** `docs/link.md` is a symlink to a file outside the repository
- **THEN** the file is not read
- **AND** it is not added to the prompt

### Requirement: Duplicates and already-read files are not repeated
Each resolved doc SHALL appear in the conventions finder prompt at most once. This holds however many links point to it. A link to a `CLAUDE.md` or `coding-standards.md` that the finder already reads SHALL NOT add a second copy.

#### Scenario: Same doc linked twice
- **WHEN** both `CLAUDE.md` and `coding-standards.md` link `docs/rules.md`
- **THEN** `docs/rules.md` appears in exactly one block

#### Scenario: Link back to an already-read file
- **WHEN** `coding-standards.md` links `CLAUDE.md`
- **AND** the finder already reads that `CLAUDE.md`
- **THEN** that `CLAUDE.md` content appears exactly once in the prompt

### Requirement: Links are collected at head and at base
For a change review, links SHALL be collected from the linking files at head. Links that exist only in the base version of a linking file SHALL also be collected. A linked doc's content SHALL be read at head when the doc exists at head, and otherwise at base. A tree or directory review has no base, so it SHALL collect links at head only.

#### Scenario: Link removed by the change is still followed
- **WHEN** the base version of `CLAUDE.md` links `docs/untrusted-content.md`
- **AND** the change removes that link at head
- **THEN** `docs/untrusted-content.md` is included in the conventions finder prompt

#### Scenario: Doc deleted by the change is read at base
- **WHEN** a link to `docs/old-rules.md` exists at base
- **AND** the change deletes `docs/old-rules.md`
- **THEN** the block for `docs/old-rules.md` holds its base content

#### Scenario: Link added by the change is followed
- **WHEN** the change adds a link to `docs/new-rules.md` in `CLAUDE.md` at head
- **THEN** `docs/new-rules.md` is included

#### Scenario: Tree review uses head only
- **WHEN** a `--tree` review runs
- **THEN** links are collected from head files only
- **AND** no base lookup is attempted

### Requirement: Binding links are ordered first
Linked docs SHALL be ordered in two groups.

- **Binding group.** A doc is in this group when any line linking it contains the word `bind`, `binding`, `must` or `required`. Matching is case-insensitive and on whole words, so `mustard` does not match.
- **Other group.** Every remaining doc.

The binding group SHALL come first. Within each group, docs SHALL appear in first-seen order. That order follows the order in which `conventions_blocks` reads the linking files, then line order within each file.

#### Scenario: Binding doc is placed before an earlier non-binding doc
- **WHEN** line 3 of `CLAUDE.md` links `docs/style.md` with no keyword
- **AND** line 10 of `CLAUDE.md` reads "These rules are binding: [contract](docs/degradation-contract.md)"
- **THEN** `docs/degradation-contract.md` appears before `docs/style.md` in the prompt

#### Scenario: Keyword match is case-insensitive
- **WHEN** a linking line reads "You MUST follow [x](docs/x.md)"
- **THEN** `docs/x.md` is in the binding group

#### Scenario: Substring does not count
- **WHEN** a linking line reads "See [mustard notes](docs/mustard.md)" and has no other keyword
- **THEN** `docs/mustard.md` is in the other group

#### Scenario: First-seen order within a group
- **WHEN** two binding links appear in `CLAUDE.md`, first to `docs/a.md` and then to `docs/b.md`
- **THEN** `docs/a.md` appears before `docs/b.md`

### Requirement: Total conventions content stays within CONVENTIONS_MAX_CHARS
The combined conventions content SHALL NOT exceed `CONVENTIONS_MAX_CHARS`. This content is the existing `CLAUDE.md` and `coding-standards.md` blocks plus all linked doc blocks.

- The existing blocks keep their current priority.
- Linked docs are added in the order defined above.
- A linked doc is included whole or not at all. It is never truncated.
- A doc that does not fit in the remaining budget is skipped, and later, smaller docs are still considered.

#### Scenario: Doc that does not fit is skipped
- **WHEN** the remaining budget is 4,000 characters
- **AND** the next linked doc is 6,000 characters
- **THEN** that doc is not included
- **AND** no part of it appears in the prompt

#### Scenario: Smaller later doc still fits
- **WHEN** a 6,000-character doc is skipped for budget
- **AND** the next doc is 2,000 characters with 4,000 characters remaining
- **THEN** the 2,000-character doc is included

#### Scenario: Budget is never exceeded
- **WHEN** linked docs total more than `CONVENTIONS_MAX_CHARS`
- **THEN** the length of the conventions content in the prompt is at most `CONVENTIONS_MAX_CHARS`

### Requirement: Skipped docs are recorded
Every resolved link target that is not included SHALL be recorded in the run's coverage. Each record holds the repository-relative path and one of these reasons:

- `budget`: did not fit.
- `missing`: absent at both head and base.
- `unreadable`: read error or non-UTF-8 content.
- `outside-repo`: escapes the repository root or is a symlink to outside it.

A skipped doc SHALL NOT stop the review.

#### Scenario: Budget skip is recorded
- **WHEN** a linked doc is skipped for budget
- **THEN** coverage contains an entry with that doc's path and reason `budget`

#### Scenario: Missing doc is recorded and the review continues
- **WHEN** `CLAUDE.md` links `docs/gone.md`
- **AND** the file exists at neither head nor base
- **THEN** coverage contains an entry for `docs/gone.md` with reason `missing`
- **AND** the conventions finder still runs

#### Scenario: Escaping link is recorded
- **WHEN** a link resolves outside the repository root
- **THEN** coverage contains an entry with reason `outside-repo`

#### Scenario: Ignored URLs and non-Markdown targets are not recorded as skips
- **WHEN** `CLAUDE.md` links an `https:` URL or a `.py` file
- **THEN** no coverage skip entry is created for that link

### Requirement: Linked doc content is fenced as untrusted
Each linked doc block SHALL be fenced and defanged through `multiplai_core.untrusted` before it is placed in the prompt. The repository under review is controlled by the change author, which is why its linked docs are treated as untrusted. Text inside a linked doc that mimics a fence boundary or a prompt instruction SHALL NOT be able to end the fence early.

#### Scenario: Doc containing a fake fence terminator
- **WHEN** a linked doc contains text that matches the closing fence marker
- **THEN** the rendered prompt still contains exactly one opening and one closing fence for that doc
- **AND** the doc text is defanged inside it

### Requirement: Violations of linked docs are findings
The conventions finder prompt SHALL state that the linked docs are rules of this repository. It SHALL instruct the finder to report a finding for any changed line that breaks a rule in one of them. A finding produced this way SHALL cite the changed line, and SHALL name the doc whose rule is broken in its text. Citation gates, verification and merge stay unchanged.

#### Scenario: Prompt contains the rule instruction
- **WHEN** the conventions finder prompt is built with at least one linked doc
- **THEN** the prompt contains an instruction to report changed lines that break rules in the listed docs
- **AND** the prompt lists each included doc's path

#### Scenario: Violation finding cites the changed code
- **WHEN** the conventions finder reports that a changed line breaks a rule in `docs/untrusted-content.md`
- **THEN** the finding's citation points at the changed line in the diff, not at the doc
- **AND** the finding text names `docs/untrusted-content.md`

#### Scenario: No linked docs leaves the prompt unchanged
- **WHEN** no `CLAUDE.md` or `coding-standards.md` read by the finder contains a followable link
- **THEN** the conventions finder prompt is identical to the prompt built before this change