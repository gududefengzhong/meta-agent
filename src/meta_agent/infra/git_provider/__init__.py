"""GitProvider adapters.

【当前】
- ``FakeGitProvider``: 内存实现，不接任何远端，按 ``(tenant_id,
  repo_url, head_branch)`` 做幂等。用于单元测试和 auto_pr v1 的端到端
  契约打通。
- ``GitHubGitProvider``: GitHub REST 适配器（github.com / GHE），
  search→create 两段，4 类错误映射，5xx/429/transport 重试。v1 进程级
  单 token；多租户独立凭据延后里程碑。
【目标】GitLabGitProvider 等其他远端适配器；多租户凭据隔离；熔断接入。
"""

from meta_agent.infra.git_provider.fake import FakeGitProvider
from meta_agent.infra.git_provider.github import GitHubGitProvider
from meta_agent.infra.git_provider.github_config import GitHubGitProviderConfig

__all__ = ["FakeGitProvider", "GitHubGitProvider", "GitHubGitProviderConfig"]
