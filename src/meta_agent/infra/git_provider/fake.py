"""In-memory ``GitProvider`` for tests and the auto_pr v1 milestone.

``FakeGitProvider`` does not talk to any remote: it keeps an in-process
dictionary keyed by ``(tenant_id, repo_url, head_branch)`` and emits
deterministic ``fake://`` URLs. It exists so the orchestration core
can land a real ``builtin.auto_pr`` graph and a real task contract
before a network-touching GitHub adapter is wired up.

Cross-tenant invariant: the dedup table is keyed on ``tenant_id``, so
two tenants pushing to the same ``(repo_url, head_branch)`` see
independent fake PRs. This mirrors the L0 isolation requirement; the
future GitHub adapter will inherit the same key shape.

Reuse semantics (v1):

* Same ``(tenant_id, repo_url, head_branch)`` + same
  ``head_commit_sha`` → ``action="reused"``, same ``pr_id`` / ``url``.
* Same ``(tenant_id, repo_url, head_branch)`` + different
  ``head_commit_sha`` → ``action="created"``, new ``pr_id``; the
  previous entry for the key is replaced. Modelling force-push vs
  new PR semantics is a real-adapter concern and is deferred.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from meta_agent.core.ports.git_provider import GitProvider, PullRequestRef


class FakeGitProvider(GitProvider):
    """Deterministic in-memory ``GitProvider`` for unit tests and smoke."""

    PROVIDER_NAME = "fake"

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._id_factory = id_factory or (lambda: f"pr-{uuid.uuid4().hex[:12]}")
        self._open: dict[tuple[str, str, str], PullRequestRef] = {}
        self.calls: list[dict[str, str]] = []
        self.closed = False

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
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "trace_id": trace_id,
                "repo_url": repo_url,
                "base_ref": base_ref,
                "head_branch": head_branch,
                "head_commit_sha": head_commit_sha,
                "title": title,
                "body": body,
            }
        )
        key = (tenant_id, repo_url, head_branch)
        existing = self._open.get(key)
        if existing is not None and existing.head_commit_sha == head_commit_sha:
            return existing.model_copy(update={"action": "reused"})
        pr_id = self._id_factory()
        ref = PullRequestRef(
            provider=self.PROVIDER_NAME,
            pr_id=pr_id,
            url=f"fake://{self.PROVIDER_NAME}/{tenant_id}/{pr_id}",
            action="created",
            head_branch=head_branch,
            base_ref=base_ref,
            head_commit_sha=head_commit_sha,
        )
        self._open[key] = ref
        return ref

    async def close(self) -> None:
        self.closed = True
