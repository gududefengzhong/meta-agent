# CONTEXT_PROPAGATION.md - 上下文与标识传播契约

## 文档定位
本文件定义企业级 Code Agent 系统中 **跨进程、跨服务、跨存储** 必须贯穿的标识集合及其传播规则。

本契约是 L0 约束的一部分；任何违反本契约的实现视为违反「多租户、可追踪、成本可见」等不可妥协约束。

本文件状态：【目标】契约定义全部条款；【目标】SDK 实现；【候选】具体 header 与列名沿用第 2 节命名，最终以 Phase 0 落地版本为准。

## 1. 必须贯穿的标识集合

| 名称 | 含义 | 生成方 | 生命周期 | 必填 |
|---|---|---|---|---|
| `tenant_id` | 租户标识；多租户隔离的根 | 鉴权层从 token / SSO 主张中解析 | 请求级 | 是 |
| `principal_id` | 实际发起人（用户 / 服务账号） | 鉴权层 | 请求级 | 是 |
| `session_id` | 长会话标识；可跨多个任务 | 客户端或会话服务在首次交互时生成 | 跨任务，可持久 | 是（人交互场景） |
| `task_id` | 单个任务标识；用于异步执行与状态回查 | 任务提交服务 | 任务生命周期 | 是 |
| `trace_id` | 分布式 Trace 根标识；与 W3C Trace Context 对齐 | 入口层；缺失则生成 | 单次请求 / 任务执行 | 是 |
| `span_id` | OTel Span 标识 | OTel SDK | Span 生命周期 | 是 |
| `parent_span_id` | OTel 父 Span 标识 | OTel SDK | 由调用关系决定 | 视情况 |
| `idempotency_key` | 副作用调用幂等键 | 客户端 / 网关 | 单次副作用窗口 | 副作用接口必填 |
| `request_id` | 单次 HTTP / RPC 请求标识 | 接入层 | 单次请求 | 是 |

约定：
- 上述标识统一为字符串，建议 ULID 或 UUIDv7；`trace_id` / `span_id` 遵循 OTel 长度要求。
- `tenant_id` 不可被业务层覆写；`session_id` 不应跨租户复用。
- 缺失必填标识时入口层拒绝请求；下游模块假设其存在。

## 2. 传播媒介

### 2.1 HTTP / WebSocket（候选 header 命名）
- `X-Tenant-Id`
- `X-Principal-Id`
- `X-Session-Id`
- `X-Task-Id`
- `X-Request-Id`
- `Idempotency-Key`
- Trace 相关 header 遵循 W3C Trace Context（`traceparent`、`tracestate`），不另起名。

### 2.2 消息队列
消息属性（headers / attributes / metadata）携带同名字段；消息体只承载业务负载，标识统一在元数据。

### 2.3 数据库
任务、会话、审计、计费、Outbox 等核心表必须包含：
- `tenant_id`、`session_id`（可空）、`task_id`（可空）、`trace_id`、`created_by`、`idempotency_key`（副作用表必含）。

### 2.4 日志 / 指标 / Trace
- 结构化日志键名固定：`tenant_id`、`session_id`、`task_id`、`trace_id`、`span_id`、`request_id`。
- 指标标签覆盖 `tenant_id`、`task_type`、`model`、`tool`；高基数标签（如 `task_id`）不进指标。
- OTel Span 必须带 `tenant_id`、`session_id`、`task_id`（存在时）。

## 3. 进程内传播
- 入口层将上下文写入 `contextvars`；下游模块通过统一访问器读取。
- 仓储层在写入主表前 **强制断言** `tenant_id` 存在；缺失视为编程错误而非运行时异常。
- 跨线程 / 跨协程切换必须显式传递上下文，禁止依赖隐式共享。

## 4. 跨服务传播
- 出站 HTTP / RPC / MQ 必须自动注入上述 header / 属性，由统一中间件完成。
- 入站统一中间件解析并写入 contextvars；不允许业务层手动解析。
- 外部依赖（OpenRouter、Git Provider、外部 MCP、对象存储）的调用日志须保留 `trace_id` 以便回溯。

## 5. 持久化与回放
- 任务 Checkpoint 必须包含完整上下文快照。
- 审计与计费事件必须可凭任一标识（`trace_id` / `task_id` / `session_id` / `tenant_id`）回查。
- 长任务恢复时上下文从 Checkpoint 重建，禁止从临时内存重建。

## 6. 不允许的做法
- 在业务代码中读取或写入除上述键名以外的「等价别名」。
- 把 `tenant_id` 仅放在 URL 路径或请求体而不写入上下文。
- 仅依赖单进程内存保存上下文；任何持久化路径必须落库或落消息属性。
- 在日志中拼接 ID 字符串而不使用结构化字段。

## 7. 违反约定的处理
- 单元测试与 lint 检查覆盖关键中间件与仓储层。
- PR Review 必须验证新增接口和表是否遵守本契约。
- 出现违反情况按 L0 约束处理，不得延后修复。
