# 多智能体群聊：E2E 测试矩阵、内测 Pilot、监控告警与渐进发布

> 范围：MARS-23（E2E QA + Pilot：内测、监控配置）
> 上游依据：`specs/multi-agent-chat-orchestrator.md`（PRD）、Codex 审核意见（MARS-13 评论）
> 说明：本仓库当前处于规格阶段（尚无前端/平台/API 实现代码），因此本文产出的是**可执行的验收方案与配置基线**，而非已通过的测试报告。用例的「通过」状态需在实现落地（MARS-19/20/21/22）后回填；监控规则文件 `deploy/monitoring/alerts.yaml` 可直接导入 Prometheus 兼容告警栈。

---

## 0. 验收标准追溯

| 本 issue 验收标准 | 对应本文章节 | 证据形式 | 当前状态 |
|---|---|---|---|
| 所有测试矩阵用例通过 | §1 E2E 测试矩阵 | 用例表 + 执行记录（待回填） | 待实现后执行 |
| 内测无 P0/P1 遗留 | §2 内测 Pilot | 每日缺陷分级表 + 退出门禁 | 待 Pilot 执行 |
| 监控面板可观测关键指标 | §3.2 仪表盘面板 | 面板清单 + 指标映射 | 配置已定义，待接入 |
| 告警规则生效并有文档 | §3.3 告警规则 + `alerts.yaml` | 规则文件 + 验证用例 | 规则已定义，待导入验证 |

---

## 1. E2E 测试矩阵

### 1.1 约定

- **优先级**：P0 = 阻断/安全/数据泄露/核心流程不可用；P1 = 重要功能缺陷、影响多数用户；P2 = 次要功能或边界；P3 = 体验/文案。
- **零容忍项**：越权读取、secret 透传、回复内派发指令被实际执行——任一出现即判 P0 且该版本不得进入内测。
- **执行环境**：内测工作区；测试 agent 由 MARS-21 提供（3–5 个，含不同延迟、成功/失败模式）。
- **状态列**：`未执行 / 通过 / 失败 / 阻塞`，实现落地后回填。

### 1.2 三种核心模式

| ID | 类别 | 场景与关键步骤 | 预期结果 | 优先级 | 状态 |
|---|---|---|---|---|---|
| CORE-01 | parallel | 3 个 agent 参与并行请求，发送后观察回复区 | 3 条回复并发呈现，均带来源标签（agent avatar/name/run id/status/latency） | P0 | 未执行 |
| CORE-02 | parallel | 参与者数量 = 上限 5，发送请求 | 请求被接受，5 个 run 均创建 | P1 | 未执行 |
| CORE-03 | parallel | 参与者数量 = 6（超过上限） | 请求被服务端拒绝，返回明确上限错误，无 run 创建 | P1 | 未执行 |
| CORE-04 | linear | 按固定顺序触发 A→B→C，检查 B/C 收到的上下文 | 严格串行；B 收到 A 的回复作为上下文，C 收到 B 的回复；顺序不可乱 | P0 | 未执行 |
| CORE-05 | linear | 第 2 步超时或失败 | 该步标记 failed，后续步骤按既定失败策略处理（终止或跳过），UI 显示失败点 | P1 | 未执行 |
| CORE-06 | supervisor | 主管分派 → worker 提交 → 主管 approve → completed | 完整状态流转正确，task 终态 completed，最终合成包含来源与摘要 | P0 | 未执行 |
| CORE-07 | supervisor | 主管 reject 并提供结构化反馈 → worker 重做 → 重新提交 → approve | 反馈（reject_reason/change_instructions）被 worker 可见并用于重做；attempt_count 递增 | P0 | 未执行 |
| CORE-08 | supervisor | max_attempts = 3，连续 reject 至第 3 次 | 第 3 次 reject 后 task 标记 failed，不再派发；remaining attempts 显示 0 | P0 | 未执行 |
| CORE-09 | all | 使用「汇总/合成」按钮 | 合成结果保留每条原始回复的 1–2 行摘要与来源标签，标注实际纳入的来源与未返回项 | P1 | 未执行 |

