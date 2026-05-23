"""Prompt / response redaction (Phase γ-D).

A small regex-based scanner that walks a string and replaces matches
of well-known secret / PII patterns with named placeholders. Used by
:class:`RedactingLLMClient` to scrub outbound prompts and inbound
responses before they reach any downstream layer (audit / metering /
OpenRouter / customer-visible output).

The module is intentionally standalone (no dependency on the LLM
layer) so other call sites — future audit redaction, response log
scrubbing, etc. — can use the same scanner.

Pattern coverage at v0:

* GitHub-style tokens (``ghp_``, ``gho_``, ``ghu_``, ``ghs_``, ``ghr_``)
* OpenAI-style API keys (``sk-...``)
* AWS access key ids (``AKIA...``, ``ASIA...``)
* JWT tokens (``eyJ...`` three-segment)
* RSA / EC private key PEM blocks
* Bearer / Basic ``Authorization`` header values
* Postgres / MySQL / MongoDB / Redis connection URIs with embedded
  credentials
* Email addresses (best-effort; common low-entropy false positive)

The list is deliberately conservative — high recall trumps high
precision on the way out (false positives are placeholder strings
that the LLM can still reason around). False negatives are the
problem we cannot recover from once the bytes have left the process.
"""

from meta_agent.infra.redaction.patterns import (
    BUILTIN_PATTERNS,
    RedactionPattern,
)
from meta_agent.infra.redaction.redactor import (
    RedactionReport,
    Redactor,
)

__all__ = [
    "BUILTIN_PATTERNS",
    "RedactionPattern",
    "RedactionReport",
    "Redactor",
]
