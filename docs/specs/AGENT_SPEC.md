# AGENT_SPEC.md - 企业级 Code Agent 产品规格

## 文档定位
本文件是产品规格的唯一事实来源，定义目标、能力边界、非功能要求、交付形态、部署方式和阶段优先级。

本文件不定义具体协作流程；开发协作规则见仓库根目录 `CLAUDE.md`。

## 产品目标
构建一个面向企业场景的生产级 Code Agent。它可以接收自然语言工程任务，完成规划、执行、验证，并在受控流程下产出代码变更、审查结果和 PR 级交付物。

该产品既要提供真实可用的工程能力，也要满足企业环境中的多租户、计费、审计、模型路由和互操作要求。

本项目的目标不是单团队内部工具，而是面向公司级推广的平台型能力。系统必须支持分布式部署和横向扩展，能够承载多团队、多租户、多人同时使用的企业场景。

## 目标用户
- 需要在企业内部大规模部署代码代理的团队
- 需要受控自动化修复、代码审查和 PR 生成能力的平台团队
- 需要通过 MCP 或 API 集成 Agent 能力的上层客户端和工具链

## 优先级分层

### L0 - 不可违反的基础约束
这些约束优先于一切功能开发，任何阶段都不能绕过：
- 多租户隔离：`tenant_id` 贯穿请求、任务、内存、审计和计费链路
- 安全与合规：审计日志、权限控制、Secrets 管理、输入防护
- Git 工作流隔离：所有任务在 `git worktree + feature branch` 中执行，禁止直接修改主分支
- 可追踪性：关键动作、模型调用、代码变更、人工确认必须可追踪
- 成本可见性：每次 LLM 调用和每个任务的 token 与费用必须可记录、可汇总

### L1 - 第一阶段核心产品价值
这是最先要做实的用户价值层：
1. Bug 修复（Bug Fix） — **首版已落地**：`builtin.bug_fix` graph（plan/patch/verify/push/finalize），在 per-task git worktree 内修改文件、提交并可选 push；verify 失败时支持有限 replan，再把 `repo_url/base_ref/head_branch/head_commit_sha` 等交给后续 `AUTO_PR`。端到端 docker-compose smoke 已验证（含 Scheme X 失败用例）。未做：多语言 verifier、更通用的 tool-use loop / 执行沙箱。
2. 代码审查（Code Review） — **首版已落地**：`builtin.code_review` graph（prepare/review/finalize），pure-LLM，无 workspace，Pydantic schema 严格校验输出（verdict / findings / confidence）；端到端 docker-compose smoke 已验证。未做：跨多文件 diff 切片、规则化静态检查与 LLM 结合、与 auto_pr 的链路串接。
3. 自动 PR 生成与更新（Auto PR） — **v1 已落地（Fake + GitHub）**：`builtin.auto_pr` graph（prepare/publish/finalize）+ `GitProvider.open_or_reuse_pr` port。当前 worker 已支持 `FakeGitProvider` 与 `GitHubGitProvider` 两条装配路径，`BUG_FIX -> AUTO_PR` follow-up chain 也已打通。Skip 规则在 Scheme X 下落 `succeeded`：`no_repo_url` / `no_commit_sha` / `verifier_failed`。未做：PR update / comment / close、多租户独立 Git 凭据、更细的 GitHub 限流/二级限流治理。

原则：
- 优先把单条主链路打通，而不是先铺大而全的平台外壳
- 每项能力都要有明确输入、可验证输出和最小可行验收标准

### L2 - 第二阶段平台化能力
这是让产品能被外部系统稳定调用和管理的能力层：
4. MCP 完整支持，同时作为 MCP Client 和 MCP Server
5. Checkpoints / 恢复能力
6. Subagents / 子任务编排能力
7. Permission Modes / 人机协同执行模式
8. Hooks、命令扩展、插件式扩展点
9. 多会话与高级上下文引用能力

原则：
- 这些能力服务于核心链路，不应先于 L1 主价值闭环而独立膨胀
- 外部可调用能力优先通过 MCP Tool / Resource 暴露

### L3 - 第三阶段扩展与评测能力
10. 主流 Code Agent Benchmark 运行与评测
11. 多模型评测与路由实验
12. CLI / Web UI / IDE 插件等多形态交付

原则：
- Benchmark 和多端形态是验证与扩张能力，不应替代核心产品闭环

## 核心能力说明

### Bug Fix
- 输入：问题描述、仓库上下文、约束条件
- 输出：可审查的代码修改、验证结果、必要时的修复说明
- 要求：可回放、可审计、可人工接管

### Code Review
- 输入：代码 diff、PR 上下文、规则集
- 输出：结构化审查结论、风险点、建议动作
- 要求：优先发现行为回归、风险和缺失验证，而不是生成泛泛总结

### Auto PR
- 输入：任务目标、代码变更、验证结果
- 输出：规范化 PR 标题、描述、变更摘要、验证摘要
- 要求：与代码实际变更和验证结果一致，不能编造完成情况

