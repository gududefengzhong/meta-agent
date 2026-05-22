"""Tool / capability port surfaces for the Phase β agent loop.

Phase β introduces a generic ``plan → tool_call → observe → loop``
control flow. The orchestration core must stay infrastructure-blind,
so every operation the loop can take is fronted by a port defined in
this module. Concrete adapters (local-workspace filesystem, container
shell, ...) live under :mod:`meta_agent.infra.tools`.

Two layers cohabit here:

* :class:`ToolSpec` / :class:`ToolCall` / :class:`ToolResult` are the
  wire-shape the LLM sees: JSON-schema declarations, opaque arguments,
  bounded text observations. The shape is provider-agnostic but aligns
  with OpenAI / Anthropic tool-use conventions so adapters can
  transcode without re-modelling.
* :class:`FileSystemTool` / :class:`EditTool` (and later
  ``ShellTool`` / ``TestTool``) are typed capability ABCs: each
  operation is a regular coroutine with explicit parameters. The
  ``infra.tools`` layer exposes small adapter shims that translate
  ``ToolCall(name, arguments)`` into the right typed invocation.

Error taxonomy reuses :class:`AgentError`. The executor (capabilities
layer) catches every :class:`ToolError` and surfaces it to the agent
loop as a :class:`ToolResult` with ``is_error=True``; unexpected
exceptions propagate up so the graph node can decide on hard failure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from meta_agent.core.domain.errors import AgentError, ErrorCategory


class ToolCategory(StrEnum):
    """Coarse grouping of tools.

    Drives policy decisions (e.g. only ``EDIT`` tools require a
    writable workspace) and observability tagging. Adapters MUST pick
    the most specific matching category.
    """

    FILESYSTEM = "filesystem"
    EDIT = "edit"
    SHELL = "shell"
    TEST = "test"
    WEB = "web"
    """Outbound HTTP fetch + searchable doc / knowledge-base access.

    Phase β+ adds two ``WEB`` tools: ``web_fetch`` (single URL → text)
    and ``doc_search`` (query → ranked snippets). Both go through the
    α-phase safety shell (rate limit / circuit breaker / per-tool
    accounting) — no tool may bypass that layer.
    """


class ToolSpec(BaseModel):
    """Wire-shape description of a tool the LLM can call.

    ``parameters`` is a JSON Schema fragment (Draft 7-compatible);
    adapters forward it verbatim to the upstream provider. Kept as a
    plain ``dict[str, Any]`` to avoid re-modelling JSON Schema; callers
    treating ToolSpec as immutable must not mutate the dict in place.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(..., min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    category: ToolCategory


class ToolCall(BaseModel):
    """One tool invocation request emitted by the LLM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1)
    """Provider-supplied call id; echoed back so the LLM correlates result-to-call."""

    name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Bounded text observation returned from a tool execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    content: str
    is_error: bool = False
    truncated: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)


class ToolContext(BaseModel):
    """Per-call context: identity + workspace handle + output bounds.

    Built by the orchestration layer from the active ``TaskRunState``
    plus the current ``Workspace``. Tools that do not need a workspace
    (a future ``ShellTool`` against an ephemeral container) treat
    ``workspace_path`` as ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    workspace_path: Path | None = None
    output_byte_cap: int = Field(default=65536, gt=0)


class ToolError(AgentError):
    """Base class for adapter-raised tool errors. Default ``EXTERNAL``."""

    category = ErrorCategory.EXTERNAL


class ToolValidationError(ToolError):
    """Caller-side: arguments missing / out of range / schema-incompatible."""

    category = ErrorCategory.VALIDATION


class ToolPermissionError(ToolError):
    """Caller-side: tool access denied or path resolved outside the workspace."""

    category = ErrorCategory.PERMISSION


class ToolExecutionError(ToolError):
    """Adapter-side: tool ran but the underlying operation failed."""

    category = ErrorCategory.EXTERNAL


class ToolNotFoundError(ToolError):
    """The requested tool name is not registered."""

    category = ErrorCategory.VALIDATION


class GrepHit(BaseModel):
    """A single regex match returned by :meth:`FileSystemTool.grep`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1)
    line_no: int = Field(..., gt=0)
    line: str


class EditOutcome(BaseModel):
    """Summary of an applied edit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files_changed: tuple[str, ...] = Field(default_factory=tuple)
    bytes_written: int = Field(default=0, ge=0)


class ShellOutcome(BaseModel):
    """Summary of a shell command execution inside the workspace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...] = Field(default_factory=tuple)
    exit_code: int = Field(default=0, ge=0)
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class TestOutcome(BaseModel):
    """Summary of a deterministic test-suite execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite: str = Field(..., min_length=1)
    argv: tuple[str, ...] = Field(default_factory=tuple)
    exit_code: int = Field(default=0, ge=0)
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class FileSystemTool(ABC):
    """Read-only view of a per-task workspace tree.

    Implementations MUST reject any ``path`` that resolves outside
    ``ctx.workspace_path`` (raise :class:`ToolPermissionError`) and
    MUST bound text payloads by ``ctx.output_byte_cap``.
    """

    @abstractmethod
    async def read(
        self,
        ctx: ToolContext,
        *,
        path: str,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> str:
        """Return a UTF-8 slice of ``path`` starting at byte ``offset``."""

    @abstractmethod
    async def list_dir(
        self,
        ctx: ToolContext,
        *,
        path: str,
        recursive: bool = False,
        max_entries: int = 1000,
    ) -> tuple[str, ...]:
        """Return entry names under ``path`` (relative to the workspace root)."""

    @abstractmethod
    async def grep(
        self,
        ctx: ToolContext,
        *,
        pattern: str,
        path_globs: tuple[str, ...] = ("**/*",),
        max_matches: int = 200,
    ) -> tuple[GrepHit, ...]:
        """Search ``pattern`` (regex) across files matching ``path_globs``."""


class EditTool(ABC):
    """Writable surface for the per-task workspace.

    Implementations MUST reject paths outside ``ctx.workspace_path``
    and MUST surface non-zero subprocess exits or partial writes as
    :class:`ToolExecutionError` rather than silent no-ops.
    """

    @abstractmethod
    async def write(
        self,
        ctx: ToolContext,
        *,
        path: str,
        content: str,
    ) -> EditOutcome:
        """Overwrite ``path`` with ``content`` (UTF-8). Creates parent dirs."""

    @abstractmethod
    async def patch_apply(
        self,
        ctx: ToolContext,
        *,
        unified_diff: str,
    ) -> EditOutcome:
        """Apply a unified diff against the workspace root."""


class ShellTool(ABC):
    """Run allow-listed commands in the per-task workspace.

    Implementations MUST execute without invoking a shell, bind the
    current working directory to ``ctx.workspace_path`` when present,
    and reject commands outside the adapter's allow-list with
    :class:`ToolPermissionError`.

    Non-zero exits are regular outcomes, not exceptional control flow:
    agent loops often need the stderr/exit_code as an observation. The
    adapter raises :class:`ToolExecutionError` only for launch-time or
    timeout failures.
    """

    @abstractmethod
    async def run(
        self,
        ctx: ToolContext,
        *,
        argv: tuple[str, ...],
        timeout_seconds: float | None = None,
    ) -> ShellOutcome:
        """Run ``argv`` and return stdout/stderr plus the exit status."""


class TestTool(ABC):
    """Run an allow-listed verification suite inside the workspace.

    ``suite`` is a stable product identifier (for example
    ``python_lint`` or ``typescript_typecheck``), not an arbitrary shell
    command. Implementations map the suite to deterministic argv and
    may optionally scope it to ``targets`` under ``ctx.workspace_path``.

    Non-zero exits are observations, not exceptional control flow.
    Adapters raise :class:`ToolExecutionError` only for launch-time or
    timeout failures.
    """

    @abstractmethod
    async def run(
        self,
        ctx: ToolContext,
        *,
        suite: str,
        targets: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
    ) -> TestOutcome:
        """Run ``suite`` against ``targets`` within the workspace."""


class WebFetchOutcome(BaseModel):
    """Result of a single :class:`WebFetchTool.fetch` call.

    Adapters MUST populate ``content_type`` and ``final_url`` even on
    success — callers (and the LLM observing the tool result) often
    need the redirect target to make sense of the response. ``status``
    is the HTTP status code; non-2xx outcomes still return normally
    (with ``content`` carrying whatever the upstream sent) so the
    agent loop can reason about them, matching the
    ``ShellTool`` / ``TestTool`` convention.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    final_url: str = Field(..., min_length=1)
    status: int = Field(..., ge=100, le=599)
    content_type: str
    content: str
    truncated: bool = False
    bytes_received: int = Field(..., ge=0)


class WebFetchTool(ABC):
    """Outbound HTTP GET against a vetted domain allow-list.

    The adapter is the single chokepoint for outbound HTTP from agent
    loops: it enforces the domain allow-list, applies a size cap, and
    raises :class:`ToolPermissionError` when the URL falls outside the
    allow-list. Network errors and timeouts surface as
    :class:`ToolExecutionError`; non-2xx HTTP responses are *not*
    errors — they return a populated :class:`WebFetchOutcome` so the
    agent loop can inspect the status.

    Binary content (anything whose ``Content-Type`` is not text-shaped)
    MUST be refused with :class:`ToolValidationError`; the LLM
    consumes UTF-8 text only and silently base64-encoding bytes would
    hide the failure.
    """

    @abstractmethod
    async def fetch(
        self,
        ctx: ToolContext,
        *,
        url: str,
        timeout_seconds: float | None = None,
    ) -> WebFetchOutcome:
        """Fetch ``url`` and return its decoded body bounded by ``ctx.output_byte_cap``."""


class DocHit(BaseModel):
    """A single result returned by :class:`DocSearchTool.search`.

    ``source_uri`` is opaque to the LLM but stable across calls —
    callers re-fetch the full document by passing it back through
    ``WebFetchTool`` or an adapter-specific resolver. ``score`` is
    adapter-internal (cosine distance, BM25, keyword overlap) and is
    only useful for ordering hits within a single search response.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_uri: str = Field(..., min_length=1)
    title: str
    snippet: str
    score: float = Field(..., ge=0.0)


class DocSearchTool(ABC):
    """Searchable knowledge-base surface.

    The default Phase β+ adapter is in-memory keyword-scored, but the
    Port stays narrow so future adapters (OSS / COS / vendor doc
    APIs) can drop in without touching graph code. Implementations
    MUST clamp ``limit`` to a sensible upper bound (the in-memory
    adapter uses 20) and MUST return an empty tuple — never raise —
    when the query is well-formed but matches nothing.
    """

    @abstractmethod
    async def search(
        self,
        ctx: ToolContext,
        *,
        query: str,
        limit: int = 5,
    ) -> tuple[DocHit, ...]:
        """Rank documents against ``query`` and return up to ``limit`` hits."""