### 1.3 部分失败与降级

| ID | 类别 | 场景与关键步骤 | 预期结果 | 优先级 | 状态 |
|---|---|---|---|---|---|
| DEG-01 | parallel/linear | 单个 agent 超时 | 该 agent 标记 failed，其余结果正常合并，UI 显示超时提示 | P0 | 未执行 |
| DEG-02 | parallel/linear | 单个 agent 返回错误 | 同上，标记 failed 并继续 | P1 | 未执行 |
| DEG-03 | all | 超过 50% 参与者失败 | 返回错误并给出降级建议（降级到单一专家 / 人工接管 CTA） | P0 | 未执行 |
| DEG-04 | all | 全部 agent 失败 | 回退到安全简答并提示人工，不产生误导性合成 | P1 | 未执行 |
| DEG-05 | all | agent 在超时后才返回回复 | 迟到回复以「迟到」状态可见，但不被并入已定稿的合成；UI 提供发起新版汇总的入口 | P1 | 未执行 |
| DEG-06 | supervisor | 部分 worker 失败、部分成功、含迟到回复 | 成功项继续审核流程，失败项隔离，迟到项单独标注，汇总标注实际纳入来源 | P1 | 未执行 |

### 1.4 循环触发防护与深度限制

| ID | 类别 | 场景与关键步骤 | 预期结果 | 优先级 | 状态 |
|---|---|---|---|---|---|
| LOOP-01 | all | 构造调用链使 Coordinator/群聊服务已在链中 | 检测到链深或 seen-agent set 后不再触发新参与者，除非有明确人类指令 | P0 | 未执行 |
| LOOP-02 | all | 单请求消息数超过 per-request max_messages | 超出部分被拒绝，不产生额外触发 | P0 | 未执行 |
| LOOP-03 | all | 某 agent 回复中包含「触发另一个 agent」的指令文本 | 该文本作为不可信内容处理，不产生任何二次派发 | P0 | 未执行 |
| LOOP-04 | all | 检查审计/invocation graph | 每次触发的调用关系被记录，可查核 | P1 | 未执行 |

### 1.5 权限、私有 agent 与数据隔离（零容忍）

| ID | 类别 | 场景与关键步骤 | 预期结果 | 优先级 | 状态 |
|---|---|---|---|---|---|
| SEC-01 | all | 无权用户尝试触发私有 agent | 实际派发被服务端拒绝；preview 与派发结果一致地不触发 | P0 | 未执行 |
| SEC-02 | all | 无权用户枚举参与者列表 | 私有 agent 的存在性与在线状态均不暴露 | P0 | 未执行 |
| SEC-03 | all | preview 后、提交前撤销用户对某 agent 的权限 | 实际派发以实时权限为准，被撤销的 agent 不被触发，返回明确原因 | P0 | 未执行 |
| SEC-04 | all | 某回复输入在汇总前被撤销读取权限 | 汇总前重新校验读取权限，该输入不被纳入，汇总可见范围受输入权限约束 | P0 | 未执行 |
| SEC-05 | all | 检查 agent 间传递的上下文字段 | 仅传递授权的非敏感字段；custom_env、mcp_config、连接器配置、工具日志、私有历史均不传播 | P0 | 未执行 |
| SEC-06 | all | 检查 delegation 的 invoke gate | 发起链顶为人类身份，遵守现有 invoke 授权规则 | P0 | 未执行 |
| SEC-07 | all | 汇总输出的事实核对 | 输出引用可核对来源，保留分歧，95% 测试对话不引入超出原始回复事实的新信息，不含新敏感信息 | P1 | 未执行 |

### 1.6 trigger-preview 一致性

| ID | 类别 | 场景与关键步骤 | 预期结果 | 优先级 | 状态 |
|---|---|---|---|---|---|
| PV-01 | all | 对比 preview 展示的 agent chips 与实际触发的 run | 完全一致，返回 trigger_outcomes | P0 | 未执行 |
| PV-02 | all | 参与者中存在不可触发项（权限不足/离线） | preview 给出明确原因，不隐瞒 | P1 | 未执行 |
| PV-03 | all | preview 的成本提示与实际计费对比 | 预估与实计费在约定容差内一致 | P2 | 未执行 |

