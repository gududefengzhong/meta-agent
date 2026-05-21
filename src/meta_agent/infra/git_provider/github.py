"""GitHub HTTP adapter for the :class:`GitProvider` port.

Implements the v1 minimal surface — :meth:`open_or_reuse_pr` — against
the GitHub REST API (``GET /repos/{o}/{r}/pulls`` for search,
``POST /repos/{o}/{r}/pulls`` for create). Compatible with both
github.com and GitHub Enterprise Server via
:attr:`GitHubGitProviderConfig.base_url`.

Idempotency: the adapter searches for an open PR on
``{owner}:{head_branch}`` and, when present, only reuses it if its
``head.sha`` matches the caller's ``head_commit_sha``. A mismatched
existing PR maps to :class:`GitProviderInvalidRequestError` — v1 has no
``update_pr_body`` surface, so silently rewriting upstream PRs is not
on the table.

Error mapping (status → exception):

* 401 → :class:`GitProviderAuthError`
* 403 with secondary-rate-limit markers → :class:`GitProviderTransientError`
* other 403 → :class:`GitProviderAuthError`
* 422 / 404 / 400 → :class:`GitProviderInvalidRequestError`
* 429 / 5xx / transport → :class:`GitProviderTransientError` (retried)
* anything else → :class:`GitProviderError`

Secret handling: ``token`` lives on the client headers; the adapter
NEVER logs or echoes it. Bodies are not logged. Outbound exception
messages reference only status codes and call sites, not response
bodies (those may contain caller-supplied data echoed back).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from meta_agent.core.ports.git_provider import (
    GitProvider,
    GitProviderAuthError,
    GitProviderError,
    GitProviderInvalidRequestError,
    GitProviderTransientError,
    PullRequestRef,
)
from meta_agent.infra.git_provider.github_config import GitHubGitProviderConfig

logger = logging.getLogger(__name__)

_HTTPS_REPO_RE = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$")
_SSH_REPO_RE = re.compile(r"^git@[^:]+:([^/]+)/([^/]+?)(?:\.git)?$")


def _parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Parse ``owner`` and ``repo`` from an HTTPS or SSH GitHub URL.

    Accepts ``https://github.com/o/r``, ``https://host/o/r.git``, and
    ``git@host:o/r.git``. Raises :class:`GitProviderInvalidRequestError`
    when neither shape matches; v1 deliberately does not try harder
    (path-with-subgroup style, query strings, etc.).
    """
    for pattern in (_HTTPS_REPO_RE, _SSH_REPO_RE):
        m = pattern.match(repo_url)
        if m is not None:
            owner, repo = m.group(1), m.group(2)
            if owner and repo:
                return owner, repo
    raise GitProviderInvalidRequestError(f"unsupported repo_url shape: {repo_url!r}")


