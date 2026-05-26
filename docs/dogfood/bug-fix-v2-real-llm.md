# Dogfood: `bug_fix_v2` against a real LLM

> Goal: take `bug_fix_v2` (our agent loop) out of unit-test land where every
> LLM response is a hand-scripted `FakeLLMClient`, and point it at a real
> OpenRouter model on a real bug we constructed. Surface concrete signals
> (and concrete gaps) about agent quality and harness completeness.

The integration test lives at
[`tests/integration/test_bug_fix_v2_real_llm.py`](../../tests/integration/test_bug_fix_v2_real_llm.py)
(marker: `real_llm`). Skipped automatically when `OPENROUTER_API_KEY`
is not set, so unit-CI does not burn tokens.

---

## The bug

`src/discount.py` in a fresh repo we generate per run:

```python
def discount_price(price: float, discount_percent: float) -> float:
    """Apply a percentage discount to price."""
    return price - (price * discount_percent / 100)
```

No input validation. Two of the five pytest cases the fixture ships fail
on this code:

| test | expectation | current behavior |
|---|---|---|
| `test_discount_normal` | `discount_price(100, 20) == 80` | passes |
| `test_discount_zero`   | `discount_price(100, 0) == 100` | passes |
| `test_discount_full`   | `discount_price(100, 100) == 0` | passes |
| `test_discount_negative_raises` | `ValueError`, msg contains `"discount_percent"` | **silently returns 110** |
| `test_discount_over_100_raises` | `ValueError`, msg contains `"discount_percent"` | **silently returns −50** |

The agent has to add validation without breaking the three normal cases.

## Model

| key | value |
|---|---|
| Routing provider | OpenRouter (`infra/llm/openrouter.py`) |
| Default model | `deepseek/deepseek-v4-pro` |
| Stack | `OpenRouterClient` → `RedactingLLMClient` → `MeteredLLMClient` in this dogfood harness |
| Configurable via | `OPENROUTER_MODEL` env var |

`OpenRouterClient` sends `reasoning: {"exclude": true}` by default so
reasoning-model prompts do not return private reasoning text in normal
chat-completion responses. If a provider still returns `content: null`
with a plaintext `message.reasoning`, the adapter falls back to that
field rather than treating the response as transiently empty.

## Run A — default verifier (`python_lint`)

