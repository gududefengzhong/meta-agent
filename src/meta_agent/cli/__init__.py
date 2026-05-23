"""meta-agent CLI v0 (Phase δ-1 Track A).

The first client that exercises the streaming + audit pipeline
shipped in PRs #33–#35. Distributed as ``python -m meta_agent.cli``
for now; a console-script entry point follows once the surface
stabilises.

Subcommands
===========
* ``submit`` — POST /v1/tasks, print the task_id
* ``tail`` — connect to /v1/tasks/{id}/llm-stream and
  /v1/tasks/{id}/events, multiplex to stdout / stderr until the
  task reaches a terminal state
* ``run`` — submit + tail in one call (the common interactive flow)

Configuration
=============
* ``META_AGENT_API_URL`` (default ``http://localhost:8000``) — base URL
* ``META_AGENT_TOKEN`` — bearer token. Required by every command;
  unset → exit 2 with a clear error before any HTTP is attempted.

The CLI is intentionally thin: no plan-mode / diff-review / inline
permissions yet. Those are follow-up PRs that need backend support
landing first.
"""
