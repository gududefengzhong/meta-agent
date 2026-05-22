"""Prompt asset model.

A ``PromptAsset`` is a versioned, immutable LLM prompt template used by
graph nodes (system / user prompts). Per Phase β+ in
``docs/specs/AGENT_SPEC.md``, every LLM call must carry the
``prompt_id`` + ``version`` of the prompt that drove it so
``llm_usage_logs`` and ``audit_events`` can join calls back to the
exact text that produced them.

Identity:

* ``(prompt_id, version)`` is unique. New versions get monotonically
  larger integers; lower-numbered versions are never overwritten.
* ``content_hash`` is the lowercase hex SHA-256 of ``content`` and is
  recomputed on construction. It exists for change detection — when a
  seed run sees an existing ``prompt_id`` whose latest version has a
  different hash, it inserts a new version rather than mutating in
  place.

Scoping:

* ``tenant_id is None`` means "global / system" — visible to every
  tenant. A per-tenant override lives as a separate row with the same
  ``prompt_id`` and that tenant's id. Resolution (tenant override beats
  global) is the adapter's responsibility, not the model's.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field


def compute_content_hash(content: str) -> str:
    """Return the canonical lowercase hex SHA-256 of ``content``."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class PromptAsset(BaseModel):
    """An immutable versioned prompt template.

    The model is frozen and ``extra='forbid'`` so callers cannot smuggle
    untracked fields past audit / usage join logic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    version: int = Field(..., ge=1)
    tenant_id: str | None = Field(default=None, min_length=1)
    content: str = Field(..., min_length=1)
    description: str | None = Field(default=None)
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        return compute_content_hash(self.content)