### Benchmark
- 支持运行和记录主流 Code Agent Benchmark
- 目标 benchmark 可包括 SWE-bench Verified / Pro、Terminal-Bench 2.0、LiveCodeBench 等
- benchmark 支持多模型对比，但 benchmark 体系本身排在核心产品能力之后

## 企业级非功能要求

### 多租户与权限
- `tenant_id` 全链路隔离
- 支持 RBAC
- 支持企业认证接入，例如 SSO

### 计费与成本治理
- 记录每次 LLM 调用的模型、token、费用、租户归属
- 支持任务级和租户级成本聚合
- 支持预算监控、告警和成本回溯

### 高可用与性能
- 支持高并发、限流、熔断、缓存和异步调度
- 必须支持分布式部署、横向扩容和多副本运行，不能只依赖单机部署形态
- 目标态可部署在高可用基础设施上
- 具体 QPS、延迟、可用性指标应在实施阶段结合容量规划单独固化，避免在产品规格中写死未经验证的数字

### 审计与可观测性
- 关键链路具备日志、指标、追踪和错误上报
- 能复盘任务执行路径、模型决策和人工确认点

### 人机协同
- 高风险、破坏性、越权或成本显著的动作必须支持人工确认
- 系统要显式表达当前执行模式，而不是隐藏自动化边界

## 架构原则
- 核心编排建议采用状态机/图式工作流，便于恢复、审计和人机协同
- 核心业务逻辑保持语言中立；语言特定能力通过工具层扩展
- 模型路由、计费、租户隔离、审计不要散落在业务逻辑里，应作为清晰的横切能力管理
- 演示层、插件层、宿主层不应成为核心域模型的事实来源
- Evals 用于驱动质量提升，但不应替代真实产品链路验收

## 推荐技术方向
以下是当前推荐方向，不代表所有项都已实现：
- 语言：Python 3.11+
- 数据建模：Pydantic v2
- 工作流编排：LangGraph
- LLM 接入与路由：OpenRouter
- 存储：PostgreSQL、Redis、PGVector
- 观测：LangSmith、OpenTelemetry、Prometheus、Grafana、Sentry
- 部署：Docker、Kubernetes

## 横切基础设施选型
【目标】以下选型经评审拍板，作为 Phase 0 起点的横切基础设施默认实现：
- 任务队列：Redis Streams（通过 Queue Port 抽象，长期保留迁移到 NATS JetStream / Kafka 的能力）
- 入口网关：Higress（统一处理 TLS、认证、入口限流、灰度）
- 应用层限流：Redis 令牌桶 + `RateLimiter` Port，覆盖 tenant / task_type / model / tool 等维度
- 熔断：`pybreaker` 本地快路径 + Redis 共享统计汇总，针对外部依赖（OpenRouter / Git Provider / 外部 MCP / 对象存储）启用，显式 fallback
- 抽象约束：队列、限流、熔断、外部依赖调用均以 Port 抽象，业务代码不直接依赖具体驱动

具体对比、权衡维度与决策记录见 `docs/specs/INFRA_SELECTION_MATRIX.md`。

## 交付形态

【目标】产品定位是 **code agent 产品本身**（开发者直接使用），不是平台 backend。客户端形态按优先级：

1. **VS Code 插件**（首选）：日常 inline 体验 —— diff review、inline approval、plan mode、trajectory viewer、workspace browser 都在这里
2. **CLI**（必须）：power user / CI 集成 / 远程会话；以 streaming 形式呈现 agent 工作过程
3. **独立企业服务（REST + SSE）**：API 接入与异步任务管理；多副本 / 高可用形态
4. **Web UI**（次要）：管理面（多任务列表、成本看板、AWAITING_APPROVAL 待审）+ 非交互场景兜底
5. **MCP Server**（互操作）：让 Claude Code / Cursor / ChatGPT 等其他 host 也能调起我们；属于"扩面"，不是替代主客户端
6. **JetBrains 插件 / 其他 IDE**：后续阶段

### LLM 自带（BYO key）

- 客户在 VS Code / CLI 配置面自行选择 LLM 提供方（OpenRouter / Anthropic / OpenAI / 本地）+ 自己的 key
- 服务端只做 LLM 路由、redaction、计量、缓存、限流，不持有客户的 LLM 凭据
- 默认路由仍按 β+ 的 step_kind → 中国系开源模型（DeepSeek / Qwen / GLM）；客户可整体或按 step_kind 覆盖

### 设计原则
- VS Code 插件 / CLI / Server 都必须能够脱离任一具体 LLM 提供方运行
- MCP 是互操作接口，**不是**主交付形态 —— 它让外部 host 用我们的能力，但 VS Code 插件 / CLI 才是我们自己的产品门面
- 生产交付必须支持分布式部署，不得将单机版作为唯一正式交付形态
- 凡进入用户日常 IDE 的能力必须支持 streaming responses + inline permission protocol（不能只让用户等批处理结果）

## 部署与交互方式

