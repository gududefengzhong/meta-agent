# Dogfood: small `bug_fix_v2` eval baseline

This is the project-level evaluation baseline for the interview-focused
Bug Fix CLI Agent. It is intentionally small and repo-local: no
SWE-bench, no MCP, no VS Code, no Kubernetes. The goal is to make the
current product loop comparable across runs.

## Scope

The baseline lives at
[`tests/integration/test_bug_fix_v2_eval_baseline.py`](../../tests/integration/test_bug_fix_v2_eval_baseline.py)
and runs `bug_fix_v2` against five fixed fixture repos:

| case | language | verifier |
|---|---|---|
| `py_greeting_punctuation` | Python | `python_test` |
| `py_discount_validation` | Python | `python_test` |
| `py_tax_percent` | Python | `python_test` |
| `ts_greeting_punctuation` | TypeScript | `typescript_test` |
| `ts_clamp_range` | TypeScript | `typescript_test` |

Each case creates a fresh git repo, submits one `BUG_FIX` task through
the real Postgres/Redis/worker/Docker workspace path, uses a real
OpenRouter model, and records the result row plus usage/audit summaries.

## What It Measures

The test prints one `BUG_FIX_V2_EVAL_BASELINE_JSON` block containing:

- `verifier_passed`
- `failure_category`
- `files_changed`
- `attempts`
- `tool_invocations`
- `patch_present`
- `llm_calls`
- `tokens`
- `cost_usd_micros`
- `tool_events`
- `tool_failures`

The test asserts only the harness contract:

- each task reaches a terminal state
- each task produces a result row
- each task records at least one LLM usage row
- each task records at least one tool audit event

It does not assert pass@1. Quality is the JSON summary, not the pytest
exit code.

## How To Run

```bash
docker build -t meta-agent:local .

OPENROUTER_MODEL=deepseek/deepseek-v4-pro \
pytest tests/integration/test_bug_fix_v2_eval_baseline.py \
  -m "integration and real_llm" -v -s
```

`OPENROUTER_API_KEY` can be exported or placed in `<repo>/.env`; the
fixture loader follows the same rule as the single-case real LLM
dogfood.

## Boundary

This baseline is not a benchmark submission. It is a reproducible local
quality harness for the current product shape:

- real LLM calls
- deterministic test verifier
- persisted tool audit
- persisted LLM usage/cost
- structured failure explanation

Once this is stable, the next useful improvement is to save selected
baseline JSON runs in this document as dated snapshots.

## Run A — `deepseek/deepseek-v4-pro`

First full 5-case run on 2026-05-27 Asia/Shanghai.

| metric | value |
|---|---|
| Command | `OPENROUTER_MODEL=deepseek/deepseek-v4-pro pytest tests/integration/test_bug_fix_v2_eval_baseline.py -m "integration and real_llm" -v -s` |
| Result | ✅ `1 passed, 1 warning in 106.75s` |
| Cases | 5 |
| Verifier passed | 5 |
| Verifier failed | 0 |
| Total tokens | 31,744 |
| Total cost | 29,315 micro-USD |

Per-case summary:

| case | language | verifier | llm_calls | tool_invocations | tool_failures | tokens | cost micro-USD |
|---|---|---:|---:|---:|---:|---:|---:|
| `py_greeting_punctuation` | Python | ✅ | 2 | 1 | 0 | 2,190 | 2,882 |
| `py_discount_validation` | Python | ✅ | 6 | 5 | 2 | 8,687 | 7,345 |
| `py_tax_percent` | Python | ✅ | 6 | 5 | 2 | 8,090 | 7,195 |
| `ts_greeting_punctuation` | TypeScript | ✅ | 5 | 4 | 1 | 6,598 | 6,154 |
| `ts_clamp_range` | TypeScript | ✅ | 5 | 4 | 2 | 6,179 | 5,739 |

Observations:

- Pass@1 on this small baseline was 5/5 for this run.
- The TypeScript and multi-step Python cases still produced `tool.failed`
  events even though final verification passed, which confirms the
  tool-call audit is useful for identifying inefficient or invalid
  intermediate actions.
- All rows had `failure_category=null`; future regressions should show
  `verifier_failed`, `tool_failed` diagnostics in trace, or truncation
  categories from `failure_explanation`.
