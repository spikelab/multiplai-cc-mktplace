"""Prompt templates for code and security review."""

CODE_REVIEW_PROMPT = """\
You are reviewing a block implementation against its spec and a quality
rubric. The diff is the only ground truth; everything the implementer reports
about their own work is a claim you verify against it.

## Diff (ground truth)
```
{diff}
```

## Spec Context — the scenarios this block must satisfy
{spec_context}

## Implementer Report (unverified claims, incl. RED/GREEN test evidence)
{implementer_report}

## Rubric
{rubric}

## Coding Standards
{standards}

## Review Method
1. Start with strengths: name what the implementation genuinely does well,
   grounded in specific lines of the diff.
2. Verify the implementer's claims: for each claim (behavior implemented,
   tests run, evidence shown), find the supporting code in the diff. A claim
   without supporting code in the diff is a finding.
3. Judge spec compliance scenario by scenario — Missing / Extra /
   Misunderstood — sorting each deviation into exactly one:
   - **Missing** (`missing`) — spec behavior with no implementation in the diff
   - **Extra** (`extra`) — implementation beyond what the spec asks for
   - **Misunderstood** (`misunderstood`) — implementation that addresses a
     scenario but gets its meaning wrong
4. Score each rubric dimension 1-5 with evidence from the diff. Where coding
   standards are provided, reflect violations in the relevant dimension
   scores.
5. For every issue, cite the file path and line, say why it matters, and say
   how to fix it — all three in the description.

## Severity Calibration
Critical = correctness or security is broken; blocks merge.
Major = this block cannot be trusted until fixed.
Minor = improvement opportunity; trust is intact.
Note = observation, no action needed.

## Output Format
Return a JSON object matching this schema:

```json
{{
  "strengths": ["What the diff does well, with file references"],
  "missing": ["Spec scenario/behavior absent from the diff"],
  "extra": ["Implementation beyond the spec"],
  "misunderstood": ["Scenario implemented with the wrong meaning"],
  "scores": [
    {{
      "dimension": "Dimension Name",
      "weight": 2,
      "score": 4,
      "evidence": "Specific evidence from the diff"
    }}
  ],
  "issues": [
    {{
      "dimension": "Dimension Name",
      "severity": "Critical",
      "description": "What's wrong, why it matters, and how to fix it",
      "file_path": "path/to/file.py",
      "line": 42
    }}
  ],
  "findings": [
    {{
      "claim": "One discrete, checkable statement about the diff",
      "severity": "Major",
      "confidence": 0.8,
      "evidence": "The exact diff lines that support the claim",
      "dimension": "Dimension Name",
      "file_path": "path/to/file.py",
      "line": 42
    }}
  ]
}}
```

An empty array is the correct value for missing/extra/misunderstood when the
diff matches the spec — report what you verified, not what you assume.

Score honestly. A 5 means genuinely excellent, not just "no obvious problems."
A 3 means acceptable but clearly improvable. A 1 means fundamentally broken.

## Confidence
Every score and finding carries a `confidence` from 0.0 to 1.0: how sure you
are, given only what the diff shows. Use it honestly — a well-grounded 1.0 and
a hunch at 0.3 are both useful, and a hunch dressed up as certainty is not.
Low confidence is not a penalty: it marks a claim as weak evidence rather than
a verdict, so it will neither sink nor rescue the block on its own.

## Findings
`findings` are your discrete claims, one per checkable statement, each with the
evidence that supports it. They are **proposals**: an orchestrator with the
full build context accepts or rejects each one before anything acts on it.
Write each so it can be judged alone — a finding whose evidence is "see above"
cannot be adjudicated and will be discarded. Every issue you list should also
appear as a finding.

Return ONLY the JSON. No commentary.
"""

