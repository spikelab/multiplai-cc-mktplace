# costs — behavioural contract

Run with:

```
uv run --script ../../../multiplai-dev/skills/skill-creator/scripts/promote_skill.py . --contract
```

These assertions pin the *shape* of what `costs_report.py` accepts and emits,
not the numbers — a cost ledger's contents change hourly, so asserting on
totals would produce a test that fails for the wrong reason and gets deleted.
What must not change silently is the set of `--by` dimensions and the fact that
`--json` emits JSON: the costs SKILL.md routes the user to specific flag
combinations, and a renamed dimension turns that table into a set of commands
that error.

Each case is a shell command plus a substring that must appear in its output.
Commands run with this directory as cwd.

### help lists every documented --by dimension

```sh
uv run --project ../../../.. ../../scripts/costs_report.py --help
```

Expect: {branch,component,day,model,project,session,skill}

### an unknown --by dimension is rejected rather than silently ignored

```sh
uv run --project ../../../.. ../../scripts/costs_report.py --by nonsense 2>&1 || true
```

Expect: invalid choice

### --json emits parseable JSON, not a formatted table

```sh
uv run --project ../../../.. ../../scripts/costs_report.py --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('json-ok')"
```

Expect: json-ok