### 部署原则
- 采用云厂商中立设计，不绑定单一基础设施供应商
- 默认以容器化方式交付，优先支持 Docker 部署和 Kubernetes 编排
- 同一套核心服务应支持本地开发、单机部署和云上生产部署
- 单机部署只用于开发、演示和 PoC；正式生产形态必须支持分布式部署

### 支持的部署环境
可以部署在以下环境中，产品规格不限制云厂商：
- 阿里云
- 腾讯云
- AWS
- GCP
- Azure
- 企业自建机房或私有云

原则：
- 只要目标环境支持容器、网络、持久化存储和密钥管理，就应能够承载本系统
- 云厂商差异应收敛在基础设施配置层，不应侵入核心业务代码

### 推荐部署拓扑

#### 1. 单机 / PoC 部署
适用场景：
- 本地开发
- 小规模验证
- 内部 PoC

限制说明：
- 该形态只用于开发验证，不作为公司级正式推广方案
- 不应把单机部署能力误写成生产级最终架构

典型组成：
- `agent-api`：统一入口，提供 REST / WebSocket / MCP 接口
- `agent-worker`：执行异步任务、代码操作、评测任务
- `postgres`：业务数据、任务状态、计费记录、审计记录
- `redis`：队列、缓存、会话态
- 可选 `pgvector` 扩展：长期记忆和检索

#### 2. 云上生产部署
适用场景：
- 企业内部正式使用
- 多租户和高并发场景

典型组成：
- API Gateway / Ingress：统一接入鉴权、限流、TLS
- `agent-api` 多副本：处理同步请求、任务提交、状态查询
- `agent-worker` 多副本：处理异步执行、代码修改、benchmark、审查任务
- PostgreSQL 高可用实例
- Redis 高可用实例
- 对象存储：保存日志包、工件、报告、评测结果
- 可观测性组件：日志、指标、追踪、错误上报
- Secret Manager / KMS：管理 API Key、数据库密码、云凭证

硬要求：
- 生产部署必须支持 API 层和 Worker 层多副本运行
- 任务状态、会话状态、审计记录、计费记录不得只保存在单进程内存中
- 任一单实例故障不应导致全局任务状态不可恢复

### 部署模式

#### MCP Server 模式
- 作为一个可连接的 MCP Server 进程或服务部署
- 上层客户端通过 MCP Tool / Resource 与系统交互
- 适合接入 Claude Code、Cursor、ChatGPT 等宿主

#### 独立服务模式
- 作为独立后端服务部署
- 通过 REST API 提交任务、查询任务、获取结果
- 通过 WebSocket 或 SSE 接收任务状态流和长任务进度
- 适合企业内部平台、门户或工作流系统集成

#### CLI / Web UI 模式
- CLI 作为轻量客户端，通过 API 或 MCP 调用后端
- Web UI 作为任务管理与人工确认界面，不应直接承载核心执行逻辑

### 交互方式

#### 同步交互
适用于轻量操作：
- 健康检查
- 配置查询
- 规则查询
- 轻量审查或预检查请求

推荐接口：
- REST API
- MCP Tool 调用

#### 异步交互
适用于长任务：
- Bug Fix
- Auto PR
- 大规模 Code Review
- Benchmark 运行

推荐流程：
1. 客户端提交任务
2. 系统返回 `task_id`
3. 客户端通过 REST 轮询、WebSocket、SSE 或 MCP 续取状态
4. 任务完成后返回结构化结果、日志摘要、工件地址和人工确认点

### 云厂商适配说明
- 阿里云、腾讯云或其他云厂商都可以部署，本系统不依赖某一家专有 PaaS 才能运行
- 若使用托管能力，推荐映射到等价基础设施，而不是把云厂商 SDK 直接写进核心域逻辑

示例映射：
- Kubernetes：阿里云 ACK / 腾讯云 TKE / 其他托管 K8s
- PostgreSQL：云数据库 PostgreSQL 或自建 PostgreSQL
- Redis：云 Redis 或自建 Redis
- 对象存储：OSS / COS / S3 兼容存储
- Secret 管理：云 KMS / Secret Manager 或企业自建密钥系统

### 交付要求
- 至少提供一套本地可运行的 `docker-compose` 或等价开发部署方案
- 至少提供一套 Kubernetes 生产部署清单或 Helm Chart
- 必须提供环境变量、密钥、数据库和对象存储的配置说明
- 必须明确哪些接口给人用，哪些接口给宿主 Agent 或其他系统调用
- 必须提供面向公司级推广的分布式部署方案说明，包括扩容方式、状态外置方式和故障恢复策略

## 分阶段推进计划

【目标】本节定义从当前 main 出发、达到 L0–L3 全部能力的执行节奏，作为后续每个里程碑选型的参考。每段以「目标 / 关键交付项 / 退出条件」三段式描述。

L0–L3 描述的是能力**分层**，本节描述的是**落地顺序**，不替换上面的能力定义。遇到与 L0 约束冲突时仍以 L0 优先。

