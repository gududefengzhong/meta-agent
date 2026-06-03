"""meta-agent CLI helper for the bug-fix API.

Distributed as ``python -m meta_agent.cli`` for now; a console-script
entry point can follow later if the surface stabilises.

Subcommands
===========
* ``submit`` — POST /v1/tasks, print the task_id
* ``tail`` — poll /v1/tasks/{id} until the task reaches a terminal state
* ``run`` — submit + tail in one call

Configuration
=============
* ``META_AGENT_API_URL`` (default ``http://localhost:8000``) — base URL
* ``META_AGENT_TOKEN`` — bearer token. Required by every command;
  unset → exit 2 with a clear error before any HTTP is attempted.

The CLI is intentionally thin: it is a development helper over the
REST API, not the primary product surface.
"""
