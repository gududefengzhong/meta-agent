# Dogfood: `bug_fix` eval baseline

This is the project-level evaluation baseline for the current
API-first bugfix agent. It is intentionally small and repo-local:
no SWE-bench, no MCP, no VS Code, no Kubernetes. The goal is to make
the current product loop comparable across runs while reusing the same
task-level telemetry contract that the API exposes.

## Source Of Truth

The source of truth for per-task evaluation metrics is:

- `GET /v1/tasks/{task_id}/observability`

That endpoint is backed by the shared read model in:

- [src/meta_agent/core/domain/task_observability.py](../../src/meta_agent/core/domain/task_observability.py)
- [src/meta_agent/api/services/task_observability.py](../../src/meta_agent/api/services/task_observability.py)

It derives one compact summary from persisted telemetry:

- `tasks.result_json` for verifier / patch / attempts / failure outcome
- `llm_usage_logs` for call / token / cost / latency counters
- `audit_events` for tool and human-intervention counters

The eval baseline reuses this summary contract. It does not maintain a
second, test-only metric definition.

## Scope

The baseline lives at
[tests/integration/test_bug_fix_eval_baseline.py](../../tests/integration/test_bug_fix_eval_baseline.py)
and runs `bug_fix` against five fixed fixture repos:

| case | language | verifier |
|---|---|---|
| `py_greeting_punctuation` | Python | `python_test` |
| `py_discount_validation` | Python | `python_test` |
| `py_tax_percent` | Python | `python_test` |
| `ts_greeting_punctuation` | TypeScript | `typescript_test` |
| `ts_clamp_range` | TypeScript | `typescript_test` |

Each case creates a fresh git repo, submits one `BUG_FIX` task through
the real Postgres/Redis/worker/Docker workspace path, uses a real
OpenRouter model, then builds the final per-case row from the shared
task observability summary.

## What The Summary Measures

`GET /v1/tasks/{task_id}/observability` returns these bugfix-oriented
task metrics:

- `task_id`
- `state`
- `result_status`
- `verifier_passed`
- `failure_category`
- `failure_kind`
- `attempts`
- `files_changed`
- `patch_present`
- `llm_calls`
- `llm_failures`
- `total_tokens`
- `total_cost_usd_micros`
- `total_latency_ms`
- `tool_events`
- `tool_failures`
- `human_interventions`
- `cost_by_step_kind`
- `models`

The eval baseline prints the same shape per case, then derives one
small aggregate block from those summaries:

- `success_rate`
- `average_tokens_per_case`
- `average_cost_usd_micros_per_case`
- `tool_failures`
- `verifier_failures`
- `human_interventions`
- `llm_failures`

## Harness Contract

The integration test asserts only the harness contract:

- each task reaches a terminal state
- each task records at least one LLM usage row
- each task records at least one tool audit event

It does not assert pass@1. Quality is the printed JSON summary, not the
pytest exit code.

## How To Run

```bash
docker build -t meta-agent:local .

OPENROUTER_MODEL=deepseek/deepseek-v4-pro \
pytest tests/integration/test_bug_fix_eval_baseline.py \
  -m "integration and real_llm" -v -s
```

`OPENROUTER_API_KEY` can be exported or placed in `<repo>/.env`; the
fixture loader follows the same rule as the single-case real LLM
dogfood.

## How To Read The Output

The test prints one `BUG_FIX_V2_EVAL_BASELINE_JSON` block.

Interpret it in two layers:

1. Per-case rows
   These are the JSON-safe rendering of the shared observability
   summary plus a few fixture labels like `case_id` and `language`.

2. `jd_metrics`
   This is a simple aggregate over the same per-task summaries. It is
   useful for product and interview review, but it is not a separate
   data source.

If a future run disagrees with the eval JSON, the task-level truth
should be checked in this order:

1. `/v1/tasks/{task_id}/observability`
2. `/v1/tasks/{task_id}/result`
3. `/v1/tasks/{task_id}/trajectory`
4. raw `audit_events` / `llm_usage_logs` only if deeper debugging is required

## Boundary

This baseline is not a benchmark submission. It is a reproducible local
quality harness for the current bugfix product shape:

- real LLM calls
- deterministic verifier
- persisted tool audit
- persisted LLM usage / cost
- structured failure explanation
- one shared observability read model

The important constraint is consistency: product surfaces, dogfood
evaluation, and future dashboards should all reuse the same task-level
observability summary instead of redefining metrics in parallel.

## Run A — `deepseek/deepseek-v4-pro`

First full 5-case run on 2026-05-27 Asia/Shanghai.

| metric | value |
|---|---|
| Command | `OPENROUTER_MODEL=deepseek/deepseek-v4-pro pytest tests/integration/test_bug_fix_eval_baseline.py -m "integration and real_llm" -v -s` |
| Result | ✅ `1 passed, 1 warning in 106.75s` |
| Cases | 5 |
| Verifier passed | 5 |
| Verifier failed | 0 |
| Total tokens | 31,744 |
| Total cost | 29,315 micro-USD |
| Success rate | 100% |
| Average tokens / case | 6,348.8 |
| Average cost / case | 5,863 micro-USD |
| Tool failures | 7 |
| Human interventions | 0 |

Per-case summary:

| case | language | verifier | llm_calls | tool_events | tool_failures | tokens | cost micro-USD |
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
  verifier, tool, or runtime failure categories through the shared
  observability summary.
- The aggregate block is now conceptually downstream of the same
  `/observability` contract used by the product API.