### Phase α — 安全生产线 v0
【状态】**已完成（v0）**。以下保留本阶段目标与退出口径，作为实现边界记录。

【目标】使现有 L1 主链路可在多租户、有限流 / 熔断 / 鉴权 / 预算控制的最小可用环境下被多调用方安全使用。

关键交付项：
- `RateLimiter` Port + Redis 令牌桶实现，先接入 OpenRouter（tenant + model 维度），后续扩 Git Provider（tenant + repo）与 Tool（tenant + tool）
- `CircuitBreaker` Port + `pybreaker` + Redis 共享统计；先接 OpenRouter 与 Git Provider；命中后显式 fallback
- 入口鉴权中间件：解析 `Authorization: Bearer <token>` → 构造 `RequestContext`；SSO / OIDC 仅留 Port，不实现
- `Secrets` Port：env + 文件双实现；KMS / Vault 留 Port 占位
- 任务级预算硬上限：基于 `llm_usage_logs` 当月聚合检查；超限拒绝并写 `llm.budget.exceeded`
- 查询 API：`GET /v1/audits`、`GET /v1/usages`、`GET /v1/usages/aggregate`

退出条件：
- 上述 Port 与默认实现合入 main
- OpenRouter 调用全链路被限流 + 熔断覆盖，且仍写入 `llm_usage_logs`
- 多租户审计与成本可通过 API 查询，且至少 1 个集成测试覆盖

实现备注（v0 保留限制）：
- 默认 auth backend 仍以 token validator 为主；SSO / OIDC 仍仅保留 Port
- GitProvider 的限流 / 熔断已纳入统一安全壳，但多租户独立 Git 凭据留到后续阶段
- Budget 只对 LLM 调用生效，不覆盖后续 Tool / Shell 消耗

### Phase β — Tool-use agent loop + 执行沙箱
【目标】把 `bug_fix` 从一次性 patch 升级为 plan-act-observe agent loop，对齐主流编辑器型 code agent 基线；同时把工具执行隔离起来。

关键交付项：
- `LLMRequest` 增加 `tools: list[ToolSpec]`；OpenRouter adapter 透传
- 工具层 Ports：`FileSystemTool`（read / list / grep）、`EditTool`（write / patch apply）、`ShellTool`（白名单 + 超时 + 输出截断）、`TestTool`（多语言 dispatch）
- 通用 `shell_agent` graph：plan → tool_call → observe → loop，带 `max_steps` 与 per-task LLM 成本上限
- 容器化 `WorkspaceManager`：Docker 实现；Firecracker / gVisor 等更强隔离方案留 Port
- `bug_fix` v2 切换到 `shell_agent`，保留 replan 语义
- 多语言 verifier：先加 TypeScript（tsc + vitest）

退出清单（固定验收口径）：
- `shell_agent` graph 支持 `plan -> tool_call -> observe -> loop`，并覆盖：
  - `max_steps`
  - `max_total_tokens`
  - tool observation 的 error / truncation / metadata 回灌
- `ToolRegistry` / `ToolExecutor` 可执行并约束以下默认工具面：
  - `fs_read` / `fs_list_dir` / `fs_grep`
  - `edit_write` / `edit_patch_apply`
  - `shell_run`
  - `test_run`
- `bug_fix_v2` 默认承担 `TaskType.BUG_FIX` 的 tool-use 主路由，并保留 v1 作为 legacy fallback。
- `bug_fix_v2` 通过 `test_run` 完成 deterministic verify，至少支持：
  - Python：`python_lint` / `python_test`
  - TypeScript：`typescript_typecheck` / `typescript_test`
- docker-backed integration smoke 必须跑通：
  - Python repo：`tests/integration/test_bug_fix_v2_docker_smoke.py`
  - TypeScript repo：`tests/integration/test_bug_fix_v2_docker_smoke.py`
- LLM / GitProvider 路径继续复用 α 阶段建立的限流、熔断与计费。
- 工具执行面至少具备 allow-list、timeout、output 截断与 workspace/container containment。

当前 main 状态（2026-05-22）：
- 已完成：
  - `LLMRequest.tools`、OpenRouter tool-call 透传
  - `FileSystemTool` / `EditTool` / `ShellTool` / `TestTool`
  - 通用 `shell_agent`
  - `bug_fix_v2` + 单次 replan
  - Docker `WorkspaceManager`
  - docker-backed `fs_* / edit_* / shell_run / test_run`
  - Python suites（`python_lint` / `python_test`）
  - TypeScript suites（`typescript_typecheck` / `typescript_test`）
  - Python 与 TypeScript 的 docker-backed integration smoke
- 收口边界：
  - docker-backed integration smoke 是 β 的退出验收形式；不再要求额外的 docker-compose smoke 作为字面条件
  - `DockerWorkspaceManager` 当前复用 host-side `git worktree`，并用 companion container 执行 docker-backed tools
  - docker backend 下的 `fs_*` / `edit_*` / `shell_run` / `test_run` 已在 container 内执行；`git commit` / `git push` 仍由 `bug_fix_v2` 在 host-side worktree 上完成
  - 工具执行治理当前聚焦 allow-list、timeout、output cap 和 sandbox containment；独立 tool-level rate-limit / breaker / metering 作为 β+ 增强项
  - Firecracker / gVisor 属于更强隔离 backend，留在 β+ 或后续阶段，不阻塞 β 关账

