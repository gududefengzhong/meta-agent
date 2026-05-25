# EVAL_BASELINE — meta-agent 内部 SWE-bench 评测基线

## 这份文档是什么

定义 `eval/swebench/` 重建后**"跑稳"的具体可观察标准**。
**不是**一份计划文档（什么时候做什么），而是一份**契约**：每条标准都对应一个"做完了 / 没做完"的可测信号。

这份文档的存在动机：上一轮自建 SWE-bench harness（PR #42–#52，已由 #53 撤回）失败的根因不是"代码写错了"，而是**没有 SLA**——每个 PR 都只是"再修一处看看 gate 红不红"，永远在追下一个 bug 而不知道"够好了"是什么意思。本文档提供那把尺子。

## 不在范围内的事

- 拿行业可比 pass@1（SWE-bench Verified leaderboard）—— 那是别人的事，我们不追
- 全量 SWE-bench Verified（500 instance）—— 一开始不做，N 阶段才会到
- SWE-bench Pro —— 完全不在本基线范围
- 上游 `swebench` PyPI 包 —— 暂不引入；但若任何一条标准用"自己写"成本明显高于"换上游"，回头评估
- 评测**生成 agent**（meta-agent 本身好不好）—— 那是另一个 loop；本基线只确保**测量工具**可信

## 为什么是 Lite/Verified 而不是 Pro

- Pro 是为前沿模型设计的难度；我们 base agent 在最简单档都没经过真实压测，直接冲 Pro 拿不到信号（多半全 0%）
- Lite 全是 pytest-friendly 的小 instance，避开 Django/sympy 这类非 pytest runner（这是上一轮卡得最久的地方）
- 50–100 个 Verified instance 跑稳 = 模型/prompt 变化能看出 5% 以内 delta = 足够支撑 agent 内部 dev iteration

## 五条"跑稳"标准（按依赖顺序）

### 标准 1：dataset 固定 + 可复现

**对外承诺**：任何时候执行 `python -m eval.swebench list --dataset <path>`，对同一 dataset 文件输出**字符级一致**。

**可测信号**：
- 内置 fixture（`eval/swebench/fixtures/instances_sample.json`）有明确的 commit hash 来源（commit message 或文件头部记录"From `princeton-nlp/SWE-bench_Lite` test split, hash=XXXX"）
- 加载顺序 deterministic（不依赖 dict iteration / hash randomization）
- 任何"从 HuggingFace 拉新数据"必须落本地 JSON 后再 commit；运行时**不联网拉 dataset**

**反例（不算稳）**：每次 run 时 `load_dataset("princeton-nlp/...")` 联网取——因为 HF 上游可能改 schema、加列、修 patch 字段，破坏复现性。

---

### 标准 2：模型 + prompt 固定 + 进 report

**对外承诺**：评测 report JSON 中**必须**包含以下字段，缺一不可：
- `model`: 实际调用的模型 ID（如 `deepseek/deepseek-chat`），CLI 强制 `--model` flag，不能省略
- `prompt_version`: prompt 文件的 git SHA 或内容 SHA-256 前 12 位
- `dataset_snapshot`: 用的 dataset 文件的 SHA-256 前 12 位
- `harness_version`: `eval/swebench/` 模块在跑这次评测时所在的 git commit SHA

**可测信号**：
- 同 model + 同 prompt_version + 同 dataset_snapshot 的两次 run，**resolved 集合应该完全一致**（LLM 不确定性除外——见标准 3）
- 任何 prompt 改动**必须**走 git commit；不接受"我临时在 worker 改了 system prompt 看一下"这种实验

**反例**：report 里只有 `instance_id` 和 `resolved`，不知道哪个模型、哪版 prompt——这种 report 对比起来是无源之水。

---

### 标准 3：LLM 不确定性受控

**对外承诺**：所有评测 run 强制 `temperature=0`（或当前模型支持的最低不确定性配置），且记录这个值进 report。

**可测信号**：
- 同 (model, prompt_version, dataset_snapshot, temperature=0) 跑两次，**resolved 集合一致率 ≥ 95%**（剩余 5% 容差给模型推理的微小不确定性）
- CLI 默认 `--temperature 0`；要测温度影响请显式 `--temperature 0.7`

**反例**：每次跑结果飘 10%，无法判断 prompt 改动是真改善还是噪声。

---

### 标准 4：report 格式 deterministic + 易 diff

**对外承诺**：连续两次同 input 跑出的 report JSON，`diff` 应该**只**显示真实 diff（resolved 状态改变、step 数变化），**不**显示：
- 字段顺序变化
- 时间戳（`duration_seconds` 这种允许，但 `timestamp` / `run_started_at` 不要）
- 浮点尾数（`pass_at_1` 保留 4 位小数即可）
- instance 顺序变化（按 `instance_id` 排序输出）

**可测信号**：
- `python -m eval.swebench run ... --report-path a.json && python -m eval.swebench run ... --report-path b.json && diff a.json b.json` 在 trivial 改动下应该输出空或 1-2 行
- report 字段顺序由 pydantic model 定义，不由 dict 序列化顺序决定

**反例**：每次 report JSON 字段乱序，diff 100 行噪声里淘 3 行真信号。

---

### 标准 5：scope 显式 + 失败不静默扩散

