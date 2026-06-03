"""Built-in prompt seeds and the ``ensure_seeded`` helper.

Each :class:`PromptSeed` declares one ``prompt_id`` and its current
canonical content. ``ensure_seeded`` walks the seed list at worker
boot, compares each seed's content hash against the latest registered
version, and:

* registers ``version=1`` if the ``prompt_id`` is brand new;
* registers ``version=N+1`` if the latest version's hash differs from
  the seed (i.e. someone updated the seed in code);
* does nothing if the latest version already matches.

This lets prompt evolution happen via code commits + redeploy: the
seed in code is the source of truth at deploy time, but operators
can still hot-patch a single tenant's row via direct DB writes
between deploys, and the cache TTL plus the seed-respects-current-
latest behavior keeps those overrides safe.

Parameter substitution uses :class:`string.Template` (``$name`` syntax)
because several prompts contain JSON-schema snippets with literal
braces; using ``str.format`` here would require escaping every brace
in the seed text and breaking the WYSIWYG property between seed and
final prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from meta_agent.core.domain.prompt_asset import PromptAsset, compute_content_hash
from meta_agent.core.ports.prompt_registry import PromptRegistry


@dataclass(frozen=True)
class PromptSeed:
    """Canonical declaration of a prompt at seed time."""

    prompt_id: str
    description: str
    content: str


# ---------------------------------------------------------------------------
# Built-in seeds. Migrated verbatim (modulo $name placeholders) from the
# inline strings that used to live in the graph files. When you change a
# seed here, ``ensure_seeded`` will register a new version on next boot;
# the previous versions remain in the table.
# ---------------------------------------------------------------------------


_BUG_FIX_PLAN_SYSTEM = (
    "You are a code repair agent. Read the issue and the listed files, "
    "then write a concise plan (at most 6 lines) describing the minimal "
    "change required to fix the bug. Do not output code yet; only the plan."
)


_BUG_FIX_PATCH_SYSTEM = (
    "You are a code patcher. Apply the provided plan to fix the issue. "
    'Return ONLY JSON of the form {"files":[{"path":"<rel>","content":"<full>"}]}. '
    "You may only modify files in this allow-list: [$allow_list]. "
    "At most $max_files files; each file at most $max_file_bytes bytes. "
    "Emit the FULL new content of each modified file, not a diff."
)


_BUG_FIX_V2_SYSTEM = (
    "You are a code repair agent working inside a dedicated task workspace. "
    "Use the available tools to inspect files, modify code, and optionally run "
    "safe verification commands. Only edit files in this allow-list: "
    "[$allow_list]. Prefer the smallest viable fix. When the change is ready, "
    "stop calling tools and reply with a one-line summary of what you changed."
)


_CODE_REVIEW_SYSTEM = (
    "You are a senior code reviewer. Read the unified diff and any "
    "supplied context, then return ONLY a single JSON object matching "
    "this schema (no prose, no fences):\n"
    '{"verdict":"approve"|"request_changes"|"comment",'
    '"summary":"<one-paragraph reviewer summary>",'
    '"findings":[{"category":"bug"|"regression"|"security"|"test_gap"|"style"|"other",'
    '"severity":"blocker"|"major"|"minor"|"info",'
    '"file":"<repo-relative path or null>",'
    '"line_range":"<e.g. 12-18 or null>",'
    '"message":"<what is wrong and why>",'
    '"suggested_action":"<what to change or null>"}],'
    '"confidence":<float 0.0-1.0>}\n'
    "Emit at most $max_findings findings. Prefer fewer, higher-signal "
    "findings over volume. Focus on behavior regressions, missing "
    "tests, security risks and obvious bugs."
)


BUILTIN_PROMPT_SEEDS: tuple[PromptSeed, ...] = (
    PromptSeed(
        prompt_id="bug_fix.plan.system",
        description="System framing for the bug_fix v1 plan node.",
        content=_BUG_FIX_PLAN_SYSTEM,
    ),
    PromptSeed(
        prompt_id="bug_fix.patch.system",
        description=(
            "System framing for the bug_fix v1 patch node. Placeholders: "
            "$allow_list, $max_files, $max_file_bytes."
        ),
        content=_BUG_FIX_PATCH_SYSTEM,
    ),
    PromptSeed(
        prompt_id="bug_fix.system",
        description=(
            "System framing for the bug_fix graph (tool-use loop). Placeholder: $allow_list."
        ),
        content=_BUG_FIX_V2_SYSTEM,
    ),
    PromptSeed(
        prompt_id="code_review.system",
        description=(
            "System framing for the code_review graph; produces the "
            "structured-JSON verdict. Placeholder: $max_findings."
        ),
        content=_CODE_REVIEW_SYSTEM,
    ),
)


async def ensure_seeded(
    registry: PromptRegistry,
    *,
    seeds: tuple[PromptSeed, ...] = BUILTIN_PROMPT_SEEDS,
    now: datetime | None = None,
) -> tuple[PromptAsset, ...]:
    """Reconcile each seed against the registry.

    Returns the assets that now represent the latest version of each
    seed (whether registered fresh in this call or already present).
    Idempotent — calling twice with the same seeds is a no-op.
    """

    materialised: list[PromptAsset] = []
    timestamp = now if now is not None else datetime.now(UTC)
    for seed in seeds:
        current_version = await registry.latest_version(seed.prompt_id, tenant_id=None)
        next_version = 1 if current_version is None else current_version + 1
        seed_hash = compute_content_hash(seed.content)
        if current_version is not None:
            existing = await registry.fetch_or_none(
                seed.prompt_id, version=current_version, tenant_id=None
            )
            if existing is not None and existing.content_hash == seed_hash:
                materialised.append(existing)
                continue
        asset = PromptAsset(
            prompt_id=seed.prompt_id,
            version=next_version,
            tenant_id=None,
            content=seed.content,
            description=seed.description,
            created_at=timestamp,
        )
        await registry.register(asset)
        materialised.append(asset)
    return tuple(materialised)
