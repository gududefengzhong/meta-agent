"""Outbound webhook infra (Phase γ-B-2).

* :class:`WebhookFanout` — called by the worker right after an audit
  event lands; resolves matching subscriptions and writes one
  ``webhook_deliveries`` row per (event, subscription) pair. The
  fanout is best-effort: failures are caught and swallowed so the
  audit emission itself cannot be blocked by a webhook outage.
* :class:`WebhookDispatcher` — long-running background loop that
  claims pending deliveries, signs the payload, POSTs it, and moves
  rows through the lifecycle (``pending`` →
  ``dispatched`` / ``dead_letter``).
* :func:`compute_signature` — HMAC SHA-256 helper used on both
  signing and (where deployed) verifying sides.
"""

from meta_agent.infra.webhook.dispatcher import (
    WebhookDispatcher,
    WebhookDispatcherConfig,
    compute_next_attempt_at,
)
from meta_agent.infra.webhook.fanout import WebhookFanout
from meta_agent.infra.webhook.signing import (
    SIGNATURE_HEADER,
    SIGNATURE_PREFIX,
    compute_signature,
)

__all__ = [
    "SIGNATURE_HEADER",
    "SIGNATURE_PREFIX",
    "WebhookDispatcher",
    "WebhookDispatcherConfig",
    "WebhookFanout",
    "compute_next_attempt_at",
    "compute_signature",
]
