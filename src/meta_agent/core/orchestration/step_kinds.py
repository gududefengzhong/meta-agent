"""Canonical ``step_kind`` vocabulary used by graphs and the LLM router.

The vocabulary is small on purpose — each value is a coarse bucket the
:class:`LLMRouter` policy can reason about. Graphs tag every outgoing
:class:`LLMRequest` with one of these values; new graph types add new
constants here when a genuinely new bucket appears (and only then).

Buckets:

* ``STEP_PLAN`` — high-level reasoning / decomposition. The
  ``shell_agent`` tool-use loop and ``bug_fix`` v1's planning node
  both fall here.
* ``STEP_EDIT`` — code-modification step (write whole files / unified
  diff). ``bug_fix`` v1's patch node uses it.
* ``STEP_REVIEW`` — code-review judgment (``code_review`` graph).
* ``STEP_CHAT`` — generic short-form chat (reserved for smoke / probe).
* ``STEP_OBSERVE`` — reserved for future observation-summary steps;
  no graph emits it today but it is part of the public vocabulary so
  router configs can pre-allocate a model slot.
"""

from __future__ import annotations

STEP_PLAN = "plan"
STEP_EDIT = "edit"
STEP_REVIEW = "review"
STEP_CHAT = "chat"
STEP_OBSERVE = "observe"

ALL_STEP_KINDS: tuple[str, ...] = (
    STEP_PLAN,
    STEP_EDIT,
    STEP_REVIEW,
    STEP_CHAT,
    STEP_OBSERVE,
)


__all__ = [
    "ALL_STEP_KINDS",
    "STEP_CHAT",
    "STEP_EDIT",
    "STEP_OBSERVE",
    "STEP_PLAN",
    "STEP_REVIEW",
]
