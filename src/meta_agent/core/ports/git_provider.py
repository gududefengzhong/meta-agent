"""GitProvider port: open or reuse pull requests on a remote git host.

The first L1 port that takes an external *write* action: a commit
produced by ``builtin.bug_fix`` only lives on a feature branch inside
the worker's worktree until ``builtin.auto_pr`` calls this port and
the adapter publishes the change as a real pull request.

Adapter contract (v1, minimum surface):

* Exactly one publish operation: :meth:`GitProvider.open_or_reuse_pr`.
  V1 models "one open PR per ``(tenant_id, repo_url, head_branch)``".
  If an existing open PR on that branch already points at the caller's
  ``head_commit_sha``, the adapter returns the same
  :class:`PullRequestRef` with ``action="reused"``. If an existing open
  PR on that branch points at a *different* head SHA, the adapter MUST
  fail the call as a caller-side contract violation: v1 has no
  "update the existing PR" surface, so silently rewriting or replacing
  upstream PRs is forbidden.
* The adapter MUST NOT log or echo credentials. Error messages
  surfaced to callers must already be redacted; see
  ``infra/workspace`` for the precedent.
* Secrets and access tokens are an adapter concern. The port is
  intentionally credential-blind so the orchestration layer never
  carries provider keys.

Future additions (out of v1 scope): ``update_pr_body``, ``add_comment``,
``find_existing_pr``, ``list_prs_for_branch``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meta_agent.core.domain.errors import AgentError, ErrorCategory

PullRequestAction = Literal["created", "reused"]


class PullRequestRef(BaseModel):
    """Provider-side result of an :meth:`GitProvider.open_or_reuse_pr` call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(..., min_length=1)
    """Adapter name, e.g. ``"fake"`` or ``"github"``."""

    pr_id: str = Field(..., min_length=1)
    """Stable provider-side PR identifier."""

    url: str = Field(..., min_length=1)
    """Canonical URL pointing at the PR; opaque to the orchestration layer."""

    action: PullRequestAction
    """``"created"`` on the first publish for a key; ``"reused"`` thereafter."""

    head_branch: str = Field(..., min_length=1)
    base_ref: str = Field(..., min_length=1)
    head_commit_sha: str = Field(..., min_length=1)


class GitProviderError(AgentError):
    """Base class for adapter-raised git-provider errors.

    Default category is :class:`ErrorCategory.EXTERNAL`; transient
    subclasses override it so callers can decide retry policy without
    parsing the exception class.
    """

    category = ErrorCategory.EXTERNAL


class GitProviderTransientError(GitProviderError):
    """Recoverable failure (5xx, transient network error, secondary rate limit)."""

    category = ErrorCategory.TRANSIENT


class GitProviderAuthError(GitProviderError):
    """401/403 from the upstream. Indicates a missing or revoked token."""

    category = ErrorCategory.PERMISSION


class GitProviderInvalidRequestError(GitProviderError):
    """4xx caused by malformed/forbidden caller input. Not retryable."""

    category = ErrorCategory.VALIDATION


class GitProvider(ABC):
    """Adapter contract: publish a feature-branch commit as a pull request."""

    @abstractmethod
    async def open_or_reuse_pr(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        repo_url: str,
        base_ref: str,
        head_branch: str,
        head_commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        """Open a new PR or reuse the existing one for this branch+commit.

        V1 branch contract:

        * No open PR on ``(tenant_id, repo_url, head_branch)`` →
          create a new PR.
        * Existing open PR on that branch with the same
          ``head_commit_sha`` → return the same
          :class:`PullRequestRef` with ``action="reused"``.
        * Existing open PR on that branch with a different head SHA →
          raise :class:`GitProviderInvalidRequestError` until a future
          ``update_pr_body`` / ``update_pr_head`` surface exists.

        Raises:
            GitProviderAuthError: missing/revoked credentials.
            GitProviderInvalidRequestError: malformed ``repo_url`` /
                non-existent ``base_ref`` / forbidden cross-fork push.
            GitProviderTransientError: timeout / 5xx / secondary rate
                limit; safe to retry with backoff.
            GitProviderError: any other adapter-specific failure.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any connection pool. Safe to call multiple times."""
