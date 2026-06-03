# AGENT_SPEC.md - Bug Fix Agent 产品规格

## 文档定位
本文件是产品规格的事实来源，定义目标、能力边界、非功能要求、交付形态和阶段优先级。

本文件不定义具体协作流程；开发协作规则见仓库根目录 `CLAUDE.md`。

## 产品目标
构建一个**生产姿态的 Bug Fix Agent**：调用方通过 REST API 提交 bug 描述 + 仓库引用，agent 用 plan-act-observe 循环读代码、改文件、跑 verifier，最终产出 PR-ready patch。

设计上贯彻多租户隔离、计费、审计、模型路由、可观测和成本治理；实现上以"production-shape reference 实现"为目标——证明这套架构能在企业部署所需的工程姿态下落地，**不以"已部署 SaaS"为前提**。

## 目标用户
- 需要 bug fix / 代码修改自动化的工程团队

## 优先级分层

### L0 - 不可违反的基础约束
这些约束优先于一切功能开发，任何阶段都不能绕过：
- 多租户隔离：`tenant_id` 贯穿请求、任务、内存、审计和计费链路
- 安全与合规：审计日志、权限控制、Secrets 管理、输入防护
- Git 工作流隔离：所有任务在 `git worktree + feature branch` 中执行，禁止直接修改主分支
- 可追踪性：关键动作、模型调用、代码变更、人工确认必须可追踪
- 成本可见性：每次 LLM 调用和每个任务的 token 与费用必须可记录、可汇总

### L1 - 核心产品价值（已落地）
1. **Bug Fix**：`bug_fix` graph（plan/patch/verify/push/finalize），在 per-task git worktree 内修改文件、提交并可选 push；verify 失败时支持有限 replan；多语言 verifier（Python ruff+pytest / TypeScript tsc+vitest）；docker-backed integration smoke 双语跑通。
2. **Auto PR**：`builtin.auto_pr` graph（prepare/publish/finalize）+ `GitProvider.open_or_reuse_pr` Port；FakeGitProvider 与 GitHubGitProvider 两条装配路径；`BUG_FIX → AUTO_PR` follow-up chain 已打通。它是可选发布步骤，不反向决定父 `bug_fix` 任务是否成功。

说明：
- `code_review` 等其它 graph 仍存在于仓库中，但不属于当前主产品叙事
- README、dogfood baseline、面试叙事统一围绕 `bug_fix` 主链路

原则：
- 优先把单条主链路打通，不铺大而全的平台外壳
- 每项能力都要有明确输入、可验证输出和最小验收标准

### L2 - 平台化能力
- Checkpoints / 恢复（γ 已落地）
- Permission Modes / 人机协同（γ + δ-1 已落地）
- 多会话与上下文引用（δ-1 已落地 session 模型 + 历史注入）
- Subagents / 子任务编排（设计就绪，未实现）
- Hooks / 命令扩展（设计就绪，未实现）
- MCP Server / 互操作（设计就绪，未实现）

### L3 - 评测与扩展能力
- 离线 / 在线评估体系（audit_events + llm_usage_logs + task_checkpoints + trajectory API 已落地数据采集层；5-case repo-local real-LLM eval baseline 已落地；终态 task 自动 best-effort 导出到 Langfuse，CLI 仍可按需重导出）
- 多模型路由实验（β+ `LLMRouter` 已落地按 `step_kind` 路由）
- CLI v0 已落地（开发辅助形态），包含 `submit` / `tail` / `run` / `trace`

原则：评测和开发辅助能力不应替代核心闭环。

## 当前实现快照（2026-05-27）

当前仓库已经完成的是一个**生产姿态 reference implementation**，不是 SaaS 部署：

