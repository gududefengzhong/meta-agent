"""Evaluation and dogfood helpers.

These modules are intentionally separate from the product-facing CLI and
runtime surfaces. They support smoke baselines, dogfood harnesses, and
other operator-driven evaluation flows.
"""

from meta_agent.evals.smoke_runner import main as smoke_runner_main

__all__ = ["smoke_runner_main"]
