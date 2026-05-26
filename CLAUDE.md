# CLAUDE.md - 仓库协作与开发规范

## 文档角色
本文件只定义本仓库中的开发协作规则、实现约束和交付流程。

产品目标、能力边界、阶段优先级、交付形态统一以 `docs/specs/AGENT_SPEC.md` 为准。
通用开发哲学统一参考 `docs/specs/ANDREJ_KARPATHY_PRINCIPLES.md`。

如果三者出现冲突，优先级如下：
1. `CLAUDE.md`：当前仓库的协作与交付规则
2. `docs/specs/AGENT_SPEC.md`：产品规格与能力边界
3. `docs/specs/ANDREJ_KARPATHY_PRINCIPLES.md`：通用开发原则

## 开发方式
- 大任务默认先做规划，再进入实现。
- 优先小步迭代，避免一次性铺开整套系统。
- 每次改动都要说明目标、影响范围和验证方式。
- 只修改完成当前目标所必需的代码和文档。
- 重要设计决策、破坏性调整、对外接口变更需要人工确认。

## 会话边界与交接
- 不要默认把多个里程碑串在一个超长线性会话里；优先以 commit、阶段里程碑或明确子任务作为新会话边界。
- 不要机械地频繁切换会话。新会话的上下文重建是一次性成本，长会话的上下文膨胀是持续成本；只有在后续仍有明显工作量时，阶段性切换才通常更划算。
- 开启新会话时，默认提供最小必要交接信息：当前目标、起点 commit/branch、已做出的关键决策、建议关注的模块或文件、预期验证范围。
- 如果当前只剩少量收尾工作，且切换后仍需重新探索较大范围上下文，则优先留在当前会话完成。
- 如果当前会话已经积累了大量历史、长文件阅读记录或重复验证输出，优先切到新会话继续，而不是在原会话中无限追加上下文。
- 跨会话延续时，应先恢复"当前要做什么"和"哪些结论已固定"，不要先做大范围重复探索。

## 成本控制默认策略
- 新功能或新阶段默认先做最小化探索：先定位入口、相关模块和已有测试，再进入实现。
- 优先使用搜索、局部阅读和定向 diff；不要一上来读取大量整文件，除非局部信息不足以判断改动边界。
- 实现路径明确后直接落地，不要反复请求确认；只有在设计边界、破坏性变更或范围明显外溢时才停下来确认。
- 默认避免重复验证同一套内容；除非上一轮失败需要复验，否则改动完成后只做一次必要校验。
- 不要把"顺手补充"的重构、文档扩写、额外集成测试或下一里程碑工作混入当前目标，除非用户明确要求。

## 新会话输入建议
- 建议使用"目标 + 起点 + 限制 + 验证"四段式输入，而不是粘贴整段旧会话历史。
- 推荐最小格式：
  - 本轮目标是什么。
  - 从哪个 commit 或当前工作树状态继续。
  - 哪些决定已经固定、这轮不要再争论。
  - 这轮希望控制的范围和验证强度。
- 如果当前 milestone 已完成、但下一步尚未明确，不要强行指定后续 roadmap；新会话的首要目标应是基于当前代码、规格和最近状态判断"最合理的下一步是什么"。
- 此时默认要求 agent 先做最小化探索，列出少量候选下一步，说明各自收益、风险、前置条件，并推荐一个"最小但推进最大"的选项，而不是直接发散到远期大计划。

## 实现约束
- 默认使用 Python 3.11+。
- 默认保持强类型和清晰的数据边界，优先使用 `pydantic v2` 风格的数据模型。
- 核心编排优先按 LangGraph 风格的状态流设计。
- 核心业务逻辑应独立于具体客户端，避免把 Claude Code、Cursor 或其他宿主的交互细节直接耦合进核心域模型。

## 代码与运维规范
- 保持完整类型注解；公共接口和关键模块需要 docstring。
- 禁止硬编码密钥、令牌和租户敏感信息。
- 生产路径禁止使用 `print`，统一使用结构化日志。
- 日志、审计、计费、任务记录等链路中，必须保留 `tenant_id`、`trace_id` 等关键上下文。
- 涉及失败重试、外部调用、异步执行的路径，要显式处理错误模型和重试策略。