### 1.7 幂等、恢复与成本

| ID | 类别 | 场景与关键步骤 | 预期结果 | 优先级 | 状态 |
|---|---|---|---|---|---|
| RST-01 | all | 客户端用相同 idempotency_key 重复提交 | 不重复创建请求，返回同一 request_id 与快照 | P0 | 未执行 |
| RST-02 | all | 入队确认丢失后重发 | 下游幂等键去重，不产生重复 run | P0 | 未执行 |
| RST-03 | all | 客户端断线后按 request_id 重新订阅 | 状态完整恢复，已成功回复继续可见 | P0 | 未执行 |
| RST-04 | all | 服务重启后进行中的请求 | 请求状态可恢复，不丢失终态 | P1 | 未执行 |
| COST-01 | parallel | preview 对多 agent 并发的成本预估 | 展示并发 agent 数与预计 token/费用提示 | P1 | 未执行 |
| COST-02 | all | 请求用量统计 | 记录并发 agent 数、token 用量、终态与用量，并可核对 | P1 | 未执行 |
| COST-03 | all | 每请求预算/并发配额超限 | 服务端拒绝或限流，返回明确错误 | P1 | 未执行 |
| OBS-01 | all | 检查请求与 run 关联 | UI 中 request_id 可关联本次请求的全部 run id，可点击查看日志 | P1 | 未执行 |
| OBS-02 | supervisor | 检查 task 审计链 | 创建、派发、提交、审核、打回、重试均有审计记录 | P1 | 未执行 |

### 1.8 Supervisor 专项复核（对应 Codex 要求）

> 以下为 Codex 明确要求纳入验收矩阵的独立用例，编号与 §1.2–§1.7 中的对应用例交叉引用，确保不被遗漏。

| 代号 | Codex 要求 | 对应用例 |
|---|---|---|
| S-1 | preview 后撤权 | SEC-03 |
| S-2 | 私有 agent 枚举 | SEC-02 |
| S-3 | 重复提交及队列重试 | RST-01 / RST-02 |
| S-4 | 断线恢复 | RST-03 |
| S-5 | 部分失败及迟到回复 | DEG-06 / DEG-05 |
| S-6 | 汇总越权和回复内的派发指令 | SEC-04 / LOOP-03 |
| S-7 | attempt_count 达上限后不再重试 | CORE-08 |
| S-8 | 审核反馈正确传递给 worker | CORE-07 |

---

## 2. 内测 Pilot 计划

### 2.1 目标与范围

- **目标**：在受控工作区内验证三种模式的易用性、性能与准确性，发现并通过门禁清除 P0/P1 缺陷，为阶段 2 全量发布提供 go/no-go 依据。
- **规模**：5–10 人内测团队（建议 8 人），覆盖不同角色：普通用户、重度 Chat 用户、1 名管理员、1 名对权限敏感的安全观察者。
- **周期**：阶段 1 内测共 **1 周（7 天）**；第 8 天进行 go/no-go 评审。

### 2.2 时间安排

| 天 | 活动 | 产出 |
|---|---|---|
| D0 | 内测招募、发放快速上手指南（三种模式各 1 个种子场景） | 名单 + 指南 |
| D1–D2 | 集中试用 parallel / linear；每日 15 分钟站会 | 首批反馈与缺陷 |
| D3–D4 | 集中试用 supervisor（含打回重试、迟到回复） | Supervisor 专项反馈 |
| D5 | 权限与私有 agent 场景定向验证（由安全观察者执行） | 安全项结论 |
| D6 | 反馈问卷汇总、缺陷分级复盘 | 分级缺陷表 |
| D7 | 修复窗口：清除 P0/P1 | 修复验证记录 |
| D8 | go/no-go 评审 | 决策记录 |

