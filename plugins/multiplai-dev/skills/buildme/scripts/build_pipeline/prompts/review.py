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
  ]
}}
```

An empty array is the correct value for missing/extra/misunderstood when the
diff matches the spec — report what you verified, not what you assume.

Score honestly. A 5 means genuinely excellent, not just "no obvious problems."
A 3 means acceptable but clearly improvable. A 1 means fundamentally broken.

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

## Instructions
- Check that the blocks integrate: shared interfaces line up, nothing is
  wired to a stub, no block undoes another's work.
- Check the rubric dimensions across the whole build, not per block.
- Cite concrete evidence from the diff for every issue.

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