The default `bug_fix_v2` verify suite is `python_lint`
([bug_fix_v2.py:62](../../src/meta_agent/core/orchestration/graphs/bug_fix_v2.py#L62)).
Run the test with no `verify_suite` override.

| metric | value |
|---|---|
| Task terminal state | `SUCCEEDED` |
| Steps (plan/act/observe loops) | 4 |
| Attempts (replan iterations) | 1 |
| Total tokens | 5,557 (prompt 5,183 / completion 374) |
| Files changed | `src/discount.py` |
| Diff stat | `5 ++++- — 1 file changed, 4 insertions(+), 1 deletion(-)` |
| Verifier suite | `python_lint` |
| Verifier passed | ✅ true (`ruff check` clean) |
| Wall clock | ~24s |
| Push | skipped (`no_token` — no git remote configured) |

**Read this carefully**: the verifier passed because `python_lint` only
runs `ruff check`. It does *not* run the failing pytest cases. So a
`SUCCEEDED + verifier_passed=True` outcome here does **not** prove the
fix is correct — it only proves the agent edited a file and didn't
break lint. To get a true red→green signal, we need Run B.

## Run B — strict verifier (`python_test`)

> Historical run: this captured the pre-fix infrastructure failure where
> the workspace image lacked `pytest`. After the verifier/runtime fix,
> `python_test` is the default suite and the rebuilt `meta-agent:local`
> image includes `pytest`.

Override the payload with `"verify_suite": "python_test"` so the verifier
actually invokes pytest.

| metric | value |
|---|---|
| Task terminal state | `SUCCEEDED` (harness contract) |
| Steps | 2 |
| Attempts | 2 (one replan triggered) |
| Total tokens | 2,556 |
| Files changed | `src/discount.py` |
| Diff stat | `4 +++- — 1 file changed, 3 insertions(+), 1 deletion(-)` |
| Verifier suite | `python_test` |
| Verifier passed | ❌ false |
| Verifier stderr | `/opt/venv/bin/python3: No module named pytest` |
| Wall clock | ~100s |
| Push | skipped (`verifier_failed`) |

**Finding**: the workspace container image (`meta-agent:local`, built from
[`Dockerfile`](../../Dockerfile)) installs deps via
`uv sync --frozen --no-dev`. `pytest` lives in the `dev` optional group
in [`pyproject.toml`](../../pyproject.toml) and is not in the runtime
image. So the `python_test` verifier can never pass for the current
workspace image, regardless of what the agent does.

The agent did do useful work — it edited `src/discount.py`, the diff
applied, the lint-step worked, replan triggered on the failed verifier
— but the verdict it received was an infrastructure error, not a
correctness signal.

## Run C — `deepseek/deepseek-v4-pro` + metered harness

Current baseline after fixing the verifier/runtime/metering gaps.

| metric | value |
|---|---|
| Command | `pytest tests/integration/test_bug_fix_v2_real_llm.py -m "integration and real_llm" -v -s` |
| Model requested | `deepseek/deepseek-v4-pro` |
| Model served | `deepseek/deepseek-v4-pro-20260423` |
| Task terminal state | `SUCCEEDED` |
| Files changed | `src/discount.py` |
| Verifier suite | `python_test` |
| Verifier passed | ✅ true |
| Host patch replay | ✅ true |
| Host pytest after replay | `5/5 passed, 0 failed` |
| LLM usage rows | 7 |
| Total recorded tokens | 14,082 |
| Total recorded cost | 17,082 micro-USD |
| Wall clock | ~81s |
| Push | skipped (`no_token` — no git remote configured) |

The resulting patch:

```diff
 if discount_percent < 0 or discount_percent > 100:
+    raise ValueError(f"discount_percent must be between 0 and 100, got {discount_percent}")
 return price - (price * discount_percent / 100)
```

---

## Findings (what the dogfood surfaced)

These are concrete and would not have come out of unit tests.

### F1. Default verifier is too weak for the "bug fix" framing
**Status: fixed.** `bug_fix_v2` now defaults to `python_test`, so bug-fix
success is gated by pytest rather than lint alone. Callers can still
override with `"verify_suite": "python_lint"` for lint-only smoke checks.

Original finding: `bug_fix_v2` defaulted to `python_lint`, so a
lint-clean change that didn't fix the failing test could still return
`verifier_passed=True`. For a product called "Bug Fix CLI Agent", the
natural default is to run the failing tests and accept only on green.
Lint remains useful as an optional additional gate, not as the default
correctness signal.

### F2. Workspace image lacks test runners
**Status: fixed.** `pytest` is now a runtime dependency, so the
`meta-agent:local` image built from `Dockerfile` includes the Python test
runner used by the `python_test` verifier. Rebuild the image before
rerunning this dogfood:

```bash
docker build -t meta-agent:local .
```

Original finding: the runtime stage of `Dockerfile` excluded dev deps,
so `pytest` was unavailable inside the workspace where the agent's
verifier executes. That made `python_test` fail for infrastructure
reasons regardless of the patch quality.

### F3. LLM cost is never written to `llm_usage_logs`
**Status: fixed for real dogfood.** The real-LLM harness now wraps the
client in `MeteredLLMClient` and passes the same `PgLLMUsageRepository`
to `WorkerLoop`, so terminal task output can include
`cost_by_step_kind`. Run C produced 7 usage rows with model identity,
prompt/completion/total tokens, OpenRouter `usage.cost` mapped to
`cost_usd_micros`, latency, prompt provenance, and `step_kind`.

Original finding: direct integration wiring used
`OpenRouterClient → RedactingLLMClient` and bypassed
`MeteredLLMClient`, leaving the usage table empty even though production
worker bootstrap already wires metering.

### F4. Reasoning models can't yet round-trip through the adapter
**Status: fixed for the current v4-pro dogfood path.** The OpenRouter
adapter now requests `reasoning.exclude=true` and falls back to
`message.reasoning` only when `content` is `null` and no tool call is
present. Run C completed against `deepseek/deepseek-v4-pro`.

Remaining limitation: `LLMUsage` records total cost and normal token
counts, but does not yet persist provider-specific reasoning-token and
cache-token subfields separately.

### F5. Agent's actual patch isn't retained for inspection
**Status: fixed.** `bug_fix_v2` now stores the pre-commit unified diff in
`result.output["patch"]`, alongside `diff_stat` and `files_changed`.

Original finding: after `workspace.cleaned`, the agent's branch / commit
/ worktree are all destroyed (the workspace is a separate `git clone`).
The task result row stored `diff_stat` and `commit_sha` but not the diff
itself, so a subtly wrong fix was hard to inspect after cleanup.

---

## What the test asserts (and does not)

It asserts only the **harness contract**:
- task reaches a terminal state (`SUCCEEDED` or `FAILED`)
- result row exists and carries the right `graph_id`
- structured fields (`files_changed`, `verifier_output`, `push_skip_reason`)
  are present

It does *not* assert the patch is correct. Agent-quality signals
(verifier pass/fail, post-fix pytest count, final file content) are
**printed** to test stdout for human inspection — a 0% baseline is
still useful as long as the harness itself works.

This is intentional: in a stochastic system, hard-asserting agent
quality flakes CI. The dogfood doc is where the quality signal lives.

## How to reproduce

```bash
# Build the workspace image once
docker build -t meta-agent:local .

# Set the key (or put it in <repo>/.env)
export OPENROUTER_API_KEY=sk-or-...

# Run
pytest tests/integration/test_bug_fix_v2_real_llm.py \
       -m "integration and real_llm" -v -s
```

`-s` is important so the diagnostic dump (audit-event timeline, final
file, verifier output) reaches your terminal.

## Where this goes next

The remaining dogfood follow-up is narrower now: persist
provider-specific reasoning-token / cache-token breakdowns if we need
finer cost attribution than total tokens + `usage.cost`.