### 2.3 反馈收集

- **三个维度**（与验收标准一致）：
  - 易用性：模式选择是否直观、Task 面板是否易懂、反馈表单是否顺手。
  - 性能：首条回复延迟、全部回复完成时间、汇总耗时、卡顿/超时感受。
  - 准确性：合成是否忠于原始回复、来源标注是否可核对、是否出现新事实或分歧丢失。
- **收集渠道**：应用内一键反馈控件 + 每日站会 + D6 结构化问卷（每个维度 1–5 分 + 开放题）。
- **埋点配合**：反馈关联 request_id，便于回放当时的运行轨迹与审计链。

### 2.4 缺陷分级与退出门禁

| 级别 | 定义（满足任一即归入） | 处理 SLA | 退出要求 |
|---|---|---|---|
| P0 | 越权/secret 透传/回复内指令被实际执行；数据泄露；核心流程完全不可用；服务崩溃 | 立即修复，发现当日内 | **必须为 0** |
| P1 | 三种模式中任一主流程阻断；attempt_count 规则失效；汇总严重失真；影响多数用户 | 48 小时内 | **必须为 0** |
| P2 | 边界场景错误、降级提示不友好、性能明显但不阻断 | 本迭代内 | 允许遗留，需登记 owner 与排期 |
| P3 | 文案、对齐、非关键体验 | 后续版本 | 允许遗留 |

**Pilot 退出门禁（go/no-go）**：
1. P0 = 0 且 P1 = 0；
2. §1 测试矩阵中 P0 用例全部通过，P1 用例通过率 ≥ 95%；
3. 三个反馈维度平均分 ≥ 3.5/5，且无维度低于 3.0；
4. 监控面板与告警规则已接入并验证（见 §3）；
5. go/no-go 评审记录归档。

任一门禁未达 → 延长内测 1 周或回到修复阶段，不进入阶段 2。

---

## 3. 监控与告警配置

### 3.1 指标目录

| 指标名（Prometheus） | 类型 | 标签 | 用途 |
|---|---|---|---|
| `multi_trigger_requests_total` | counter | `mode`, `status`, `workspace` | 请求量、成功率、各模式调用量分布 |
| `multi_trigger_request_duration_seconds` | histogram | `mode` | 请求级 p50/p95/p99 延迟 |
| `multi_trigger_agent_latency_seconds` | histogram | `agent_id`, `mode` | 单 agent 回复延迟，定位慢参与者 |
| `multi_trigger_agents_in_flight` | gauge | `workspace` | 并发 agent 数（成本告警输入） |
| `multi_trigger_tokens_total` | counter | `mode`, `agent_id`, `kind`(prompt/completion) | token 用量与成本估算 |
| `multi_trigger_estimated_cost` / `multi_trigger_actual_cost` | gauge | `request_id`, `mode` | 预估 vs 实际成本偏差 |
| `multi_trigger_errors_total` | counter | `type`(timeout/failure/permission/limit), `mode` | 错误率与错误类型分布 |
| `multi_trigger_tasks_total` | counter | `status`, `workspace` | Supervisor task 状态分布 |
| `multi_trigger_task_attempts` | histogram | `supervisor_agent_id` | 平均重试次数 |
| `multi_trigger_task_review_total` | counter | `action`(approve/reject) | 审核通过率、打回率 |
| `multi_trigger_budget_usage_ratio` | gauge | `workspace` | 预算阈值使用率 |

> 命名与标签为基线约定；实现（MARS-20/22）需按此暴露指标，告警规则文件 `deploy/monitoring/alerts.yaml` 以此为准。

### 3.2 仪表盘面板