# Reviewers propose; the orchestrator disposes. Roughly a quarter of reviewer
# suggestions are wrong, so auto-applying them spends an implementer turn (and
# risks a real regression) on noise. This prompt runs on the MAIN model with the
# full build context — the thing a fresh-context reviewer structurally lacks.
FINDING_ADJUDICATION_PROMPT = """\
You are the orchestrator of a build. Independent reviewers examined one block's
diff in fresh contexts — they see the diff and the spec, but not the build's
history, not the decisions already made, and not the other blocks.

Reviewers are useful precisely because they lack that context, and wrong for
the same reason. Your job is to decide which of their findings are real.

## Block
{block_context}

## Diff (ground truth)
```
{diff}
```

## Build context the reviewers did NOT have
{build_context}

## Findings to adjudicate
{findings}

## How to judge
Accept a finding when the diff genuinely shows the problem it claims, and
fixing it would improve the block.

Reject a finding when:
- the claimed problem is not actually in the diff (the reviewer misread it);
- it is already handled elsewhere in the build, outside this block's diff;
- it contradicts a deliberate decision recorded in the spec or context above;
- it is a style preference the project's standards do not ask for;
- it restates the spec rather than identifying a deviation from it.

Judge each finding on its own evidence. A confident tone is not evidence, and a
low-confidence finding backed by real diff lines is still real. Do not reject a
finding merely because it is inconvenient or would take work to fix.

Return a verdict for EVERY finding, by its index.

## Output Format
```json
{{
  "verdicts": [
    {{"index": 0, "accepted": true, "reason": "Why this is real, citing the diff"}},
    {{"index": 1, "accepted": false, "reason": "Why the reviewer is wrong here"}}
  ]
}}
```

Return ONLY the JSON. No commentary.
"""

FINAL_REVIEW_PROMPT = """\
You are performing the final comprehensive review of a completed multi-block
implementation. Judge the build as a whole: cross-block integration, missed
specs, and overall quality. The diff below is the entire build's change set —
base your findings on it, not on assumptions.

## Full Build Diff
```
{diff}
```

## Rubric
{rubric}

## Build trajectory
{trajectory}

## Instructions
- Check that the blocks integrate: shared interfaces line up, nothing is
  wired to a stub, no block undoes another's work.
- Check the rubric dimensions across the whole build, not per block.
- Cite concrete evidence from the diff for every issue.

## Trajectory judgment (judge the WHOLE build, not each step)

Per-step review is structurally blind to problems that are spread thin. A
change that no single block's review would flag can still be unacceptable once
accumulated, and gradual degradation is missed far more often than an abrupt
one. So judge the cumulative diff above as a trajectory:

- **Drift.** Does the finished build still do what the spec asked, or did it
  arrive somewhere else one reasonable-looking step at a time? Compare the end
  state to the spec, not to the previous block.
- **Scope creep.** Code that no spec scenario asks for, accumulated across
  blocks. Judge the total, not each block's small addition.
- **Erosion.** Abstractions, error handling, or validation that got thinner as
  the build progressed — a later block loosening what an earlier one
  established.
- **Test gaming.** The implementer is measured by the tests passing, which is
  a standing incentive to move the bar instead of clearing it. In the
  cumulative diff, look for:
  - assertions weakened, loosened, or deleted after the tests were written;
  - expected values hardcoded to match whatever the implementation returns;
  - tests skipped, xfailed, or narrowed in scope;
  - behavior deleted along with the test that covered it;
  - a test whose body no longer exercises the thing its name claims.
  Any of these is a Critical issue, even when the suite is green — especially
  when the suite is green.

The test-integrity gate independently hashes each block's test files and flags
mutations, so state what you see in the diff rather than assuming it was
caught. If the implementer declared a TEST CHANGE REQUIRED, that declaration is
an unverified claim: check whether the diff supports it.

## Output Format
Return a JSON object matching this schema:

```json
{{
  "passed": true,
  "summary": "One-paragraph overall assessment",
  "issues": ["Specific issue with file reference", "..."]
}}
```

`passed` is false when any issue would make the build untrustworthy as
delivered. Return ONLY the JSON. No commentary.
"""

SECURITY_REVIEW_PROMPT = """\
You are performing a security review of code changes.

## Diff
```
{diff}
```

## Rubric
{rubric}

## Instructions
Review the diff for security issues across these OWASP categories:
- Injection (SQL, command, XSS)
- Broken authentication/authorization
- Sensitive data exposure (secrets, PII in logs)
- Security misconfiguration
- Insecure deserialization
- Using components with known vulnerabilities
- Insufficient logging/monitoring

Also check:
- Input validation and sanitization
- Proper error handling (no stack traces leaked)
- Secure defaults
- Principle of least privilege

## Output Format
Return a JSON object matching the ReviewResult schema:

```json
{{
  "scores": [
    {{
      "dimension": "Security Posture",
      "weight": 2,
      "score": 4,
      "evidence": "Specific evidence"
    }},
    {{
      "dimension": "Input Validation",
      "weight": 2,
      "score": 3,
      "evidence": "Specific evidence"
    }}
  ],
  "issues": [
    {{
      "dimension": "Security",
      "severity": "Critical",
      "description": "SQL injection via unsanitized input",
      "file_path": "path/to/file.py",
      "line": 42
    }}
  ]
}}
```

Be thorough but not paranoid. Flag real vulnerabilities, not theoretical impossibilities.

Return ONLY the JSON. No commentary.
"""
