"""Prompt templates for code and security review."""

CODE_REVIEW_PROMPT = """\
You are reviewing code changes against a quality rubric.

## Diff
```
{diff}
```

## Rubric
{rubric}

## Coding Standards
{standards}

## Spec Context
{spec_context}

## Instructions
Score each rubric dimension on a 1-5 scale with evidence from the diff.
Where coding standards are provided above, treat violations as issues and
reflect them in the relevant dimension scores.
Flag issues with severity (Critical, Major, Minor, Note).

Critical = blocks merge, must fix.
Major = significantly degrades quality, should fix.
Minor = improvement opportunity, nice to fix.
Note = observation, no action needed.

## Output Format
Return a JSON object matching this schema:

```json
{{
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
      "description": "What's wrong",
      "file_path": "path/to/file.py",
      "line": 42
    }}
  ]
}}
```

Score honestly. A 5 means genuinely excellent, not just "no obvious problems."
A 3 means acceptable but clearly improvable. A 1 means fundamentally broken.

Return ONLY the JSON. No commentary.
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
