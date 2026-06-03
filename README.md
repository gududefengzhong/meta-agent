# meta-agent

> **Backend-first Bug Fix Agent** — production-grade LLM workflow: accept a
> repository-scoped bug report over HTTP, run an isolated tool-use loop, verify
> the fix deterministically, and optionally open a PR.

## TL;DR

| | |
|---|---|
| **What it does** | Takes a bug description + repo, drives a plan-act-observe agent loop that reads code, edits files, runs a verifier (lint / type-check / tests), produces a PR-ready patch. |
| **How** | Custom state machine over a Tool Calling registry; tree-sitter-backed code retrieval; OpenRouter for LLM with per-step model routing |
| **Why production-shape** | Multi-tenant audit + cost tracking + budget gate + circuit breaker + rate limiter + redaction layer + checkpoint resume — patterns required for enterprise deployment, present from day 1 |
| **Primary interface** | REST API. CLI exists as a local dev helper, but the product surface is API-first. |

**Goal**: demonstrate ability to ship a focused LLM-driven bug-fix system with
the production patterns enterprise deployment actually needs — auditing,
observability, reliability, cost control, human-in-the-loop.

## Quick start (API path)

```bash
# 1. Configure
cp .env.example .env
# edit .env:
#   OPENROUTER_API_KEY=sk-or-v1-...
#   META_AGENT_AUTH_BACKEND=env
#   META_AGENT_API_KEYS=dev-token:default-tenant:default-user

# 2. Start stack (postgres + redis + migrations + api + worker)
docker compose up --build -d

# 3. Submit a bug-fix task
curl -X POST http://localhost:8000/v1/tasks \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "bug_fix",
    "input_payload": {
      "issue_description": "Add input validation for discount_percent. Values below 0 or above 100 must raise ValueError, and the error message should mention discount_percent.",
      "repo_url": "https://github.com/gududefengzhong/meta-agent-smoke.git",
      "base_ref": "case/py-discount-validation",
      "target_files": ["discount.py", "tests/test_discount.py"],
      "verify_suite": "python_test",
      "model": "deepseek/deepseek-v4-pro"
    }
  }'

# 4. Inspect state / result / trajectory
curl -H "Authorization: Bearer dev-token" \
     http://localhost:8000/v1/tasks/<task_id>

curl -H "Authorization: Bearer dev-token" \
     http://localhost:8000/v1/tasks/<task_id>/result

curl -H "Authorization: Bearer dev-token" \
     "http://localhost:8000/v1/tasks/<task_id>/trajectory"
```

## Docker operations

The default local stack is defined in `docker-compose.yml`:

- `postgres`
- `redis`
- `migrations`
- `api`
- `worker`

The runtime image does **not** include `uv`. Inside containers, use
`alembic ...`, `python ...`, or the compose service commands directly.

### Build / start

Build the local image:

```bash
docker compose build
```

Build only the app-related services after a code or migration change:

```bash
docker compose build api worker migrations
```

Start the full stack in the background:

```bash
docker compose up -d
```

Build and start in one command:

```bash
docker compose up --build -d
```

Start only infra dependencies for local host-based development:

```bash
docker compose -f docker-compose.dev.yml up -d
```

### Database migrations

Recommended: run the one-shot migration service:

```bash
docker compose run --rm migrations
```

Run migrations from the running `api` container:

```bash
docker compose exec api alembic upgrade head
```

If you use `docker-compose.dev.yml` and run API / worker on the host, run Alembic on the host:

```bash
META_AGENT_DB_URL=postgresql+asyncpg://meta_agent:dev-only@localhost:5432/meta_agent \
uv run alembic upgrade head
```

Common rule:

- changed Python code only: `docker compose up -d --force-recreate api worker`
- added/changed Alembic migration: `docker compose build api worker migrations && docker compose run --rm migrations && docker compose up -d --force-recreate api worker`

### Restart / recreate

Restart containers without rebuilding the image:

```bash
docker compose restart api worker
```

Recreate containers from the current image:

```bash
docker compose up -d --force-recreate api worker
```

Full refresh after app-image or migration changes:

```bash
docker compose build api worker migrations
docker compose run --rm migrations
docker compose up -d --force-recreate api worker
```

### Logs / status

Check service status:

```bash
docker compose ps
```

Tail all logs:

