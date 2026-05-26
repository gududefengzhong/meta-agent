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
| Default model | `deepseek/deepseek-chat-v3-0324` |
| Stack | `OpenRouterClient` → `RedactingLLMClient` (PII/secret scrub on prompts) |
| Configurable via | `OPENROUTER_MODEL` env var |

> **Why not `deepseek/deepseek-v4-pro`?** v4-pro is a reasoning model — its
> OpenRouter response has `content: null` with the chain-of-thought in a
> separate `reasoning` field. Our adapter currently treats empty content
> as `LLMTransientError`. Supporting reasoning models cleanly needs
> adapter changes (reasoning→content fallback, larger max_tokens budget,
> separate token accounting for reasoning vs answer). Tracked as a
> follow-up; for now we stay on v3-0324 which is the same vendor at a
> similar capability tier and works through the existing pipeline.

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

---

## Findings (what the dogfood surfaced)

These are concrete and would not have come out of unit tests.

### F1. Default verifier is too weak for the "bug fix" framing
`bug_fix_v2` defaults to `python_lint`. A lint-clean change that doesn't
fix the failing test will still return `verifier_passed=True`. For a
product called "Bug Fix CLI Agent" the natural default should be: **run
the failing tests, accept only on green**. Lint should be an additional
gate, not the gate.

**Implication**: change the default suite to `python_test` (or compose
`python_lint && python_test`), and accept the cost of needing pytest in
the workspace.

### F2. Workspace image lacks test runners
The runtime stage of `Dockerfile` excludes dev deps, so pytest /
typescript test runner are not available inside the workspace where the
agent's verifier executes. The current default (`python_lint`) is a
silent workaround.

**Implication**: either ship a dedicated workspace image with
`pytest + ruff + tsc + vitest` (the cross-product the verifier suites
declare in [`local_workspace.py`](../../src/meta_agent/infra/tools/local_workspace.py)
and [`docker_workspace.py`](../../src/meta_agent/infra/tools/docker_workspace.py)),
or document explicitly that `python_test` only works when the workspace
image extends the base.

### F3. LLM cost is never written to `llm_usage_logs`
Both runs left the table empty. The dogfood wires
`OpenRouterClient → RedactingLLMClient` directly; `MeteredLLMClient`
(which writes the row) is not in the chain. Same is true of the smoke
test
([`test_bug_fix_v2_docker_smoke.py`](../../tests/integration/test_bug_fix_v2_docker_smoke.py)) —
it relies on the audit-event payload for cost visibility, not on the
usage table.

**Implication**: either compose `MeteredLLMClient` in
`worker.bootstrap.build_llm_client` (so all real runs are metered by
construction) or document that the usage table only fills via the
top-level worker boot path and is empty under direct unit/integration
wiring. We currently rely on parser-on-audit-events, which works but is
implicit.

### F4. Reasoning models can't yet round-trip through the adapter
Setting `OPENROUTER_MODEL=deepseek/deepseek-v4-pro` raises
`LLMTransientError("empty content from provider")` because the response
content is `null` and the answer lives in `reasoning`. The adapter
needs to: (a) prefer `content` if present, else fall back to
`reasoning`, (b) account for reasoning tokens separately in
`LLMUsage`, (c) raise larger default `max_tokens` budgets since
reasoning consumes them.

**Implication**: a small adapter change unlocks the entire reasoning-model
tier; until then we are confined to non-reasoning chat models.

### F5. Agent's actual patch isn't retained for inspection
After `workspace.cleaned`, the agent's branch / commit / worktree are
all destroyed (the workspace is a separate `git clone`). The task
result row stores `diff_stat` and `commit_sha` but **not the diff
itself**. So if the agent succeeds but its fix is subtly wrong, we have
no record of what it actually wrote.

**Implication**: add the unified diff (`git diff base..HEAD`) into the
`result.output` so the agent's work is auditable after cleanup. Cheap
fix, big eval-loop dividend.

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

The five findings above prioritise as follows:

1. **F5** (capture the diff) — 1-line fix, highest signal-per-byte
2. **F1 + F2** (real verifier) — change the default + extend workspace image
3. **F3** (metering) — wire `MeteredLLMClient` in bootstrap so all real runs are costed
4. **F4** (reasoning models) — small adapter PR, unlocks v4-pro tier

None of these are needed for the existing bug_fix_v2 surface to keep
working — they are the next iteration's evaluation infrastructure.