| 面板 | 关键图表 | 数据源指标 |
|---|---|---|
| 总览 | 请求速率、成功率、延迟 p50/p95/p99、各模式调用量占比 | `requests_total`, `request_duration_seconds` |
| Supervisor | 审核通过率、打回率、平均重试次数、task 状态分布 | `task_review_total`, `task_attempts`, `tasks_total` |
| 成本 | 并发 agent 数、token 用量趋势、预算消耗、预估/实际偏差 | `agents_in_flight`, `tokens_total`, `budget_usage_ratio`, `*_cost` |
| 错误 | 错误率趋势（按类型）、失败最多的 agent Top N | `errors_total`, `agent_latency_seconds` |
| 审计日志查询 | 按 request_id / 用户 / agent / 模式 / 状态 / 时间范围过滤，展示完整调用链与 task 审计链 | 审计事件流（结构化日志） |

**审计日志查询界面要求**：
- 支持按 `request_id`、发起人、agent、mode、状态、时间范围组合过滤；
- 展示每次触发的调用关系（invocation graph）与 Supervisor 的创建/派发/提交/审核/打回/重试全链；
- 完整运行日志沿用独立访问控制：普通用户只能看到自己可见的回复与状态，审计详情页需更高权限。

### 3.3 告警规则

规则基线见 `deploy/monitoring/alerts.yaml`（Prometheus 兼容格式），要点如下：

| 规则 | 条件 | 级别 | 通知 |
|---|---|---|---|
| 错误率过高 | `error rate > 1%` 持续 5 分钟 | critical | **paging** |
| 请求成功率过低 | `success rate < 99%` 持续 10 分钟 | critical | paging |
| 延迟劣化 | p95 > 30s 持续 10 分钟 | warning | 工单/群通知 |
| 并发 agent 超限 | 并发 agent 数 > 5（上限）持续 5 分钟 | warning | 群通知 |
| token 预算预警 | 预算使用率 > 80% | warning | 群通知 |
| token 预算超限 | 预算使用率 > 100% | critical | paging |
| 成本预估偏差 | 实际/预估 > 1.5 持续 15 分钟 | warning | 群通知 |
| Supervisor 打回率过高 | 打回率 > 50% 持续 15 分钟 | warning | 群通知 |
| 请求堆积 | 进行中请求 > 阈值 且无变化 15 分钟 | warning | 群通知 |

> 「错误率 > 1% 触发 paging」为硬性要求，已在规则文件中设为 `critical` 并标注 `paging: true`。

### 3.4 告警验证用例

| ID | 步骤 | 预期 |
|---|---|---|
| ALERT-01 | 注入 2% 错误率，持续 6 分钟 | ErrorRateHigh 触发 critical 并 paging |
| ALERT-02 | 注入 p95 > 30s 10 分钟 | RequestLatencyHigh 触发 warning |
| ALERT-03 | 并发 agent 数拉到 6 | TooManyConcurrentAgents 触发 |
| ALERT-04 | 预算使用率拉到 85% / 105% | BudgetWarning / BudgetExceeded 分别触发 |
| ALERT-05 | Supervisor 打回率拉到 60% | SupervisorHighRejectRate 触发 |

---

## 4. 渐进发布与回滚方案

### 4.1 阶段划分

| 阶段 | 范围 | 准入条件 | 时长 |
|---|---|---|---|
| 阶段 0 | 开发团队 dogfood | §1 P0 用例通过 | 内部，不限 |
| 阶段 1 | 内测团队（§2） | Pilot 退出门禁全部达成 | 1 周 |
| 阶段 2a | 全量工作区 10% 灰度 | 阶段 1 go/no-go 通过 + 监控告警在线 | 1–2 天 |
| 阶段 2b | 50% → 100% 全量 | 2a 错误率 < 1%、无 P0/P1 | 逐日放量 |

### 4.2 Feature Flag 配置

| Flag | 作用 | 默认值 |
|---|---|---|
| `multi_agent_chat.enabled` | 总开关（kill switch） | false |
| `multi_agent_chat.allowlist` | 阶段 1 内测成员/工作区白名单 | 空 |
| `multi_agent_chat.rollout_percentage` | 阶段 2 灰度比例 | 0 |
| `multi_agent_chat.max_participants` | 参与者上限 | 5 |
| `multi_agent_chat.max_attempts` | Supervisor 默认重试上限 | 3 |
| `multi_agent_chat.merge_strategy` | 默认合成策略 | summary |
| `multi_agent_chat.cost_budget` | 每工作区预算阈值 | 按工作区配置 |

