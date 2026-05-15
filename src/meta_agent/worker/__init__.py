"""Task worker runtime.

A worker is a long-lived process that pulls envelopes off the task
queue, runs the corresponding orchestration graph, and persists every
step as a checkpoint and audit record. The runner is queue-shaped via
the :class:`DeliveryStream` protocol so the same control flow can be
exercised against a real :mod:`meta_agent.infra.queue` consumer or an
in-memory fake.
"""

from meta_agent.worker.runner import DeliveryStream, WorkerConfig, WorkerLoop

__all__ = ["DeliveryStream", "WorkerConfig", "WorkerLoop"]