- **主链路**：API submit → PG outbox → Redis Streams → worker consumer group → graph runtime → tool-use loop → deterministic verifier → task result；`BUG_FIX → AUTO_PR` follow-up chain 已有，真实 GitHub PR 能力通过 `GitProvider` Port 接入。
- **执行隔离**：per-task git workspace / feature branch；Docker workspace backend；tool allow-list、超时、输出截断。
- **可观测事实来源**：`audit_events`、`llm_usage_logs`、`task_checkpoints`、`tasks.result_json` 全部落 PostgreSQL；`GET /v1/tasks/{id}/trajectory` 和 `meta-agent trace <task_id>` 能按时间线复盘任务。
- **成本与失败解释**：LLM token / latency / cost 写入 usage；终态 output 聚合 `cost_by_step_kind`；`failure_explanation` 对 verifier failure、budget/max-step truncation 等给出稳定分类。
- **真实 dogfood**：`deepseek/deepseek-v4-pro` 单 case real-LLM dogfood 通过，记录 patch、tool trajectory、usage/cost。
- **小型评测**：5-case repo-local real-LLM eval baseline 已落地，覆盖 3 个 Python + 2 个 TypeScript bug-fix case；Run A 为 5/5 verifier passed，31,744 tokens，29,315 micro-USD，并输出 JD 友好指标（success rate、avg tokens/cost、tool failures、verifier failures、human interventions）。
- **明确边界**：SWE-bench、MCP、VS Code、K8s 不是当前面试项目交付范围；它们只作为后续扩展或面试中说明取舍。

当前最明显的产品缺口是**可视化还不是 task-centric ops console**：数据已经结构化落库，worker 会在任务终态后 best-effort 导出 Langfuse trace，并提供 `meta-agent export-langfuse <task_id>` 手动重导出。短期保留 DB 作为 source of truth；中期用 Langfuse 做 LLM trace / eval 分析视图；长期再做 task-centric ops console。

## 核心能力说明

### Bug Fix
- 输入：问题描述、仓库上下文、约束条件
- 输出：可审查的代码修改、验证结果、必要时的修复说明
- 要求：可回放、可审计、可人工接管

### Auto PR
- 输入：任务目标、代码变更、验证结果
- 输出：规范化 PR 标题、描述、变更摘要、验证摘要
- 要求：与代码实际变更和验证结果一致，不编造完成情况

### Evaluation / Monitoring
- 数据采集层（已落地）：`audit_events` 记录每步 graph 决策；`llm_usage_logs` 记录每次 LLM 调用的 model / step_kind / tokens / cost / latency；`task_checkpoints` 持久化状态机；trajectory API 按时间轴 JOIN 三表
- 分析层（可插拔）：可对接 Langfuse 等开源 LLM observability 平台做 trace / drift / dataset eval / LLM-as-judge；audit + usage 作为合规与计费的 source of truth，Langfuse 作为开发期 / 上线后分析视图。当前 `.env` 可准备 `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`；worker 会在终态后自动导出单个 task trajectory，CLI 可通过 `meta-agent export-langfuse <task_id>` 手动补导
- 要求：监控指标至少覆盖任务成功率、工具调用成功率、端到端时延、token 消耗、成本分布、失败原因

## 企业级非功能要求

### 多租户与权限
- `tenant_id` 全链路隔离
- 入口鉴权：`Authorization: Bearer <token>` → `RequestContext` 构造（已落地 EnvTokenValidator / PgTokenValidator 双实现）
- SSO / OIDC / RBAC：留 Port，未实现

### 计费与成本治理
- 记录每次 LLM 调用的模型、token、费用、租户归属、step_kind
- 任务级和租户级成本聚合
- 任务级预算硬上限（已落地 `BudgetPolicy` + `budget_threshold_micros` 显式契约）

### 可靠性与性能
- 限流：Redis 令牌桶 + `RateLimiter` Port，覆盖 tenant × model × tool
- 熔断：`pybreaker` 本地快路径 + Redis 共享统计，外部依赖（OpenRouter / Git Provider）触发后显式 fallback
- 缓存、并行、上下文裁剪：按场景在 Tool / Graph 层就近实现
- 异步调度：Redis Streams 队列 + 多 worker consumer group

### 审计与可观测性
- 关键链路具备结构化日志、审计事件、LLM usage、checkpoint 和 trajectory
- 能复盘任务执行路径、模型调用、工具调用、人工确认点和失败解释（trajectory API + CLI trace）
- 当前不依赖 Langfuse 作为事实来源；Langfuse 只作为可插拔分析层，worker 终态自动导出 + CLI 手动补导共同覆盖可视化分析

