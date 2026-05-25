# baseline-v1 — first real-LLM run

The first run of `python -m eval.swebench run-agent` against a real LLM after
the harness was rebuilt under `docs/specs/EVAL_BASELINE.md`. Single instance,
single shot. Captures whatever the agent produced for future diff comparison.

This isn't a "good number" — it's a **real number** to compare future agent +
prompt + model changes against.

## Configuration

| Field | Value |
|---|---|
| Model | `deepseek/deepseek-chat` |
| Instance | `psf__requests-2317` (SWE-bench Lite test split) |
| Image arch | arm64 (cached locally) |
| max_steps | 20 |
| dataset_snapshot | `97078781bcc6` |
| prompt_version | `c6c02afc1db7` |
| harness_version | `0355e930ae39` |
| Wallclock | ~2:25 |

## Result

| | |
|---|---|
| `resolved` | **False** |
| `patch_applied` | True |
| `test_command_exit_code` | 4 (pytest collection failed) |
| FAIL_TO_PASS | 0/8 — all `missing` |
| PASS_TO_PASS | 0/133 — all `missing` |
| Agent steps | 8 |
| Agent patch | 1029 bytes |

## What actually happened

Agent ran for 8 steps, generated a 1029-byte patch in `requests/compat.py` that:
- **Did** correctly identify that `builtin_str(method)` needed to handle bytes
  by adding a `def builtin_str(s): if isinstance(s, bytes): return s.decode('ascii')`
- **But also deleted the entire `elif is_py3:` block**, removing the imports
  (`urlparse`, `cookielib`, `Morsel`, `StringIO`, `OrderedDict`, …) that the
  package needs on Python 3.

Result: package fails to import → pytest can't collect `test_requests.py` →
all selectors land as `missing`. Exit code 4 is pytest's "internal /
collection error", not a per-test failure.

The agent's diff is captured at `psf__requests-2317.agent.patch` for inspection.

## What this tells us (signal interpretation)

1. **Harness is sound.** ``patch_applied: True``, ``error: None`` — apply +
   extract + container path all work end-to-end against a real eval image
   with a real LLM-generated patch. (The previous attempt failed at the
   harness layer; this attempt fails purely at the agent layer, which is the
   right place to fail.)
2. **Minimal scaffold + deepseek-chat is not enough on this instance.** The
   model understood the problem (correct ``builtin_str`` rewrite) but didn't
   understand the cost of the surrounding deletions. Possible mitigations
   for the next baseline:
   - Tighter system prompt: "make the minimal change; do not delete
     surrounding code"
   - A "run tests / pytest collect" tool the agent can call before declaring
     done — caught at step 3 instead of declaring success at step 8
   - Higher-capacity model (claude-haiku / sonnet / gpt-4o-mini) for an
     A/B baseline
3. **Wallclock 2:25** is the LLM-side latency floor for this instance + this
   model. Real batch runs will be dominated by this, not by docker.

## How to reproduce

```bash
docker pull swebench/sweb.eval.arm64.psf_1776_requests-2317:latest
# Put OPENROUTER_API_KEY in <repo>/.env or export it
python -m eval.swebench run-agent psf__requests-2317 \
    --work-root /tmp/eval-runs \
    --model deepseek/deepseek-chat \
    --max-steps 20
```

## Files in this directory

* `psf__requests-2317.json` — the full `InstanceResult` JSON (with the
  four identity fields per Standard 2)
* `psf__requests-2317.agent.patch` — the agent's net diff against the
  post-test_patch HEAD (i.e. what `extract_patch` returned and what the
  container tried to apply)
* `README.md` — this file