## 交付要求
- 改动必须附带对应的验证说明；能自动化验证的优先自动化。
- 涉及核心行为变更时，需要同步更新相关文档和 test。
- 文档里如果描述的是"目标态"而非"当前已实现状态"，必须明确标注，避免把规划写成现状。
- 不要在未落地的情况下把目录结构、命令入口、部署方式写成既成事实。

## 禁止事项
- 不要直接把所有需求揉成一个超大实现。
- 不要跳过多租户、安全、计费、审计这些基础约束去追求表面功能完成。
- 不要顺手重构无关模块。
- 不要让演示层、宿主层接口反向污染核心业务抽象。

## 参考文件
- 产品规格：`docs/specs/AGENT_SPEC.md`
- 开发原则：`docs/specs/ANDREJ_KARPATHY_PRINCIPLES.md`

## 当前状态
<!-- 阶段进度高层快照。详细 phase 段落见 docs/specs/AGENT_SPEC.md。 -->

### 产品定位
**Bug Fix CLI Agent** —— 输入 bug 描述 + 仓库，agent 用 plan-act-observe 循环读代码、改文件、跑 verifier、产 PR。CLI 是主交付形态；架构（graph + tool registry）可扩展到 Test Generation / CI Triage / Workflow Automation 等同形态自动化场景。

### 已落地的能力栈
- **α 安全生产线**：`RateLimiter` + `CircuitBreaker`（pybreaker + Redis 共享统计）+ 入口鉴权 + `Secrets` Port + 任务级 budget gate + 审计/计费查询 API
- **β tool-use loop + 容器沙箱**：`shell_agent` plan → tool_call → observe loop（含 `max_steps` / `max_total_tokens`）；4 个 Tool Ports（FS / Edit / Shell / Test）+ local 与 docker 双实现；多语言 verifier（Python ruff+pytest / TypeScript tsc+vitest）
- **β+ Agent 能力深度**：`CodeIndex`（tree-sitter + pgvector embeddings）+ `code_search` / `get_definition` / `get_references` / `outline` 4 个 retrieval tools；`WebFetch` / `DocSearch`；`LLMRouter` 按 `step_kind` 选模型；prompt 版本化（content hash → 注入 audit + usage logs）
- **γ 信任面 + 长程恢复**：Checkpoint 续跑；4 档 `PermissionMode` + 3 档 `BudgetPolicy` 正交组合；`human_gate` + `AWAITING_APPROVAL`；approve / abort API + 结构化反馈注入下一轮 plan；trajectory API（audit + checkpoints + usage 三表 JOIN 时间轴）；Outbox + webhook（HMAC + 退避 + 去重 + 死信）；per-task cost view by `step_kind` × `model`；prompt redaction layer；30 天 sweeper
- **δ-1 客户端 scaffold**：`LLMClient.stream()` 端到端 SSE（OpenRouter → 6 个 decorator 透传 → Redis pub/sub broadcaster → `GET /v1/tasks/{id}/llm-stream`）；`PermissionGate` Port + Redis 实现；Session 模型 + 历史消息线注入下一轮；CLI v0（submit / tail / run）

### 三条 L1 主链路
- `builtin.bug_fix_v2` / `builtin.bug_fix`：plan / patch / verify / push / finalize；git worktree 内修改 + commit + push；replan；多语言 verifier
- `builtin.code_review`：prepare / review / finalize，pure-LLM，Pydantic schema 严格校验输出
- `builtin.auto_pr`：prepare / publish / finalize + `GitProvider` Port（FakeGitProvider + GitHubGitProvider）；`BUG_FIX → AUTO_PR` follow-up chain

### 故意不做的事
- VS Code 插件（曾做过，定位收窄到 CLI 后移除）
- 跑分 / SWE-bench harness（外部 benchmark 与产品价值正交，不在交付范围）
- 多 Agent 编排 / Hooks / 插件机制（L2 平台层，超出 focused agent 范围）
- K8s Helm / SSO / Web UI（spec 上 ε 阶段；真实部署需求出现再做）
- gVisor / Firecracker 沙箱（spec 上 ζ 阶段；强合规客户出现再做）