### 人机协同
- 高风险动作支持人工确认（当前 REST 主产品面仅承诺 `PermissionMode = auto | approve_before_push`）
- 系统显式表达当前执行模式，不隐藏自动化边界

## 架构原则
- 核心编排采用状态机 / 图式工作流（LangGraph 风格自实现），便于恢复、审计和人机协同
- 核心业务逻辑保持语言中立；语言特定能力通过工具层扩展
- 模型路由、计费、租户隔离、审计作为清晰的横切能力管理，不散落在业务逻辑里
- 演示层 / 插件层 / 宿主层不应成为核心域模型的事实来源
- Evaluation 用于驱动质量提升，不替代真实链路验收

## 推荐技术方向
以下是当前已选用方向：
- 语言：Python 3.11+
- 数据建模：Pydantic v2
- 工作流编排：LangGraph 风格自实现状态机（见 ADR）
- LLM 接入与路由：OpenRouter（BYO key），自实现 `LLMRouter` 按 `step_kind` 路由
- 存储：PostgreSQL、Redis、pgvector
- 观测分析（可插拔）：Langfuse（OSS，self-host）
- 容器：Docker；docker-compose 本地开发栈

## 横切基础设施选型
以下选型经评审拍板：
- 任务队列：Redis Streams（通过 Queue Port 抽象，长期保留迁移到 NATS JetStream / Kafka 的能力）
- 应用层限流：Redis 令牌桶 + `RateLimiter` Port，覆盖 tenant / task_type / model / tool 等维度
- 熔断：`pybreaker` 本地快路径 + Redis 共享统计汇总，针对外部依赖（OpenRouter / Git Provider / 对象存储）启用，显式 fallback
- 抽象约束：队列、限流、熔断、外部依赖调用均以 Port 抽象，业务代码不直接依赖具体驱动

具体对比与决策记录见 `docs/specs/INFRA_SELECTION_MATRIX.md`。

## 交付形态

1. **独立服务（REST API）**（主交付，已落地）：API 接入与异步任务管理；这是当前对外产品接口
2. **CLI**（开发辅助，v0 已落地）：`python -m meta_agent.cli {submit,tail,run}`；适合本地调试 / smoke / 开发者自测

### LLM 自带（BYO key）

- 部署方选择 LLM 提供方 + 自己的 key
- 服务端负责 LLM 路由、redaction、计量、缓存、限流，并持有部署环境里的 provider 凭据
- 默认路由按 β+ 的 `step_kind` → 中国系开源模型（DeepSeek / Qwen / GLM）；客户可整体或按 step_kind 覆盖

### 设计原则
- CLI / Server 都必须能够脱离任一具体 LLM 提供方运行
- 主产品接口保持简单：提交任务、查询状态、读取结果、复盘轨迹

## 部署

### 本地开发 / PoC
- `docker compose up --build`：拉起 postgres + redis + 一次性 alembic 迁移 + api(:8000) + worker
- `.env` 提供 `OPENROUTER_API_KEY` + `META_AGENT_API_KEYS=<token>:<tenant>:<principal>`
- 调用方通过 REST API 接入；CLI 仅作本地辅助

### 生产部署（设计就绪，未实现）
- 多副本 API + Worker；Postgres 高可用；Redis 高可用
- 任务状态、审计、计费记录全部外置到 PG，不依赖单进程内存
- Outbox + webhook 提供异步通知闭环
- 容器编排（K8s Helm / 等价方案）+ 可观测组件（Prometheus + OTel）+ Secret Manager —— 这些都未实现，留待真实部署场景出现时按需补齐

## 分阶段推进计划

【目标】本节记录从 0 到当前能力的执行节奏。每段以「目标 / 关键交付项 / 退出条件」三段式描述。

### Phase α — 安全生产线 v0
【状态】**已完成**。

【目标】使现有 L1 主链路可在多租户、有限流 / 熔断 / 鉴权 / 预算控制的最小可用环境下被多调用方安全使用。