```bash
docker compose logs -f
```

Tail only API logs:

```bash
docker compose logs -f api
```

Tail only worker logs:

```bash
docker compose logs -f worker
```

Inspect migration job output:

```bash
docker compose logs migrations
```

### Stop / clean up

Stop and remove containers, keep volumes:

```bash
docker compose down
```

Stop and remove containers plus named volumes:

```bash
docker compose down -v
```

The `-v` form deletes local Postgres and Redis data for this stack. Use it only when you really want a fresh local state.

## Run a bug-fix task

The bug-fix product path is `task_type=bug_fix`. In the current worker
bootstrap, that resolves to the built-in `bug_fix` graph: provision a
per-task git worktree, run the tool-use loop, verify the edit, optionally
push, and persist the full trajectory.

`repo_url` accepts either:

- a local git repository path such as `/Users/me/code/my-repo`
- a cloneable git remote such as `https://github.com/org/repo.git`

For a first run, prefer a throwaway clone with a clean committed `base_ref`.
The agent works on an isolated feature worktree, but the source repo still
needs to be a valid git repository.

When you run the default `docker compose` stack, the worker runs inside a
container. In that setup, a host path like `/Users/me/code/my-repo` is usually
**not visible inside the worker container** unless you explicitly mount it.
So with the compose stack:

- prefer a cloneable remote git URL for `repo_url`
- or mount the host repo into the worker container and pass the in-container path

A placeholder path like `/absolute/path/to/your/git/repo` will fail during
workspace provisioning with `workspace.provision_failed`.

For the `bug_fix -> auto_pr` path, prefer an **HTTPS** GitHub remote such as
`https://github.com/org/repo.git`. The current push implementation injects a
GitHub token through git's HTTPS credential helper; an SSH remote like
`git@github.com:org/repo.git` will not use that helper and therefore will not
push successfully unless the worker container also has working SSH keys.

### Required payload fields

- `issue_description`: the bug statement the agent should fix
- `repo_url`: local path or remote git URL

### Common optional payload fields

- `base_ref`: branch/tag to branch from; typically `main`
- `target_files`: allow-list for the edit loop
- `verify_suite`: `python_test`, `typescript_test`, or another registered verifier
- `model`: override the routed LLM model for this task
- `max_steps`: cap the inner shell-agent loop

### HTTP API

```bash
curl -X POST http://localhost:8000/v1/tasks \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "bug_fix",
    "input_payload": { ...same fields as Quick start... }
  }'
```

Then inspect state / result / observability / trajectory:

```bash
curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/v1/tasks/<task_id>

curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/v1/tasks/<task_id>/result

curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/v1/tasks/<task_id>/observability

curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/v1/tasks/<task_id>/trajectory
```

### What success looks like

On success, `GET /v1/tasks/<task_id>/result` returns an `output` object with
the patch, changed files, verifier result, retry count, branch / commit info,
and push status. If git push credentials are not configured, the task can still
succeed locally; in that case expect `pushed=false` plus a `push_skip_reason`
such as `no_token` or `no_repo_url`.

### `/observability` fields

`GET /v1/tasks/<task_id>/observability` is the task-level summary view used by
the product API and the eval baseline. It derives a compact read model from
`tasks.result_json`, `llm_usage_logs`, and `audit_events`.

Core outcome fields:

- `state`: current lifecycle state of the task row
- `result_status`: terminal result status when present (`succeeded` / `failed`)
- `verifier_passed`: whether the deterministic verifier passed
- `failure_category`: stable failure class derived from result output / error
- `attempts`: number of bug-fix attempts including one replan retry when it happened
- `files_changed`: changed file list from the final patch
- `patch_present`: whether the task produced a patch payload

Cost and model fields:

- `llm_calls`: number of persisted LLM calls for the task
- `llm_failures`: number of LLM calls whose status was not `ok`
- `total_tokens`: summed prompt + completion tokens
- `total_cost_usd_micros`: summed LLM spend in micro-USD
- `total_latency_ms`: summed LLM latency across calls
- `cost_by_step_kind`: cost split by step kind such as `plan` / `edit`
- `models`: unique models used during the task

Tooling and human-loop fields:

- `tool_events`: count of `tool.*` audit events
- `tool_failures`: count of `tool.failed` audit events
- `human_interventions`: count of human approval-related audit events
- `budget_outcome`: one of `not_enabled`, `within_budget`, `awaiting_approval`, `approved`, `rejected`, `aborted`
- `auto_pr_child_status`: one of `not_applicable`, `not_enqueued`, `enqueued`, `created`, `reused`, `skipped`, `failed`, `chain_failed`, `duplicate`

Recommended interpretation order:

1. `result` answers “what did the task produce?”
2. `observability` answers “what did it cost, where did it fail, and what follow-up state did it reach?”
3. `trajectory` answers “what happened step by step?”

## Auto PR (optional follow-up)

`auto_pr` is an optional follow-up task chained from a successful `bug_fix`
run. The chain only fires when the parent bug-fix task actually pushed its
feature branch to the remote.

### Preconditions

- `bug_fix` must finish with `verifier_passed=true` and `pushed=true`
- the worker must be configured with `META_AGENT_GIT_PROVIDER=github`
- the worker must have `META_AGENT_GITHUB_TOKEN` set
- `repo_url` should be a real GitHub HTTPS remote, not a local path

Without that configuration, `bug_fix` can still succeed locally, but no
follow-up `AUTO_PR` task is enqueued.

`auto_pr` is not part of the parent bug-fix success contract. A bug-fix task
can succeed even when:

- no follow-up `AUTO_PR` child is enqueued
- the child task later returns `action=skipped`
- the child task fails due to provider, credential, or remote-state issues

Treat it as a separate, optional publication step.

### Minimal worker config

```env
META_AGENT_GIT_PROVIDER=github
META_AGENT_GITHUB_TOKEN=<github-token-with-push-and-pr-scope>
```

The current worker wiring reuses `META_AGENT_GITHUB_TOKEN` for both branch push
and PR creation. `META_AGENT_GITHUB_TOKEN` by itself is not enough; the worker
only loads the GitHub adapter when `META_AGENT_GIT_PROVIDER=github`.

### Behavior

On a successful pushed bug-fix task, the worker enqueues an `AUTO_PR` child
task using the parent result's `repo_url`, `base_ref`, `head_branch`,
`head_commit_sha`, verifier result, and summary fields. The current GitHub
adapter creates a PR or reuses an existing open PR only when the branch already
points at the same commit. It does not reconcile branch drift or update an old
PR body.

### How to inspect it

After a bug-fix task succeeds, inspect its audit trail for the follow-up:

```bash
curl -H "Authorization: Bearer dev-token" \
  "http://localhost:8000/v1/audits?task_id=<bug_fix_task_id>&limit=200"
```

Look for:

- `task.chain_enqueued`
- `payload.child_task_id`
- `payload.follow_up_type = "auto_pr"`

Then fetch the child task directly:

```bash
curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/v1/tasks/<child_task_id>/result
```

Successful `auto_pr` output includes the action (`created` or `reused`), PR
URL, PR id, base/head refs, and commit SHA. A skipped child returns
`action=skipped` with a machine-readable `reason` such as `no_repo_url`,
`no_commit_sha`, or `verifier_failed`.

## Smoke Cases

The shared smoke-case catalog lives in the separate
`gududefengzhong/meta-agent-smoke` repository. That catalog is an eval /
dogfood input source, so it is intentionally **not** part of the main product
surface.

Use the standalone runner instead:

```bash
# if not already exported in your shell:
export META_AGENT_API_URL=http://localhost:8000
export META_AGENT_TOKEN=dev-token

uv run python examples/smoke_case_runner.py --list
uv run python examples/smoke_case_runner.py --case case/py-safe-join-traversal --print-payload
uv run python examples/smoke_case_runner.py --case case/py-safe-join-traversal --run
```

By default the runner reads the public GitHub raw catalog from
`gududefengzhong/meta-agent-smoke` and reuses the same
`META_AGENT_API_URL` / `META_AGENT_TOKEN` env vars as the API client examples. If
`META_AGENT_TOKEN` is not exported, pass `--token dev-token` explicitly:

```bash
uv run python examples/smoke_case_runner.py \
  --case case/py-safe-join-traversal \
  --token dev-token \
  --run
```

If you need to test a local draft catalog, pass it explicitly with `--catalog`.

## Architecture

