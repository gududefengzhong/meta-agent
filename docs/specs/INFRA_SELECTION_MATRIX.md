# INFRA_SELECTION_MATRIX.md - 基础设施选型对比

## 文档定位
本文件提供 Phase 0 启动前需要拍板的基础设施选型对比，覆盖：
1. 任务队列
2. 分布式限流
3. 分布式熔断

本文件状态：【目标】5 项决策已拍板，结论已写入 `AGENT_SPEC.md` 的「横切基础设施选型」；本文件保留对比表与决策记录，作为后续选型调整的依据。各候选方案的对比维度仍为参考资料，不再视为「候选」。

评估维度统一：吞吐 / 顺序保证 / 持久化 / 多租户友好度 / 运维成本 / 与现有依赖契合度 / 适用规模。

倾向意见仅为建议，最终以你拍板为准。

## 1. 任务队列

### 候选方案对比

| 维度 | Redis Streams | NATS JetStream | Kafka |
|---|---|---|---|
| 吞吐 | 中（万级 msg/s 单实例） | 高（十万级 msg/s） | 极高（百万级 msg/s 集群） |
| 顺序保证 | 单 stream 内有序 | JetStream 内有序 | 分区内强有序 |
| 持久化 | 内存为主，AOF 可持久 | 文件持久 | 默认强持久 |
| 消费模型 | Consumer Group + XAUTOCLAIM 实现租约 | Pull + Ack + 重投递 | Consumer Group + offset |
| 多租户友好度 | 按 stream 分租户简单 | Subject hierarchy 天然适配 | 按 topic / key 分租户 |
| 运维成本 | 低（已有 Redis） | 中 | 高（KRaft / ZK、磁盘规划） |
| Python 客户端 | redis-py 成熟 | nats-py 成熟 | aiokafka / confluent-kafka 成熟 |
| 与现有依赖契合度 | 已使用 Redis 作缓存 / 限流，复用成本最低 | 需新增依赖与运维 | 需新增依赖与较重运维 |
| 适用规模 | PoC → 中等规模 | 中等 → 较大规模 | 大规模、强可靠场景 |

### 决策结论
- 【目标】Phase 0–3 默认采用 **Redis Streams**。理由：与 Redis（限流 / 会话短期态）复用、运维边际成本最低、足以支撑 L1 主链路验证。
- 【目标】Phase 4+ 公司级推广阶段保留迁移到 **NATS JetStream 或 Kafka** 的能力；队列接口以抽象 Port 形式存在，业务代码不直接调用驱动。

## 2. 分布式限流

### 候选方案对比

| 维度 | 应用层 Redis 令牌桶 | 网关全局限流（APISIX / Higress / Envoy） | 控制面方案（Sentinel 等价 / pyrate-limiter + 中心后端） |
|---|---|---|---|
| 限流粒度 | tenant / user / task_type / model / tool 任意维度 | URI / header / 简单维度 | 多维度，规则中心化 |
| 实施位置 | 应用代码内 | 入口层 | 应用代码内 + 控制面 |
| 跨副本一致性 | 强（Redis 原子操作 / Lua） | 强（网关层集中决策） | 取决于后端 |
| 业务感知 | 高（结合任务上下文） | 低 | 高 |
| 与熔断 / 降级联动 | 容易（同进程） | 难 | 容易 |
| 部署复杂度 | 低 | 中（需网关治理） | 中高 |

### 决策结论
- 【目标】**粗粒度限流**（URI / IP / Tenant 入口流量）放在网关层，与认证一并处理。入口网关采用 **Higress**。
- 【目标】**细粒度限流**（tenant + task_type + model + tool 组合）放在应用层，默认实现 **Redis 令牌桶**；以 `RateLimiter` Port 抽象，便于替换。
- 【目标】Phase 0–3 不引入完整控制面方案，避免过早治理化。

## 3. 分布式熔断

### 候选方案对比

| 维度 | 应用层（pybreaker / aiobreaker）+ Redis 共享统计 | 服务网格层（Istio / Envoy outlier detection） | 自研基于 Redis 滑动窗口 |
|---|---|---|---|
| 触发粒度 | 外部依赖维度（OpenRouter / Git / MCP / 对象存储） | 实例 / 服务维度 | 任意自定义 |
| 跨副本一致性 | 中（依赖共享存储更新） | 高 | 高 |
| 与重试 / 降级联动 | 容易 | 一般 | 容易 |
| 实施成本 | 低 | 中（需服务网格） | 中 |

### 决策结论
- 【目标】**外部依赖熔断**（OpenRouter / Git Provider / 外部 MCP / 对象存储）应用层使用 `pybreaker`，本地为快路径，**统计与状态在 Redis 上做最终一致汇总**，并提供显式 fallback。
- 【目标】**服务间内部熔断**交给服务网格 / 网关，不写进业务代码。
- 【目标】熔断与限流共用一致的 Port 抽象，避免实现散落。

## 4. 综合结论

| 维度 | Phase 0 默认【目标】 | 长期演进【目标】 |
|---|---|---|
| 任务队列 | Redis Streams | 视规模迁移到 NATS JetStream / Kafka |
| 入口网关与入口限流 | Higress | 同 |
| 应用限流 | Redis 令牌桶 + `RateLimiter` Port | 视规模引入控制面 |
| 熔断 | `pybreaker` + Redis 共享统计 | 服务网格联动 |
| 抽象约束 | 队列 / 限流 / 熔断均以 Port 抽象，业务代码不依赖具体驱动 | 同 |

## 5. 决策记录
- [x] 任务队列：Redis Streams 作为 Phase 0 默认 —— **接受**
- [x] 入口网关：在 APISIX / Higress / Envoy 中 —— **选定 Higress**
- [x] 应用限流：Redis 令牌桶作为默认实现 —— **接受**
- [x] 熔断：`pybreaker` + Redis 滑窗作为默认实现 —— **接受**
- [x] 队列 / 限流 / 熔断均以 Port 抽象 —— **接受**

结论已写入 `AGENT_SPEC.md` 的「横切基础设施选型」段落。后续如有调整需在 PR 中显式回溯本节并更新决策记录。
