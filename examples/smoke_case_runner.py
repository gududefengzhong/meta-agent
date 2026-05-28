#!/usr/bin/env python3
"""Thin entrypoint for the smoke/eval runner."""

from __future__ import annotations

from meta_agent.evals.smoke_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