### 4.3 回滚预案

- **即时回滚**：将 `multi_agent_chat.enabled` 置为 false，新请求立即不再进入群聊路径（影响秒级）。
- **灰度回退**：将 `rollout_percentage` 调回上一稳定档位（100% → 50% → 10% → 0）。
- **进行中请求**：开关关闭后，已入队请求按现有状态机跑到终态或由服务端取消，向用户返回明确提示；不得产生悬挂的审核任务。
- **数据保留**：审计日志与请求记录保留，便于事后复盘；不回滚已产生的可见回复。
- **回滚触发条件**：出现任一 P0；错误率 > 1% 且 paging 后 15 分钟内未恢复；预算超限且无法即时限流。

### 4.4 Go/No-Go 检查清单

- [ ] §1 测试矩阵 P0 全通过、P1 通过率 ≥ 95%
- [ ] Pilot 退出门禁四项全部达成（§2.4）
- [ ] 监控面板关键指标可观测（§3.2）
- [ ] 告警规则导入并验证（§3.4 全通过）
- [ ] Feature flag 与回滚预案演练通过（§4.2/4.3）
- [ ] go/no-go 评审记录归档

---

## 5. 验收门禁增量：五项关键用例（方案增量，尚未执行）

> 来源：Codex 在 MARS-23 / MARS-24 的审核意见（2026-11-22）。
> **状态约定：以下五项均为「计划 / 未执行」。编排文档与 YAML 语法解析不算执行通过；每项须在运行时与联调环境就绪后实跑，并用运行记录（日志/截图/审计链）取证回填。** 本文只补齐测试设计，不涉及实现、部署、告警上线或 Pilot 启动。

### 5.1 GATE-01 第三次打回后终止（attempt_count 达上限）

- **关联**：§1.2 CORE-08 / §1.8 S-7
- **前置条件**：supervisor 模式；`max_attempts=3`；worker agent 可被主管反复 reject；存在稳定可复现的 reject_reason。
- **操作**：发起一次 supervisor 请求；主管对同一 worker task 连续执行 3 次 reject（每次带结构化反馈）；第 3 次 reject 后观察 task 状态与派发行为。
- **预期结果**：第 1、2 次 reject 各使 `attempt_count` +1 并重新派发；第 3 次 reject 后 task 置为 `failed`，**不再产生新的派发/新 run**；UI 剩余重试次数显示 0；请求可按失败策略收敛为 partial/failed，不得悬挂在 `submitted/reviewed`。
- **取证点**：① 审计链中恰好 3 条 reject 记录与对应 attempt_count（1→2→3）；② 第 3 次后无新 `assigned` 事件、无新 run id；③ GET 请求快照中 task 终态 `failed` 且 remaining=0；④ UI 截图显示 failed 与 0 次剩余。

### 5.2 GATE-02 并发审核（多 task 同时到达审核态）

- **关联**：§1.2 CORE-06/07 / §1.8 S-8
- **前置条件**：supervisor 模式；同一 request 下 ≥3 个 worker task；各 worker 返回时间不同，使多个 task 近乎同时进入 `submitted`；主管审核接口可并发调用。
- **操作**：让 ≥3 个 task 同时提交；并发对这些 task 提交 review（混合 approve 与 reject，其中 reject 带结构化反馈）；观察状态一致性与最终合成。
- **预期结果**：每个 task 的 review 独立生效，状态与 attempt_count 无串扰/丢失更新；reject 的反馈只回传对应 worker；全部 task 终态后合成才产出，且纳入的来源是实际 approved 的 task；并发下无重复派发、无越权审核。
- **取证点**：① 每个 task 的审核请求与结果一一对应（task_id 隔离）；② 并发 review 后的 GET 快照逐 task 状态正确；③ 合成结果仅含 approved 来源，reject 项标注未纳入；④ 并发审核的审计事件时间戳与 task_id 映射；⑤ 审核接口的并发去重/幂等记录。