关键交付项：
- `RateLimiter` Port + Redis 令牌桶实现，先接入 OpenRouter（tenant + model 维度），后续扩 Git Provider（tenant + repo）与 Tool（tenant + tool）
- `CircuitBreaker` Port + `pybreaker` + Redis 共享统计；先接 OpenRouter 与 Git Provider；命中后显式 fallback
- 入口鉴权中间件：解析 `Authorization: Bearer <token>` → 构造 `RequestContext`
- `Secrets` Port：env + 文件双实现；KMS / Vault 留 Port 占位
- 任务级预算硬上限：基于 `llm_usage_logs` 当月聚合检查；超限拒绝并写 `llm.budget.exceeded`
- 查询 API：`GET /v1/audits`、`GET /v1/usages`、`GET /v1/usages/aggregate`

退出条件：
- 上述 Port 与默认实现合入 main
- OpenRouter 调用全链路被限流 + 熔断覆盖，且仍写入 `llm_usage_logs`
- 多租户审计与成本可通过 API 查询

### Phase β — Tool-use agent loop + 执行沙箱
【状态】**已完成**。

【目标】把 `bug_fix` 从一次性 patch 升级为 plan-act-observe agent loop，把工具执行隔离起来。

关键交付项：
- `LLMRequest.tools: list[ToolSpec]`；OpenRouter adapter 透传
- 工具层 Ports：`FileSystemTool`（read / list / grep）、`EditTool`（write / patch apply）、`ShellTool`（白名单 + 超时 + 输出截断）、`TestTool`（多语言 dispatch）
- 通用 `shell_agent` graph：plan → tool_call → observe → loop，带 `max_steps` 与 `max_total_tokens` 上限
- 容器化 `WorkspaceManager`：Docker 实现
- `bug_fix` 通过 `shell_agent` 完成工具循环，保留 replan 语义
- 多语言 verifier：Python（ruff + pytest）+ TypeScript（tsc + vitest）

退出清单：
- `shell_agent` 支持 `plan → tool_call → observe → loop`，覆盖 `max_steps` / `max_total_tokens` / 错误回灌
- `ToolRegistry` / `ToolExecutor` 可执行：`fs_read` / `fs_list_dir` / `fs_grep` / `edit_write` / `edit_patch_apply` / `shell_run` / `test_run`
- `bug_fix` 通过 `test_run` 完成 deterministic verify，支持 Python + TypeScript
- docker-backed integration smoke：Python repo + TypeScript repo 均跑通

### Phase β+ — Agent 能力深度补全
【状态】**已完成**。

【目标】把 β 的通用 tool-use loop 从「能跑闭环」推到「能在真实代码库上有指向地完成任务」。补**定位能力（retrieval）**、**外部世界感知（web / doc）**、**步级模型路由**、**任务类型扩展**。

关键交付项：
- 代码理解层（Retrieval / Symbol Graph）
  - `CodeIndex` Port + 默认实现：tree-sitter 抽符号 + 段级 embedding（pgvector）+ 跨文件 reference 索引
  - Tool 化：`code_search`、`get_definition`、`get_references`、`outline`
- 外部世界感知工具
  - `WebFetch`：URL → 文本 + 截断 + 域名 allow-list
  - `DocSearch`：基于可插拔检索 Port
  - 工具调用走 α 阶段的限流 + 熔断 + 计费链路，按 `tool` 维度计量
- 多模型路由（intra-task）
  - LLM Port 增 `LLMRouter` 实现：按 `step_kind`（plan / edit / search / observe）选模型
  - 路由决策写入 `llm_usage_logs.step_kind` 字段
- Prompt 资产管理
  - prompt 版本化：`prompt_id` + 内容哈希
  - `audit_events` 与 `llm_usage_logs` 记录调用时的 prompt 版本

### Phase γ — 信任面 + 长程恢复
【状态】**已完成**（A / B-1 / B-2 / C / D 全部合入 main）。

【目标】Permission Modes / Checkpoint 恢复 / 人机协同补到企业级形态。核心命题是 **人能看清 agent 在做什么、能在关键节点介入、能在长程任务中断后无损恢复**。

等待状态刻意 ephemeral：进入 gate 时 worker 释放任务、container 关停、worktree 留在磁盘上等待续跑。这套设计与 Claude Code 客户端等待用户回复的成本量级一致。

`Task.permission_mode` 与 `Task.budget_policy` 是两个独立组合的字段，不是包含关系。对外 REST 合约只暴露当前产品真正支持的子集。

