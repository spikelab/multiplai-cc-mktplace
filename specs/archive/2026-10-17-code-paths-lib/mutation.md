# Mutation testing

- Tool: cosmic-ray 8.7.0, lines changed since the build's first block only
- Score before the survivor round: 0.61 (182 killed / 300)
- Score after the survivor round: 0.61 (182 killed / 300)
- Minimum (`mutation.min_score`): 0.80 — FAILED
- Timed-out test runs (counted as killed): 1
- Mutants that could not be judged (left out of the score): 0
- Time allowed per test run under a mutant: 30 s (`mutation.timeout`, or 3× the unmutated suite's run when that is longer)
- Sampled: 300 of 5636 mutants ran (`mutation.max_mutants`); the sample is deterministic

## Files mutated

- `plugins/multiplai-dev/lib/code_facts/code_facts/__init__.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/cache.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/explore.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/fallback.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/paths.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/prompts.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/scope.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/trace_gate.py`
- `plugins/multiplai-dev/lib/code_facts/code_facts/tracing.py`

## Survivor round

The test writer failed (failed: Claude Code returned an error result: Reached maximum number of turns (30) (exit code: 1)
CLI stderr:
2026-10-17T10:20:18.221Z [ERROR] Failed to flush logs to Datadog: AxiosError: timeout of 5000ms exceeded); its edits were discarded and the first-run score stands.

## Surviving mutants

| File | Line | Operator |
|---|---|---|
| `plugins/multiplai-dev/lib/code_facts/code_facts/cache.py` | 108 | `core/ReplaceContinueWithBreak` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/cache.py` | 111 | `core/ReplaceContinueWithBreak` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 100 | `core/ReplaceTrueWithFalse` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 106 | `core/ReplaceComparisonOperator_Eq_GtE` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 165 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 165 | `core/ReplaceBinaryOperator_BitOr_Mod` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 179 | `core/ReplaceComparisonOperator_Lt_IsNot` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 179 | `core/ReplaceComparisonOperator_NotEq_Lt` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 183 | `core/ReplaceBinaryOperator_Add_BitOr` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 189 | `core/ReplaceComparisonOperator_Eq_IsNot` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 222 | `core/ReplaceBinaryOperator_BitOr_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 239 | `core/ReplaceComparisonOperator_Eq_LtE` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 279 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/commands.py` | 376 | `core/ReplaceComparisonOperator_Eq_IsNot` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 80 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 101 | `core/ReplaceBinaryOperator_BitOr_Div` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 101 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 185 | `core/ReplaceBinaryOperator_BitOr_Mul` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 185 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 221 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 221 | `core/ReplaceBinaryOperator_BitOr_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 244 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_patches.py` | 244 | `core/ReplaceBinaryOperator_BitOr_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 254 | `core/ReplaceBinaryOperator_BitOr_Div` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 254 | `core/ReplaceBinaryOperator_BitOr_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 255 | `core/ReplaceBinaryOperator_Add_BitOr` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 260 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 260 | `core/ReplaceBinaryOperator_BitOr_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 266 | `core/ReplaceComparisonOperator_LtE_Gt` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 266 | `core/ReplaceComparisonOperator_LtE_GtE` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 266 | `core/ReplaceComparisonOperator_LtE_NotEq` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 301 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 301 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 301 | `core/ReplaceBinaryOperator_BitOr_Mul` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 330 | `core/ReplaceBinaryOperator_BitOr_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 372 | `core/ReplaceBinaryOperator_BitOr_Mul` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 423 | `core/ReplaceBinaryOperator_BitOr_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 423 | `core/ReplaceBinaryOperator_BitOr_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 460 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 470 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 588 | `core/ReplaceBinaryOperator_BitOr_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 694 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 699 | `core/ReplaceBinaryOperator_BitOr_Mod` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/e2e_tests.py` | 703 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 158 | `core/ReplaceBinaryOperator_BitOr_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 158 | `core/ReplaceBinaryOperator_BitOr_Mod` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 267 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 267 | `core/ReplaceBinaryOperator_BitOr_Div` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 267 | `core/ReplaceBinaryOperator_BitOr_Div` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 267 | `core/ReplaceBinaryOperator_BitOr_Mod` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 318 | `core/ReplaceBinaryOperator_BitOr_Sub` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 328 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 342 | `core/ReplaceBinaryOperator_BitOr_Mod` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 389 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 401 | `core/ReplaceComparisonOperator_Eq_LtE` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 407 | `core/ReplaceBinaryOperator_Sub_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 407 | `core/ReplaceBinaryOperator_Sub_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 501 | `core/ReplaceBinaryOperator_BitOr_Div` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 570 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 570 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 616 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 623 | `core/ReplaceComparisonOperator_Eq_GtE` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 623 | `core/ReplaceComparisonOperator_Eq_LtE` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 756 | `core/ReplaceComparisonOperator_Eq_Is` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 778 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 779 | `core/ReplaceComparisonOperator_Eq_GtE` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 802 | `core/ReplaceBinaryOperator_BitOr_Mul` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 802 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 825 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 825 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 853 | `core/ReplaceBinaryOperator_BitOr_Mul` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 875 | `core/ReplaceBinaryOperator_BitOr_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 875 | `core/ReplaceBinaryOperator_BitOr_RShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 895 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1026 | `core/ReplaceBinaryOperator_BitOr_Sub` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1039 | `core/ReplaceBinaryOperator_BitOr_BitXor` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1075 | `core/ReplaceBinaryOperator_BitOr_Mod` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1100 | `core/ReplaceComparisonOperator_GtE_NotEq` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1110 | `core/ReplaceComparisonOperator_Eq_Is` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1117 | `core/ReplaceComparisonOperator_LtE_Lt` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1140 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1224 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1224 | `core/ReplaceBinaryOperator_BitOr_Sub` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1251 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/entries.py` | 1299 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/fallback.py` | 146 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/fallback.py` | 146 | `core/ReplaceBinaryOperator_BitOr_Sub` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/fallback.py` | 166 | `core/ReplaceBinaryOperator_BitOr_Div` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/fallback.py` | 180 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/fallback.py` | 192 | `core/ReplaceBinaryOperator_BitOr_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/paths.py` | 376 | `core/ReplaceBinaryOperator_BitOr_BitAnd` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/paths.py` | 382 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/paths.py` | 385 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/paths.py` | 386 | `core/ReplaceBinaryOperator_BitOr_Mul` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/paths.py` | 457 | `core/ReplaceUnaryOperator_USub_Invert` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/paths.py` | 519 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/prompts.py` | 13 | `core/AddNot` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 68 | `core/ReplaceTrueWithFalse` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 168 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 173 | `core/ReplaceBinaryOperator_BitOr_Div` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 212 | `core/ReplaceBinaryOperator_BitOr_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 243 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 243 | `core/ReplaceBinaryOperator_BitOr_Sub` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 354 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 405 | `core/NumberReplacer` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 406 | `core/ReplaceContinueWithBreak` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 411 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 456 | `core/ReplaceBinaryOperator_BitOr_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 526 | `core/ReplaceComparisonOperator_Eq_Is` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/pyindex.py` | 527 | `core/ReplaceComparisonOperator_Eq_IsNot` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/scope.py` | 24 | `core/ReplaceBinaryOperator_BitOr_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/scope.py` | 65 | `core/ReplaceBinaryOperator_Add_FloorDiv` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/scope.py` | 65 | `core/ReplaceBinaryOperator_Add_Pow` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/scope.py` | 90 | `core/ReplaceBinaryOperator_Add_LShift` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/trace_gate.py` | 193 | `core/ReplaceComparisonOperator_LtE_IsNot` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/tracing.py` | 132 | `core/ReplaceBinaryOperator_BitOr_Add` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/tracing.py` | 132 | `core/ReplaceBinaryOperator_BitOr_Mod` |
| `plugins/multiplai-dev/lib/code_facts/code_facts/tracing.py` | 132 | `core/ReplaceBinaryOperator_BitOr_Mul` |