### Phase β+ — Agent 能力深度补全
【目标】把 β 的通用 tool-use loop 从「能跑闭环」推到「能在真实代码库上有指向地完成任务」。β 给了脑子和手，β+ 补**定位能力（retrieval）**、**外部世界感知（web / doc）**、**步级模型路由**与**任务类型扩展**，同时为 δ 的 SWE-bench 评测建立可被打表的能力基线。

本阶段不引入新的横切基础设施，所有新工具与新路由都必须沿用 α 阶段建立的限流 / 熔断 / 计费 / 审计安全壳。

关键交付项：
- 代码理解层（Retrieval / Symbol Graph）
  - `CodeIndex` Port + 默认实现：tree-sitter 抽符号 + 段级 embedding（pgvector 复用 α 的 Postgres）+ 跨文件 reference 索引
  - Tool 化：`code_search`（语义 + 符号双路）、`get_definition`、`get_references`、`outline`
  - 在 `shell_agent` 默认 tool 集合中暴露；对超大仓库以 path glob / language filter 限定 scope
- 外部世界感知工具
  - `WebFetch` 工具：URL → 文本 + 截断 + 域名 allow-list；fetch 失败显式 fallback
  - `DocSearch` 工具：基于可插拔检索 Port，预留 OSS / COS / 企业内部文档源 adapter
  - 工具调用走 α 阶段的限流 + 熔断 + 计费链路（按 `tool` 维度计量），不绕过安全壳
- 任务类型扩展
  - 新增 `task_type=feature_impl`：复用 `shell_agent` graph，差异收敛在 system prompt + verifier 组合（test + lint + build）
  - `bug_fix v2` / `feature_impl` 共享同一 graph，验证 `shell_agent` 抽象的通用性，避免每新增任务类型就新起一条 graph
- 多模型路由（intra-task）
  - LLM Port 增 `LLMRouter` 实现：按 `step_kind`（plan / edit / search / observe）选模型；策略可按 tenant / task_type 配置
  - 路由决策写入 `llm_usage_logs.step_kind` 字段，供 δ 的多模型 A/B 直接消费
- Prompt 资产管理
  - prompt / graph node prompt 升为版本化资源：`prompt_id` + `version` + 内容哈希
  - worker 启动时注入注册表；`audit_events` 与 `llm_usage_logs` 记录调用时的 prompt 版本
  - 配套最小工具：prompt 版本 diff + token 估算脚本

退出条件：
- `shell_agent` 在 ≥10k 文件量级的真实仓库上能基于 `code_search` 定位修改点；docker-compose smoke 覆盖一条 retrieval-driven 修改用例
- `feature_impl` 在 docker-compose smoke 跑通 Python + TypeScript 各一例（自然语言需求 → 通过 verifier）
- `WebFetch` / `DocSearch` 至少一次端到端被 `shell_agent` 调用并产生有效观察
- 同一任务内观测到至少 2 个不同 model 命中；`llm_usage_logs` 按 `step_kind` 聚合查询可用
- prompt 版本号出现在 `llm_usage_logs` 与 `audit_events` 中

实现备注（边界控制）：
- 不引入完整 RAG 系统外壳；embedding 走 OpenRouter 或本地 sentence-transformers 二选一，先做最简单实现，向量维度与模型选择在 PR 中拍板
- Code Review 的「LLM + 规则融合」（CodeQL / Semgrep）不纳入本阶段，作为 L1 `code_review` 的增量任务单独排
- 长期记忆 / 跨任务学习仍属 L2「多会话与高级上下文引用」范畴，本阶段不展开
- 沙箱深度（Firecracker / gVisor）保留 β 的 Port 占位不动；企业安全审计触发时再单独立 phase

### Phase γ — 信任面 + 长程恢复
【目标】把 L2 的 Permission Modes / Checkpoint 恢复 / 人机协同补到企业级可推介形态。核心命题是 **人能看清 agent 在做什么、能在关键节点介入、能在长程任务中断后无损恢复**。

人机协同的等待状态被刻意保持 ephemeral：进入 gate 时 worker 释放任务、container 自然消失、worktree 留在磁盘上等待续跑；用户回应可以隔小时 / 隔天 / 隔周，系统几乎不消耗在线资源 —— 这一点与 Claude Code 客户端等待用户回复的成本量级一致。

**`Task.permission_mode` 与 `Task.budget_policy` 是两个独立组合的字段**，不是包含关系。任意一组合法配对都允许（例如 `permission_mode=auto` + `budget_policy=gate_on_threshold` 表示工具调用不需要人审，但任务级预算超阈值时强制进入 `AWAITING_APPROVAL`）。

关键交付项：