关键交付项：
- **Checkpoint 恢复**：worker 启动扫 `RUNNING` 任务，从 `task_checkpoints` 续跑；跨实例迁移不丢状态
- **PermissionMode（公共 REST 合约）**：`auto` / `approve_before_push`
- **PermissionMode（内部保留）**：`approve_each_tool` / `plan` 仍存在于 runtime，但不作为当前 bugfix 产品面的一部分
- **BudgetPolicy**：`none` / `gate_on_threshold` / `abort_on_threshold`；与 PermissionMode 正交。`gate_on_threshold` / `abort_on_threshold` 必须显式提供 `budget_threshold_micros`
- **`human_gate` 节点 + `AWAITING_APPROVAL` 状态**：graph 内置节点
- **Approve / Reject API**：`POST /v1/tasks/{id}/approve`、`POST /v1/tasks/{id}/abort`
- **结构化反馈通道**：approve 携带的 `feedback` 注入下一轮 plan
- **Trajectory 查询 API**：`GET /v1/tasks/{id}/trajectory` 按时间轴合并 audit + checkpoints + usage
- **Outbound webhook consumer**：HTTP POST + HMAC 签名 + 退避重试 + dedupe + 死信
- **Per-task 成本视图 + budget gate**：cost breakdown by step_kind & model；预算门看的是单个 task 的累计花费，不是单次调用，也不是时间窗口
- **Prompt redaction layer**：LLM 调用前后扫描 secret / PII
- **超长尾 sweeper**：扫 `AWAITING_APPROVAL` > 30 天 → `EXPIRED` + webhook 通知

退出条件：
- 人工 approve / abort 路径有集成测试覆盖；带 `feedback` 注入的 replan 跑通端到端
- 进行中任务在 worker 异常中止 + 重启 / 跨实例迁移后能从最近 checkpoint 续跑
- 任意 `AWAITING_APPROVAL` 任务通过 trajectory API 返回完整 step 序列
- Outbox → webhook 在故障注入下满足「最少一次 + 去重」
- `permission_mode` × `budget_policy` 正交组合至少覆盖测试矩阵
- Prompt redaction 覆盖 5+ 类敏感模式
- 30 天 sweeper 跑过一轮

### Phase δ-1 — 日常体验客户端
【状态】**已完成**。

【目标】把 bug fix agent 从"server 跑得通"升级为"开发者每天能用"。

关键交付项：
- **Inline permission protocol**：`PermissionGate` Port + `InMemoryPermissionGate` / `RedisPermissionGate`。worker 端 `gate.request()` 阻塞 120s；客户端接 prompt 后调用 `POST /decide`。和 γ-A 的 `AWAITING_APPROVAL` 共存
- **Session 模型**：`POST /v1/tasks` 自动 upsert session；worker 加载同 session 历史 task 的 (user_prompt, assistant_message) 注入下一轮 graph state
- **CLI v0**：`python -m meta_agent.cli {submit|tail|run}`；env-driven config；exit-code taxonomy；`--no-interactive` 绕过 prompt 处理；支持本地调试与 inline approval
- **Plan mode**：`PermissionMode.PLAN` —— shell_agent 在 `tool_call` 节点对整个 planning step 只发 1 个 gate prompt；客户端一次 approve 整批执行，deny 则全数 skip 并把理由喂回 model 重规划

退出条件：
- ✅ CLI 能提交 / 跟踪任务，并在需要时接住 inline permission prompt（三档 permission mode 全覆盖）

### 节奏说明
- 单段建议 2-3 周；单段内拆 3-5 个独立 PR
- α 是其余阶段的安全底座，必须先落
- β+ 在 β 之后、γ 之前；β+ 与 γ 不可并行（γ 的 checkpoint 设计依赖稳定的 tool 集合与 prompt 版本）
- 每段开始前先做最小化探索，确认范围与最小子集，再进入实现
- 本节奏不替代 L0–L3 的优先级分层；冲突时以 L0 约束优先

## 当前状态标注要求
凡涉及目录结构、命令入口、模块边界、部署清单，必须明确区分：
- 当前已实现
- 目标结构
- 候选方案

禁止把目标态描述成当前仓库事实。