**对外承诺**：
- `test_specs.py` 维护**允许评测的 (repo, version) 白名单**
- 遇到不在白名单的 instance，行为是 **"跳过 + 记入 report.skipped 列表"**，**不是** "尝试 + 默默失败"
- v1 白名单只放 pytest-friendly 仓（`psf/requests` / `pytest-dev/pytest` / `pallets/flask` / `sphinx-doc/sphinx` 等），**Django/sympy 暂不加入**

**可测信号**：
- report 顶层有 `skipped` 数组，每条带 `instance_id` + `reason`（"no test spec for django/django v3.2"）
- 跑全量 Verified 不会因为某个 instance 没 spec 而崩溃
- 加新仓 = 加新 spec + 加 parser + 加单元测试（不允许"现 mock 一下先跑起来"）

**反例**：碰到 Django instance silently 报 missing → pass@1 看起来低 30% → 不知道是 agent 不行还是 harness 没覆盖。

---

## 退出条件（什么时候算"跑稳了"）

**全部满足**才算这阶段完成：

- [x] 标准 1：固定 dataset 加载到 fixture，运行不联网（PR #55；CLI 不联网取数）
- [x] 标准 2 ↗ 部分：`dataset_snapshot` + `harness_version` 字段（本 PR）；`model` + `prompt_version` 在 agent path 回归后补
- [ ] 标准 3：连续 2 次同输入 run，resolved 集合一致率 ≥ 95%（依赖 agent path）
- [x] 标准 4 ↗ 部分：InstanceResult pydantic 字段顺序 deterministic（PR #55）；连续 2 次同输入完整 diff 验证依赖 agent path
- [x] 标准 5：pytest-only 白名单生效；非白名单 instance 通过 `TestSpecNotFoundError` 显式报错（PR #55）
- [ ] 跑通的 instance 数 ≥ 5，gold patch pass@1 = 100%（当前 1/1 验证：psf__requests-2317 in #56）
- [ ] 同样 instance 用一个简单 baseline agent（甚至单步 LLM 直接吐 patch）跑出**非零** pass@1（哪怕 5%）—— 证明 agent path 真接通了，不只是 gold path

---

## 不写进退出条件的事（明确不属于本阶段）

| 条目 | 推到什么时候 |
|---|---|
| Django / sympy runner 支持 | 跑稳之后下个阶段；现在加 = 标准 5 立刻破 |
| Batch checkpoint/resume | 第一次 batch 跑挂之后；现在 5-10 instance 跑完 30 分钟不需要 resume |
| Cost / latency / 失败分类细化 report | 拿到 baseline 数字之后；现在多字段没数据填 |
| CI 集成（PR 自动跑） | 全部 5 条标准满足之后；不稳的 gate 红 = 推 PR 时心智成本 |
| Pro / Terminal-Bench / 其他基准 | 本基线之外的事 |
| 跟上游 swebench leaderboard 对比 | 永远不在范围内（本基线明确不追） |

---

## 工程姿态备注

- 本文档**先于代码 commit**。代码恢复 PR 引用本文档作为 acceptance criteria，每个标准对应至少一个单元 / 集成测试
- 任何"我觉得标准 X 太严了" / "标准 Y 太松了"的反馈，**改文档**而不是改实现去绕开
- 上一轮的教训：**没有 SLA 的工程 = 永远在 chase 下一个 bug**。本文档存在的全部意义就是给那条线设界

---

## 已知环境敏感性（不计入"不稳"）

SWE-bench 上游有少量测试是**网络环境敏感**的，结果不只取决于 patch 是否正确，还取决于跑测试的网络栈：

- **DNS 行为**：`psf/requests` v2.4 的 `test_connection_error` / `test_connect_timeout` / `test_total_timeout_connect` 这类测试假定不存在的域名（如 `fooobarbangbazbing.httpbin.org`）会**解析失败**。但很多 ISP / VPN / corp DNS / Docker Desktop 配置会**劫持**不可解析域名到一个 502 的兜底 IP（典型 `198.18.0.x` 段），导致这些测试在你本地跑不出预期的 `ConnectionError` → fail。
- **外部 HTTP 服务**：少数测试依赖 `httpbin.org` 真实响应；服务降级 / 限流期间结果不稳。

**怎么算"跑稳"**：

- 同环境下，gold patch 的 **FAIL_TO_PASS 全部 passed** + **PASS_TO_PASS 非环境敏感子集 100% passed**
- 环境敏感的 PASS_TO_PASS 失败**不算 harness 失败**，但需在 report 里能识别出来（按 selector 名字 + 既知 known-flaky 清单）
- 整体退出条件里的"gold patch pass@1 = 100%"理解为"**所有非环境敏感 selector** pass"

这条不是宽容标准，是诚实承认：**有些上游测试在 isolated CI 环境之外不可重复**。等真有人需要跑到 100% 时再投入解决 DNS / network namespace 隔离，本阶段不投入。

---

## 调试入口（不计入退出条件，但每个 eval CLI 必须有）

- ``--log-test-output PATH`` 把 runner 的 raw stdout+stderr 落地。上一轮 gate 卡在 "all selectors missing" 时，**没有这个文件就只能 docker exec 复现一次**。任何重写 CLI 都必须先暴露这个口子