### 5.3 GATE-03 入队后断线恢复

- **关联**：§1.7 RST-03/RST-04 / §1.8 S-4
- **前置条件**：任一模式；请求已成功入队并返回 `request_id`；客户端订阅中；服务端可模拟网络中断与服务重启。
- **操作**：在部分 run 已入队、部分仍在运行时断开客户端连接一段时间；期间让后台继续执行至产生终态；客户端用同一 `request_id` 重新订阅；另做一次服务端重启后恢复。
- **预期结果**：重订阅后返回完整且一致的状态快照，已完成的回复继续可见，不产生重复 run、不重复计费；断线期间的进度与终态可补齐；服务重启后进行中请求状态可恢复而非丢失/悬挂。
- **取证点**：① 断线前后同一 `request_id` 的快照对比（状态单调推进、无回退）；② run id 集合前后一致（无重复创建）；③ 用量/计费记录未因重连翻倍；④ 服务重启日志与恢复后的请求终态；⑤ 客户端恢复订阅的时间线。

### 5.4 GATE-04 迟到回复

- **关联**：§1.3 DEG-05/DEG-06 / §1.8 S-5
- **前置条件**：任一模式；某 agent 的响应刻意延迟到请求超时之后才返回；其余 agent 正常返回并已触发一次合成。
- **操作**：让请求先因超时收敛并产出一次合成；随后让迟到 agent 的回复到达；观察其展示与是否影响既有合成；再触发一次新版汇总。
- **预期结果**：迟到回复以「迟到」状态可见但**不并入已定稿合成**；UI 明确标注超时/迟到；提供发起新版汇总的入口，且新版汇总是显式用户动作而非自动改写；来源标注区分纳入项与未返回项。
- **取证点**：① 首次合成的内容与来源快照（不含迟到 agent）；② 迟到回复到达后的展示截图（标记迟到）；③ 首次合成未被自动改写的审计证据；④ 用户触发新版汇总后，新合成纳入迟到项的快照与来源标注。

### 5.5 GATE-05 撤权竞争（preview 后、派发前撤权）

- **关联**：§1.5 SEC-03 / §1.6 PV-01 / §1.8 S-1
- **前置条件**：用户对某参与者 agent 有 invoke 权限；trigger-preview 已成功展示该 agent chip 与 trigger_outcomes；权限可在 preview 与实际派发之间被撤销。
- **操作**：在 preview 之后、实际派发之前撤销该用户的 invoke 权限（并构造撤销与派发竞态的边界时序）；提交请求；观察实际派发结果；再尝试对含已无权限 agent 的历史回复发起汇总。
- **预期结果**：实际派发以**派发时刻的实时权限**为准，被撤权的 agent 不被触发；preview 不保证触发，结果返回明确原因；调用者不可见其无权的 agent（不暴露存在/在线状态）；汇总前重新校验读取权限，无权回复不被纳入，越权读取与额外派发均为零容忍（P0）。
- **取证点**：① 撤权时间戳与派发时间戳的先后记录；② 派发结果中该 agent 未生成 run 及原因；③ 权限校验日志（服务端逐项校验）；④ 私有 agent 未被枚举/暴露的响应；⑤ 汇总时二次权限校验日志与未纳入项标注；⑥ 越权尝试被拒绝的审计记录（应为空通过项）。

### 5.6 五项门禁取证状态（均未执行）

| 用例 | 设计 | 前置条件 | 运行时就绪 | 执行记录 | 结论 |
|---|---|---|---|---|---|
| GATE-01 第三次打回终止 | 已补齐 | 已写明 | 待确认 | 未执行 | 计划 |
| GATE-02 并发审核 | 已补齐 | 已写明 | 待确认 | 未执行 | 计划 |
| GATE-03 入队后断线恢复 | 已补齐 | 已写明 | 待确认 | 未执行 | 计划 |
| GATE-04 迟到回复 | 已补齐 | 已写明 | 待确认 | 未执行 | 计划 |
| GATE-05 撤权竞争 | 已补齐 | 已写明 | 待确认 | 未执行 | 计划 |