- **Checkpoint 恢复**：worker 启动扫 `RUNNING` 任务，从 `task_checkpoints` 续跑 graph.run；worker 进程意外中止 / 重启 / 跨实例迁移均不丢任务状态
- **PermissionMode**：`Task.permission_mode` ∈ {`auto`, `approve_before_push`, `approve_each_tool`}
- **BudgetPolicy**：`Task.budget_policy` ∈ {`none`, `gate_on_threshold`, `abort_on_threshold`}；与 PermissionMode 正交组合；`gate_on_threshold` 触发时复用 `human_gate` 节点 + `AWAITING_APPROVAL` 状态
- **`human_gate` 节点 + `AWAITING_APPROVAL` 状态**：graph 内置节点；进入 gate 后 worker 释放任务、container 关停、worktree 保留；不引入 detached 中间态
- **Approve / Reject API**：`POST /v1/tasks/{id}/approve`（可带 `feedback` 文本）、`POST /v1/tasks/{id}/abort`
- **结构化反馈通道**：approve 携带的 `feedback` 注入下一轮 plan，复用 bug_fix_v2 已有的 "verifier feedback → replan" 模式，从机器反馈推广到人类反馈
- **Trajectory 查询 API**：`GET /v1/tasks/{id}/trajectory` 按时间轴合并 `audit_events` + `task_checkpoints` + `llm_usage_logs`，输出 step 序列供操作员审阅与重放
- **Outbound webhook consumer**：消费 `OutboxEvent` → HTTP POST + HMAC 签名 + 退避重试 + dedupe + 死信。`AWAITING_APPROVAL` 进入时必发；不接 consumer 就没法形成异步闭环
- **Per-task 成本视图 + budget gate**：任务结果输出 cost breakdown by step_kind & model；任务级预算阈值触发 `BudgetPolicy` 配置的动作
- **Prompt redaction layer**：LLM 调用前一道扫描层（regex + Detect-Secret 风格），把疑似 secret / PII 替换为占位符；响应回吐也扫
- **超长尾 sweeper**：夜间 job 扫 `AWAITING_APPROVAL` 状态 > N 天（默认 30 天）→ 标 `EXPIRED`、删 worktree、webhook 通知用户。简单兜底，无 detached / snapshot / S3 任何中间态

退出条件：

- 人工 approve / abort 路径有集成测试覆盖；带 `feedback` 注入的 replan 跑通端到端
- 进行中任务在 worker 异常中止 + 重启 / 跨实例迁移后能从最近 checkpoint 续跑
- 任意 `AWAITING_APPROVAL` 任务通过 trajectory API 返回完整 step 序列（audit + checkpoints + usage 三表一次性 JOIN 返回）
- Outbox → webhook 在故障注入下满足「最少一次 + 去重」；`AWAITING_APPROVAL` 进入时至少 1 个 webhook 实际投递且被验证签名
- `permission_mode` 与 `budget_policy` 各自的正交组合至少覆盖到测试矩阵（auto / approve_*）× （none / gate / abort）
- 任务级预算阈值触发后按 `budget_policy` 正确动作（gate 进 `AWAITING_APPROVAL`、abort 终态）；任务结果 `output` 含 `cost_by_step_kind` 字段
- Prompt redaction 覆盖至少 5 类敏感模式（API key / 数据库连接串 / 邮箱 / JWT / RSA private key）；redaction 命中写入 audit
- 30 天 sweeper 跑过一轮，过期任务被正确清理 + 通知

实现备注（边界控制）：

- 等待状态刻意不持有在线资源：worktree 在磁盘、checkpoint 在 DB，没有 detached 中间态、没有 snapshot 上传、没有 base_ref 漂移检测 —— 这些都是实测出现真问题再补，不预先建复杂状态机
- SSE `/v1/tasks/{id}/events`：nice-to-have，webhook 已覆盖大部分异步通知需求；移到本阶段尾部或 γ.5
- 任务级幂等 abort 的 cancellation 传播（中止 in-flight LLM 调用 / subprocess）：实现复杂，放 γ.5 单独 PR
- 多 worker 同时拿同一任务的竞态：复用 α 的 Redis Streams consumer group 语义，不引入新机制
- 反馈注入的 prompt 拼接：复用 PromptRegistry 的 `$feedback` 占位符机制，避免新增模板系统
- Trajectory 输出按 task 单次 JOIN，避免 N+1；> 1000 步的任务做分页

节奏调整：

【状态】**γ 已完成（A/B-1/B-2/C/D 全部合入 main）**。以下保留拆分供历史参考。

- 估时 ~2-3 周（Checkpoint 已付了 90% 的工程税，trajectory / webhook / sweeper / cost view 都是小增量；redaction 自成模块但实现简单）
- 内部拆 4 段：
  - **γ-A**：Checkpoint 恢复 + PermissionMode + BudgetPolicy + `AWAITING_APPROVAL` + abort
  - **γ-B-1 / γ-B-2**：Trajectory API + Outbound webhook consumer
  - **γ-C**：结构化反馈注入 + Per-task cost view + budget gate / abort 动作 + 30d sweeper
  - **γ-D**：Prompt redaction + SSE
