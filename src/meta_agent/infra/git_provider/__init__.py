"""GitProvider adapters.

【当前】FakeGitProvider: 内存实现，不接任何远端，按 ``(tenant_id,
repo_url, head_branch)`` 做幂等。仅用于单元测试和 auto_pr v1 的端到端
契约打通。
【目标】GitHubGitProvider / GitLabGitProvider 等真实远端适配器，
带凭据注入、限流、熔断和重试。
"""

from meta_agent.infra.git_provider.fake import FakeGitProvider

__all__ = ["FakeGitProvider"]