```mermaid
flowchart LR
    CLIENT[HTTP client<br/>curl / app / internal service]

    subgraph API [API Layer]
        AUTH[Auth Middleware<br/>Bearer token → RequestContext]
        ROUTER[Routers<br/>tasks / audits / usages / trajectory]
    end

    subgraph Q [Queue]
        STREAMS[Redis Streams<br/>task.commands]
    end

    subgraph Worker [Worker]
        DISPATCH[Dispatcher<br/>consumer-group]
        GRAPHS[Graph Registry<br/>bug_fix / auto_pr / shell_agent]
        TOOLS[Tool Registry<br/>FS / Edit / Shell / Test / code_search / WebFetch]
        SBX[WorkspaceManager<br/>Docker container + git worktree]
    end

    subgraph LLM [LLM Stack]
        ROUTE[LLMRouter<br/>step_kind → model]
        DECS[Decorators<br/>Redact → Budget → RateLimit → Breaker → Metered]
        OR[OpenRouter Adapter]
    end

    subgraph DATA [Persistence]
        PG[(PostgreSQL<br/>tasks / audit_events / llm_usage_logs / task_checkpoints / prompt_assets)]
        CODE[(On-demand code retrieval<br/>tree-sitter + workspace scan)]
        REDIS[(Redis<br/>queue / rate-limit / breaker stats)]
    end

    CLIENT -->|HTTP| API
    AUTH --> ROUTER
    ROUTER -->|enqueue| STREAMS
    STREAMS -->|pop| DISPATCH
    DISPATCH --> GRAPHS
    GRAPHS --> TOOLS
    GRAPHS --> ROUTE
    ROUTE --> DECS --> OR
    TOOLS --> SBX
    GRAPHS -.write.-> PG
    DECS -.write.-> PG
    SBX -.use.-> CODE
    DISPATCH -.->|coordination| REDIS
```

## Key engineering decisions

| Decision | Why | Trade-off |
|---|---|---|
| Custom state machine, not LangChain/LangGraph | Type-safe Pydantic state; async-first; Port pattern for multi-tenant LLM/Tool/Workspace; no framework dictating audit shape | Maintain our own engine; can't drop in LangChain plugins |
| `step_kind` model routing | Cheap models for planning, capable for editing, dedicated for review — measurable per-step cost | Extra routing config; needs benchmarking per step type |
| Multi-tenant from day 1 (`tenant_id` everywhere) | Production bug-fix agents must be safe across users; isolating after the fact is a refactor disaster | Slightly more verbose schemas; one-tenant demo mode for now |
| Container sandbox + git worktree per task | Agent can run untrusted shell commands; tasks isolated from each other | Slower startup (docker pull / exec overhead) |
| Outbox + webhook for async notifications | Long human-in-the-loop pauses without holding worker resources | Extra storage; eventual-consistency between webhook and API |
| **No** LangSmith / proprietary SaaS observability | Audit + usage tables are source of truth; Langfuse is an optional analysis layer with auto export + manual re-export | We still need task-centric dashboard views in our own product surface |
| BYO LLM key (deployment env) | Operator controls provider + billing; the app does not invent a second credential system | Need redaction and careful secret handling on the server side |

## Capabilities (delivered)

### L1 graphs (`src/meta_agent/core/orchestration/graphs/`)

- **`bug_fix`** — the main bug-fix graph. Runs plan / patch / verify / push / finalize over an isolated workspace; deterministic verify; multi-language (Python ruff+pytest, TypeScript tsc+vitest). Replans once on verify failure with prior plan + diff + verifier output as feedback.
- **`auto_pr`** — publishes a feature-branch commit as PR via `GitProvider` Port (FakeGitProvider + GitHubGitProvider). `BUG_FIX → AUTO_PR` follow-up chain.
- **`shell_agent`** — internal reusable execution graph that powers the tool loop inside `bug_fix`.

### Tool registry

`FileSystemTool` (read / list / grep) · `EditTool` (write / patch apply) · `ShellTool` (allow-list + timeout + output cap) · `TestTool` (Python + TypeScript verifier suites) · `code_search` / `get_definition` / `get_references` / `outline` (tree-sitter, on-demand workspace scan) · `WebFetch` (URL → text, domain allow-list) · `DocSearch`.

### Production patterns (from `α` / `γ` phases)