- γ-A + γ-B 即"企业可演示"形态；γ-C 让 trust 故事完整；γ-D 补足合规面 + 体验面

### Phase δ-1 — 日常体验客户端（Track A 完成；Track B 撤回）

【状态】**Track A 已完成；Track B 整体撤回**（自写 harness 是错误方向，详见下方"Track B — 撤回说明"）。

【目标】把 code agent 从"server 跑得通"升级为"开发者每天能用"。

#### Track A — 交互协议 + 客户端 scaffold（5 周量级）
- ✅ **Streaming responses**（PR #33–#35）：LLM token + tool 输出按流推给客户端（HTTP/SSE）。批处理结果模式仅保留给异步任务。`LLMClient.stream()` 端到端：OpenRouter SSE 适配 → 全 6 个 decorator 透传 → `BroadcastingLLMClient` 把 chunks 通过 Redis pub/sub 转给 API → `GET /v1/tasks/{id}/llm-stream` SSE 端点 → CLI / VS Code 实时打印。Graph 默认走 `aggregate_stream_to_response` 助手
- ✅ **Inline permission protocol**（PR #37–#38）：`PermissionGate` Port + `InMemoryPermissionGate` / `RedisPermissionGate`。worker 端 `gate.request()` 阻塞 120s 等回应；客户端从 `GET /v1/tasks/{id}/permissions/stream` 收到 prompt → 渲染 → `POST /v1/tasks/{id}/permissions/{prompt_id}/decide` 回执。和 γ-A 的 `AWAITING_APPROVAL`（异步操作员审批）共存而非互斥
- ✅ **Session / Conversation 模型**（PR #39）：`POST /v1/tasks` 自动 upsert session 行；worker 加载同 session 历史 task 的 (user_prompt, assistant_message) 注入下一轮 graph state；shell_agent / simple_chat 在 first plan 前 prepend；`GET /v1/sessions/{id}` + `/messages` 暴露重建后的对话线
- ✅ **VS Code 插件 v0**（PR #41 + 后续 #48 diff-review + #49 trajectory webview）：`clients/vscode/` TypeScript extension。`metaAgent.run` / `metaAgent.tail` 两条命令，三个 settings（apiUrl / token / permissionMode）。Edit prompts 进 side-by-side diff webview；其余进 `showWarningMessage` modal；每个任务一条 trajectory 时间轴
- ✅ **CLI v0**（PR #36）：`python -m meta_agent.cli {submit|tail|run}`；env-driven config；exit-code taxonomy；stdout = LLM 输出，stderr = 控制流；`--no-interactive` 绕过 prompt 处理

#### Track B — 撤回说明

原计划自建 SWE-bench harness。落地过程中（PR #42–#47、#50–#52）连续真跑 gate 暴露多类 harness-level 实现细节：dataset 字段的 HF 字符串 quirk、Django/sympy/pytest 各自不同的 test runner + selector 格式、conda env activation 路径、test_patch 解析陷阱……每一处都是上游 `swebench` 库已经处理过的事，我们在重新发明。

