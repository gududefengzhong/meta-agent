"""Regex-based secret / PII scrubber (Phase γ-D).

The :class:`Redactor` walks an input string, applies every
:class:`RedactionPattern` in order, and returns the rewritten text
plus a :class:`RedactionReport` summarising what was redacted (label
+ count). The report is the audit-friendly side: it never carries
the original bytes, only the labels + counts, so an audit consumer
can answer "did this prompt leak secrets?" without re-reading the
secret itself.

Design rules:

* Patterns apply in the fixed order they appear in the constructor
  argument. Most-specific first (PEM blocks, provider tokens) so an
  outer envelope pattern (``Authorization: Bearer …``) does not
  steal a match away from the inner secret-typing pattern.
* Each pattern is applied in a single pass against the **current**
  text — i.e. later patterns see the placeholders from earlier
  patterns, never the original bytes. That avoids double-counting
  the same secret under two different labels.
* The scanner never raises. Bad inputs (None, non-str) fall through
  unchanged so wrapping a redactor around an existing call site can
  never make it worse than calling without one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from meta_agent.infra.redaction.patterns import BUILTIN_PATTERNS, RedactionPattern


@dataclass(frozen=True, slots=True)
class RedactionReport:
    """What was redacted, by label.

    ``hits`` is a label → count mapping. Empty dict means "nothing
    matched". Sum of values = total replacements; can exceed the
    number of original secrets if the same secret matched multiple
    patterns (rare but possible).
    """

    hits: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.hits.values())

    @property
    def any_redacted(self) -> bool:
        return self.total > 0


class Redactor:
    """Apply a fixed pattern set to incoming text."""

    def __init__(
        self,
        patterns: Iterable[RedactionPattern] = BUILTIN_PATTERNS,
    ) -> None:
        self._patterns: tuple[RedactionPattern, ...] = tuple(patterns)

    def scrub(self, text: object) -> tuple[str, RedactionReport]:
        """Return ``(scrubbed_text, report)``.

        Non-string inputs pass through as ``str(text)`` (or ``""`` for
        ``None``) so the scanner is safe to call on opaque payloads
        without pre-checking.
        """

        if text is None:
            return "", RedactionReport()
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return text, RedactionReport()
        hits: dict[str, int] = {}
        current = text
        for pat in self._patterns:
            new, count = pat.pattern.subn(pat.placeholder, current)
            if count:
                hits[pat.label] = hits.get(pat.label, 0) + count
                current = new
        return current, RedactionReport(hits=hits)

    def scrub_str(self, text: object) -> str:
        """Convenience: drop the report when the caller only wants the text."""

        scrubbed, _ = self.scrub(text)
        return scrubbed