- **Auth**: `TokenValidator` Port (env CSV + DB-backed); `Authorization: Bearer <token>` → `RequestContext`
- **Rate limit**: Redis token bucket, tenant × model × tool dimensions
- **Circuit breaker**: `pybreaker` + Redis shared stats; explicit fallback
- **Budget**: explicit task-level threshold (`budget_policy` + `budget_threshold_micros`) plus a separate tenant-level monthly guard in the LLM client
- **Audit**: `audit_events` table writes every graph decision (rate limited, redacted, signed by `step_kind`)
- **Cost tracking**: `llm_usage_logs` per LLM call (model / tokens / cost / step_kind / prompt_version)
- **Prompt observability**: `llm_usage_logs.prompt_excerpt` stores a redacted, bounded request preview; Langfuse exports it as generation input
- **Tool failure observability**: `tool.failed` audit rows carry a redacted `error_excerpt`
- **Checkpoint resume**: worker restart picks up RUNNING tasks from `task_checkpoints`
- **Human-in-the-loop**: public REST contract supports `permission_mode ∈ {auto, approve_before_push}`; inline per-tool / per-plan gating remains internal-only for now
- **Trajectory replay**: `GET /v1/tasks/{id}/trajectory` joins audit + checkpoints + usage
- **Prompt redaction**: LLM request + response scanned for secrets / PII before logging
- **Async notification**: Outbox + webhook with HMAC + retry + dedupe + dead-letter

## Scope boundary

This repository is intentionally scoped as a **bug-fix agent**.

- Primary path: `bug_fix`
- Optional follow-up: `auto_pr`
- Evaluation support: external smoke catalog + runner

The repository still contains auxiliary runtime pieces and some historical
graphs, but the product surface, README examples, and dogfood baseline all
optimize for bug-fix.

## Project structure

```
src/meta_agent/
├── api/              # FastAPI + routers (tasks / sessions / audits / usages / trajectory)
├── cli/              # optional local dev helper around the HTTP API
├── core/
│   ├── domain/       # Task / TaskResult / Permission / Outbox / Webhook (Pydantic models)
│   ├── ports/        # LLMClient / RateLimiter / CircuitBreaker / WorkspaceManager / GitProvider / etc.
│   └── orchestration/
│       ├── graphs/   # bug_fix / auto_pr / shell_agent
│       └── ...       # GraphRegistry / GraphRunner / state machine primitives
├── infra/            # Port implementations
│   ├── llm/          # OpenRouter adapter + decorators (Redact / Budget / RateLimit / Breaker / Metered / Router)
│   ├── persistence/  # Asyncpg pool + repos
│   ├── workspace/    # DockerWorkspaceManager + LocalGitWorkspaceManager
│   ├── tools/        # Local + Docker workspace tools
│   ├── ratelimit/    # Redis token bucket + in-memory
│   ├── circuitbreaker/  # pybreaker + Redis shared
│   ├── webhook/      # Outbox dispatcher + HMAC signer
│   └── ...
└── worker/           # Dispatcher / GraphRunner / WorkspaceManager wiring / shutdown
```

## Not in scope

These are explicit future slices, not current product claims:

- K8s Helm chart / Prometheus / OTel exporter / SSO / RBAC / Web UI (Phase ε)
- gVisor / Firecracker sandbox (Phase ζ)
- AGENTS.md project memory / PR review feedback / BYO LLM config UI / MCP Server (Phase δ-3)
- VS Code plugin (was built; removed because the focused product is API-first bug fix)
- Multi-agent orchestration / hooks / plugins (L2 platform layer, beyond focused agent scope)

## Tech stack

Python 3.11+ · Pydantic v2 · FastAPI · asyncpg · Redis Streams · OpenRouter · pybreaker · Docker · alembic · tree-sitter.

## Test plan

```bash
pytest -q                    # unit tests
pytest -m integration        # docker-compose + real db / redis tests
mypy                         # strict
ruff check . && ruff format --check .
```

## References

- `docs/specs/AGENT_SPEC.md` — full product spec
- `docs/specs/INFRA_SELECTION_MATRIX.md` — Redis Streams vs alternatives, etc.
- `docs/specs/CONTEXT_PROPAGATION.md` — trace_id / tenant_id propagation contract
- `CLAUDE.md` — development collaboration rules

## Developer helpers

The repo still ships a local CLI and smoke runner for development / debugging.
They are intentionally not the primary product interface.