---

## 6. 联调就绪清单（方案增量）

> 依据 Codex 派发要求：标出已具备项、缺口与负责人；沿用 MARS-20 / MARS-22 现有分工，**不重复派发实现**。所有「缺口」须由对应负责人确认后才解除 MARS-23 阻塞。

### 6.1 已具备项（QA 侧基线）

| 项 | 说明 | 负责人 |
|---|---|---|
| 测试方案本体 | 本文 §1–§5（45+ 用例 + 五项门禁详细设计），已提交并推送到分支 `agent/workbuddy/b0d3868d4f08` | Workbuddy |
| 告警规则基线 | `deploy/monitoring/alerts.yaml`，3 组 9 条，YAML 解析校验通过（**仅语法，未验证实际触达**） | Workbuddy |
| 指标/面板/回滚/发布设计 | 本文 §3、§4 的指标目录、面板、feature flag 与回滚预案 | Workbuddy |
| 监控实现责任归属 | 指标暴露归 MARS-20/22 实现，QA 负责验证，不另起工单 | Workbuddy（验证）/ MARS-20·22（暴露） |

### 6.2 缺口与负责人

| 缺口 | 需求 | 负责人 | 阻断对象 |
|---|---|---|---|
| 产品代码仓库确认 | Codex 要求 MARS-13 负责人确认正式产品代码仓库（当前 QA 文档在 `multi-agent-chat-orchestrator` 分支，产品实现仓库未确认） | MARS-13 负责人 | 全部联调 |
| 联调环境确认 | 需可运行三种模式、可注入超时/失败/撤权/断线的受控环境 | MARS-13 负责人 | 全部联调 |
| 运行时/请求状态机 | 需可联调版本（pending→…→completed/failed/partial，request_id 与 run 关联） | MARS-20 | GATE-01/03/04、E2E 实跑 |
| 审核接口（approve/reject + 结构化反馈 + attempt_count） | 需可联调版本，支持并发审核与幂等 | MARS-20 / MARS-22 | GATE-01/02、§1.2 Supervisor |
| 恢复接口（断线重订阅 / request_id 快照 / 服务重启恢复） | 需可联调版本 | MARS-22 | GATE-03 |
| 权限与撤权生效点 | preview 与派发两次权限校验、私有 agent 不暴露的实现 | MARS-20 / MARS-22 | GATE-05、SEC-01~07 |
| 指标暴露 | §3.1 指标需在运行时真实暴露（非仅文档定义） | MARS-20 / MARS-22 | 告警实际触达验证 |
| 告警栈接入 | 告警规则需导入 Prometheus 兼容栈并配置 paging 通道 | 联调环境负责人（MARS-13 确认） | 告警触达验证 |
| Feature flag 开关 | `multi_agent_chat.enabled` 等需可切换以演练回滚 | 联调环境负责人（MARS-13 确认） | 回滚演练 |

### 6.3 下一步（按依赖顺序，本次均不启动）

1. MARS-13 负责人确认产品代码仓库与联调环境 → 解除总阻塞。
2. MARS-20 / MARS-22 确认运行时、审核、恢复、权限接口有可联调版本（沿用现有分工，不重复派发实现）。
3. 环境就绪后，Workbuddy 实跑三种模式与失败场景（GATE-01~05 + §1 矩阵），用运行记录取证回填状态列。
4. 验证 §3 指标真实暴露与告警实际触达（含 >1% 错误率 paging）、演练 feature flag 回滚。
5. 验收通过后，另提交内测名单与 Pilot 启动方案供确认；**本次不部署、不发邀请、不开 PR**。

### 6.4 本次交付边界声明

- 本次只交方案增量（§5、§6）与已推送的 QA 基线分支，**不声称任何用例执行通过**。
- YAML 语法解析通过 ≠ 告警实际触达；测试设计完成 ≠ E2E 通过。
- MARS-23 保持 `blocked`，待上游解阻后按 §6.3 推进。