class GitHubGitProvider(GitProvider):
    """``GitProvider`` adapter targeting the GitHub REST API."""

    PROVIDER_NAME = "github"

    def __init__(
        self,
        config: GitHubGitProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = None,
    ) -> None:
        if not config.token:
            raise ValueError("GitHubGitProviderConfig.token is required")
        self._config = config
        self._sleep = sleep if sleep is not None else asyncio.sleep
        headers = {
            "Authorization": f"Bearer {config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": config.user_agent,
        }
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
        )

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
        owner, repo = _parse_repo_url(repo_url)
        existing = await self._search_open_pr(owner=owner, repo=repo, head_branch=head_branch)
        if existing is not None:
            existing_sha = existing.get("head", {}).get("sha")
            if isinstance(existing_sha, str) and existing_sha == head_commit_sha:
                return self._build_ref(
                    payload=existing,
                    action="reused",
                    base_ref=base_ref,
                    head_branch=head_branch,
                    head_commit_sha=head_commit_sha,
                )
            raise GitProviderInvalidRequestError(
                f"open PR {existing.get('number')!r} on {owner}/{repo}:{head_branch} "
                f"points at a different head sha; v1 does not update upstream PRs"
            )
        created = await self._create_pr(
            owner=owner,
            repo=repo,
            base_ref=base_ref,
            head_branch=head_branch,
            title=title,
            body=body,
        )
        return self._build_ref(
            payload=created,
            action="created",
            base_ref=base_ref,
            head_branch=head_branch,
            head_commit_sha=head_commit_sha,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _build_ref(
        self,
        *,
        payload: dict[str, Any],
        action: str,
        base_ref: str,
        head_branch: str,
        head_commit_sha: str,
    ) -> PullRequestRef:
        number = payload.get("number")
        url = payload.get("html_url")
        if not isinstance(number, int) or not isinstance(url, str) or not url:
            raise GitProviderError("github response missing required PR identifier fields")
        return PullRequestRef(
            provider=self.PROVIDER_NAME,
            pr_id=str(number),
            url=url,
            action=action,
            head_branch=head_branch,
            base_ref=base_ref,
            head_commit_sha=head_commit_sha,
        )

    async def _search_open_pr(
        self, *, owner: str, repo: str, head_branch: str
    ) -> dict[str, Any] | None:
        params = {
            "head": f"{owner}:{head_branch}",
            "state": "open",
            "per_page": "1",
        }
        response = await self._send_with_retry(
            "GET", f"/repos/{owner}/{repo}/pulls", params=params, json=None
        )
        body = self._decode_json(response)
        if not isinstance(body, list):
            raise GitProviderError("github pulls list response is not an array")
        if not body:
            return None
        first = body[0]
        if not isinstance(first, dict):
            raise GitProviderError("github pulls list element is not an object")
        return first

    async def _create_pr(
        self,
        *,
        owner: str,
        repo: str,
        base_ref: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        payload = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_ref,
        }
        response = await self._send_with_retry(
            "POST", f"/repos/{owner}/{repo}/pulls", params=None, json=payload
        )
        decoded = self._decode_json(response)
        if not isinstance(decoded, dict):
            raise GitProviderError("github create-pr response is not an object")
        return decoded

    async def _send_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        """Send a request with bounded retry for transient categories.

        Transport errors, 5xx and 429 are retried up to ``max_retries``
        times with exponential backoff. ``Retry-After`` (seconds) on
        429 is honoured but clamped by ``max_backoff_seconds`` so a
        hostile upstream cannot pin the worker.
        """
        attempts = self._config.max_retries + 1
        last_error: GitProviderError | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = GitProviderTransientError(f"github transport: {type(exc).__name__}")
                logger.warning(
                    "git_provider.github.transport_error",
                    extra={"attempt": attempt + 1, "error_type": type(exc).__name__},
                )
                await self._maybe_backoff(attempt, attempts, retry_after=None)
                continue
            status = response.status_code
            if 200 <= status < 300:
                return response
            if status == 403 and self._looks_like_secondary_rate_limit(response):
                last_error = GitProviderTransientError(
                    "github transient status: 403-secondary-rate-limit"
                )
                retry_after = self._read_retry_after(response)
                await self._maybe_backoff(attempt, attempts, retry_after=retry_after)
                continue
            if status == 429 or 500 <= status < 600:
                last_error = GitProviderTransientError(f"github transient status: {status}")
                retry_after = self._read_retry_after(response) if status == 429 else None
                await self._maybe_backoff(attempt, attempts, retry_after=retry_after)
                continue
            raise self._classify_4xx(status)
        assert last_error is not None  # attempts exhausted
        raise last_error

    @staticmethod
    def _classify_4xx(status: int) -> GitProviderError:
        if status == 401:
            return GitProviderAuthError(f"github auth failed: {status}")
        if status == 403:
            return GitProviderAuthError(f"github auth failed: {status}")
        if status in (400, 404, 422):
            return GitProviderInvalidRequestError(f"github rejected request: {status}")
        return GitProviderError(f"github unexpected status: {status}")

    @staticmethod
    def _read_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _looks_like_secondary_rate_limit(response: httpx.Response) -> bool:
        """Best-effort detection for GitHub's secondary throttling.

        GitHub often reports secondary rate limits as HTTP 403 rather
        than 429. We treat those as transient when either the headers
        or the body carry the usual rate-limit markers.
        """

        if response.headers.get("retry-after") is not None:
            return True
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        try:
            body = response.json()
        except ValueError:
            return False
        if not isinstance(body, dict):
            return False
        message = body.get("message")
        if not isinstance(message, str):
            return False
        lowered = message.lower()
        return "secondary rate limit" in lowered or "abuse detection" in lowered

    async def _maybe_backoff(
        self, attempt: int, attempts: int, *, retry_after: float | None
    ) -> None:
        if attempt + 1 >= attempts:
            return
        if retry_after is not None:
            delay = min(retry_after, self._config.max_backoff_seconds)
        else:
            base = self._config.initial_backoff_seconds * (2**attempt)
            delay = min(base, self._config.max_backoff_seconds)
        await self._sleep(delay)

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise GitProviderError(f"github response invalid JSON: {exc!s}") from exc