**正确分工**（参考 Anthropic Sonnet 49% on Verified 的 [engineering post](https://www.anthropic.com/engineering/swe-bench-sonnet) / Aider / OpenHands）：
- 自己写 **agent 本身**——Anthropic 只用了 2 个 tool（Bash + str_replace_editor）就到 49%；我们的 shell_tool + edit_write 已经在同一坐标系
- **打分整体委托给上游**：`pip install swebench` → 产 `predictions.jsonl` → `python -m swebench.harness.run_evaluation`。OpenHands 的 `evaluation/benchmarks/swe_bench/` ~500 行胶水，无自写 evaluator

**跑分时机延后到 base agent 经过真实使用验证之后**。届时基准选择不再默认 SWE-bench Verified：OpenAI 2026 已停报 Verified（[原因](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)：59% 题目测试有缺陷 + 全部前沿模型 verbatim 复现 gold patch 的训练污染），改报 SWE-bench Pro / Terminal-Bench / LiveCodeBench 等；选哪个等做时定。

【从 δ-2 提前落地】
- ✅ **Plan mode**（PR #40）：`PermissionMode.PLAN` —— shell_agent 在 `tool_call` 节点对整个 planning step（assistant 内容 + 该轮所有 tool_calls）只发 1 个 gate prompt；客户端一次 approve 整批执行，deny 则全数 skip 并把理由喂回 model 重规划

退出条件：
- ✅ VS Code 插件可实时看 agent 工作、能 inline approve（多并行 session 在 scaffold 内可达；UI 列表 / 切换是 δ-2 的 "resume conversation" 任务）
- ✅ CLI 在终端内显示 streaming + 接受 inline prompt
- 〜 **跑分相关退出条件移至独立阶段**（base agent dogfood 验证后再开 PR）

### Phase δ-2 — Daily UX 升级（4-5 周）
【目标】在 δ-1 的客户端 scaffold 上把日常 dev 体验做完整。

- **Diff review UI**：VS Code WebView，hunk 级 accept / reject。当前 edit tool 返回结构化 patch，但 VS Code 端只是渲染为 plain prompt；本项目要把 patch 渲染为 side-by-side diff + 让用户按 hunk 接受
- **Rich trajectory viewer**：时间轴 panel，按 step 钻入 LLM 消息 / tool 调用 / cost。当前 CLI 是行级 streaming；trajectory API（γ-B-1）能给完整数据，本项目把它做成 IDE-friendly 渲染
- **Workspace browser**：worktree → IDE file tree 视图
- **Resume conversation**：UI 列出最近 session 并点开恢复（session 模型 / `GET /v1/sessions/{id}/messages` 已在 δ-1 落地；本项目是 IDE 侧的 list + click-to-attach）

【已提前落地】
- ✅ **Plan mode** —— δ-1 PR #40 借用现成 PermissionGate 走 batch-approval 路径实现；规格原计划的"`plan_pending` 节点 + 用户编辑 plan → 提交"是更深一版（用户可在 approve 前修改 plan 文本），尚未做

退出条件：
- 日常 bug-fix 任务在 VS Code 里完成"提任务 → 看 plan → 编辑 plan → 看 diff → accept hunks → push" 全流程，无需打开浏览器

### Phase δ-3 — 协作 / 集成（4 周）
- **AGENTS.md 项目级 memory**：worker plan 前读 repo 根 AGENTS.md merge 进 system prompt
- **PR review comments → 反馈**：GitHub webhook → 拉 review comments → 作为 follow-up 任务输入回 agent（复用 γ-C 的 `_human_feedback` 通道）
- **BYO LLM 配置面**：per-user key + 模型 / 路由 override，IDE 内可改
- **MCP Server**：暴露 submit_task / get_task / get_result / list_tasks + audit / usage Resource；独立进程，复用 API 层 deps

退出条件：
- 第三方 host（Claude Code、Cursor）可通过 MCP 调起一个 task 并 poll 到 result
- BYO key 走通至少 OpenRouter / Anthropic / OpenAI 三家

### Phase ε — 企业部署形态

【状态】outbound webhook 已在 γ-B-2 落地；本阶段范围收窄到 K8s / observability / SSO 三块。

【目标】把产品从 dev compose 推到可被企业 IT 部署的形态。

关键交付项：
- **K8s Helm chart**（API ×N + Worker ×M + Outbox dispatcher singleton；Postgres / Redis 引用外部托管）
- **Prometheus metrics 接出** + Grafana 模板 + 关键 alert 规则
- **OpenTelemetry traces 接出**（trace_id 全链路骨架已有，补 OTel exporter）
- **Sentry 错误上报**
- **SSO / OIDC 接入**（α 留过 Port，本阶段实现）
- **RBAC**：roles + scopes，不仅 tenant_id
- **API key 管理面**：轮换 / 撤销 / scope
- **Web UI v0**：管理面 / 异步任务面 / 成本看板

退出条件：
- K8s 单集群可部署，API / Worker 多副本在滚动重启下不丢任务
- SSO 接入至少 Okta + Azure AD 两家
- Web UI 可独立完成"看任务列表 / 看 trajectory / 审 AWAITING_APPROVAL / 看 tenant 成本"四件事

### Phase ζ — 沙箱深度 + 合规面（后置）
【目标】为强合规客户（金融 / 医疗 / 政府）准备的硬底盘升级。本阶段触发条件：实际客户合规审计要求出现。

候选交付项：
- gVisor / Firecracker 沙箱替换 Docker
- 第三方安全审计准备包（数据流图、威胁建模、SOC2 控制对照）
- PG 备份 + DR 文档 + RPO/RTO 验证
- 数据驻留 / 跨地域复制策略

### 节奏说明
- 单段建议 2-3 周；单段内拆 3-5 个独立 PR
- α 是其余阶段的安全底座，必须先落
- β+ 在 β 之后、γ 之前；β+ 与 γ 不可并行（γ 的 checkpoint 设计依赖稳定的 tool 集合与 prompt 版本作为录入参考）
- **δ-1 Track B 已撤回**；自建 harness 是错误方向，跑分阶段延后到 base agent dogfood 验证之后再做（届时用上游 `swebench` 库 + 薄胶水）
- δ-2 / δ-3 必须在 δ-1 之后，因为它们消费 δ-1 的客户端 scaffold + protocol
- ε 与 δ-1 / δ-2 / δ-3 可并行（ε 是部署形态，与 agent 能力面相互正交）
- 每段开始前先做最小化探索，确认范围与最小子集，再进入实现
- 本节奏不替代 L0–L3 的优先级分层；冲突时以 L0 约束优先

## 当前状态标注要求
凡涉及目录结构、命令入口、模块边界、部署清单，必须明确区分：
- 当前已实现
- 目标结构
- 候选方案

禁止把目标态描述成当前仓库事实。
