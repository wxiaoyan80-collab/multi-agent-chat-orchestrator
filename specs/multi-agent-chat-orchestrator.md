# 多人智能体群聊功能（Orchestrator/Coordinator 模式）

## 摘要

- 目标：在 Multica 的 Chat 场景里支持向多个 agent 发起"群聊"请求，展示每个 agent 的独立回复，并提供可选的汇总/合成视图与降级策略。
- 三种交互模式：
  1. **线性聊天** - 按固定顺序依次触发 agent，上一条回复作为下一条的上下文
  2. **并行聊天** - 同时触发所有参与者并并行收集回复（默认模式）
  3. **主管模式** - 主管 agent 负责派活，worker agent 干活并回复，主管审核不合格打回重做，返回次数可设限
- 推荐理由：最小改动后端（复用现有 mention/trigger 路径）、可实现序列化依赖、易于控制循环/超时与审计。

---

## 交互假设（默认，若需可调整）

- 展示模式：并行展示每个 agent 的回复（边到边展示），同时提供显眼的"汇总/合成"按钮，用户可点击合成最终答案或选择"等待所有回复后自动合成"。
- 典型并发 agent 数：3；默认上限：5（可配置）。
- 主管模式默认重试上限：3 次（workspace 可配置，请求层可覆盖）。

---

## 功能要点

### 1) 前端

- 在 Chat Composer 增加"选择参与 agent"控件（多选），并在发送前调用 trigger-preview 展示将触发的 agent chips 与预计成本提示。
- 新增**模式选择器**：linear / parallel / supervisor 三种模式。
- **Supervisor 模式**额外 UI：
  - Task 面板：显示每个 worker 的 attempt_count、状态、审核按钮与反馈表单
  - 主管打回时，worker 在 UI 可看到反馈并重新提交
  - 显示剩余重试次数（remaining attempts）与拒绝原因
- 发送后并发订阅/轮询每个被触发的 run，按来源渲染多条回复（带 agent avatar、name、状态标签）。
- 提供"汇总/合成"按钮：请求 Coordinator 做合成（或由客户端调用专门的合成 agent）。
- 展示失败/超时提示；提供"降级到单一专家"与"人工接管" CTA。

### 2) 平台服务/后端（核心）

**设计原则**：将 Coordinator 改为平台服务。现有 run 在顶层回合结束后没有后台完成唤醒，方案需要明确群聊如何跨 run 持久化收集结果。由平台持久化本次发送和参与者状态，负责排队、收集、截止时间和取消。

#### 请求记录与状态机

每次群聊请求生成稳定的 `request_id`，持久化：
- 发送幂等键、参与者记录与派发意图
- run ID 在分配后关联
- 下游幂等键覆盖入队后确认丢失、服务重启和客户端重发

**三种模式的执行策略**：

| 模式 | 执行方式 | 上下文依赖 | 适用场景 |
|------|----------|------------|----------|
| linear | 按序列依次触发，上一条回复注入下一条 | 强依赖 | 代码审查链、文档撰写流水线 |
| parallel | 同时触发所有参与者并行收集 | 无依赖 | 多专家并行咨询、头脑风暴 |
| supervisor | 主管分派任务 → worker 提交 → 主管审核 → 通过/打回 | 任务级依赖 | 复杂任务分解、质量把关 |

#### Supervisor 模式详细设计

**Task 字段**：
- `task_id`：唯一标识
- `parent_request_id`：关联的群聊请求
- `supervisor_agent_id`：主管 agent ID（支持 agent 担任主管）
- `worker_agent_id`：执行任务的 worker agent
- `payload`：任务输入载荷
- `result`：worker 提交的结果
- `attempt_count`：当前尝试次数（从 1 开始）
- `max_attempts`：最大重试次数（默认 3，可配置）
- `status`：pending / assigned / in_progress / submitted / reviewed / approved / rejected / failed / completed
- `feedback`：主管的结构化反馈（reject_reason, change_instructions）
- `created_at` / `updated_at`

**状态流**：
```
pending → assigned → in_progress → submitted → reviewed → approved → completed
                                          ↓
                                     rejected → (attempt_count < max_attempts) → assigned
                                          ↓
                                     (attempt_count >= max_attempts) → failed
```

**重试限制**：
- `max_attempts` 可按请求或全局配置，workspace 默认值为 3
- 每次打回 increment attempt_count，并在 UI 显示剩余重试次数
- 审核反馈：主管在 review 时提供结构化反馈（reject_reason, change_instructions），用于 worker 重做

#### 权限与安全

- Invoke gate：平台服务的 delegation 必须遵守当前的 invoke 授权规则——由发起的人类身份作为链顶。
- 不泄露 secrets：任何 agent 间的数据交换仅传递非敏感摘要或显式允许的字段；不要把 custom_env/mcp_config 等敏感配置传播给其他 agent。
- 循环检测：实现 max_depth 和 per-request max_messages 限制；在平台层记录 invocation graph 以便查核。
- **授权与数据隔离（服务端执行）**：从已认证会话确定原始发起人、workspace 和会话，提交及实际派发时逐个检查会话访问权和 agent invoke 权限。Preview 仅供提示，权限撤回或状态变化以实际派发结果为准。对调用者不可见的 agent 避免暴露其存在或在线状态。每个参与者仅接收已授权的消息和必要上下文，使用字段白名单排除环境变量、连接器配置、工具日志和私有历史。汇总前重新检查请求者对输入回复的读取权限，并确保汇总可见范围受输入权限约束。

#### 调用预算与审计

- 服务端执行参与者上限、并发配额和每请求预算，首发禁用参与者输出触发新的群聊调用。
- 记录原始发起人、授权及派发结果、请求与 run 关联、终态和用量。
- 面向用户展示可见回复及状态，完整运行日志沿用独立访问控制。
- 汇总把 agent 回复当作不可信文本处理，输出引用可核对的来源，保留分歧。

---

### 3) API 草案

#### 触发群聊请求
```
POST /api/v1/chat/multi-trigger
{
  "user_prompt": "string",
  "participant_agent_ids": ["agent_id_1", "agent_id_2", ...],
  "mode": "linear" | "parallel" | "supervisor",
  "supervisor_agent_id": "agent_id",  // required when mode=supervisor
  "timeout_seconds": 120,
  "merge_strategy": "summary" | "choose_best" | "concatenate",
  "max_attempts": 3,  // only for supervisor mode, defaults to workspace default
  "idempotency_key": "string"
}
```

响应：
```
{
  "request_id": "string",
  "participants": [
    {"agent_id": "...", "run_id": "...", "status": "queued" | "running" | ...}
  ],
  "tasks": [  // only for supervisor mode
    {"task_id": "...", "worker_agent_id": "...", "status": "pending", "attempt_count": 0, "max_attempts": 3}
  ]
}
```

#### 查询请求状态
```
GET /api/v1/chat/multi-trigger/{request_id}
```

响应包含所有参与者/任务的当前状态、回复内容、attempt_count 等。

#### 主管审核接口（Supervisor 模式）
```
POST /api/v1/chat/multi-trigger/{request_id}/tasks/{task_id}/review
{
  "action": "approve" | "reject",
  "feedback": {
    "reject_reason": "string",
    "change_instructions": "string"
  }  // required when action=reject
}
```

---

## 可观测性与审计

- 每次 multi-trigger 产出一组 run ids；保存 trigger-preview 的 trigger_outcomes 与 run ids。
- 在 UI/issue 中展示每个 agent 的 run id 与状态，便于点击查看完整日志。
- Supervisor 模式额外记录：task 创建、派发、提交、审核、打回、重试的完整审计链。

---

## 验收标准（示例）

- 用户能在 Composer 选择多名 agent（最多 5），选择模式（linear/parallel/supervisor），发送后能看到每个 agent 的独立回复（avatar+name）并能点击"汇总"得到合成结果。
- trigger-preview 能准确展示将触发的 agent chips 并返回 trigger_outcomes；若一个 agent 无法被触发（权限/离线），preview 给出明确原因。
- **Supervisor 模式**：能完成审核-打回-重试流程，并尊重 max_attempts（达到上限后标记 failed 不再重试）。
- 请求断线恢复、超时与迟到回复显示逻辑可验证；权限越权/透传为零容忍测试项。
- Coordinator/平台服务的合成结果包含每个原始回复的来源与摘要，并在 95% 的测试对话中不引入超出原始回复事实的新信息（合成策略可配置）。

---

## 实现任务拆分（建议）

- Spec & Review（本 issue） — 0.5 day
- Frontend prototype（Composer UI + preview + multi-run rendering + supervisor task panel）— 3 dev-days
- Platform service：请求状态机、任务持久化、权限校验、审计日志 — 3–4 dev-days
- Coordinator agent instructions 文案 + 测试 agents（模拟并发回复）— 1 dev-day
- Backend: run-listing / trace API （若需要）— 1–2 dev-days
- E2E QA + Pilot（限内测团队）— 1 dev-day
- Rollout & Monitoring 配置（限流、成本告警）— 1 dev-day

---

## 测试要点

- 并发与序列化场景（并行回答、串行链式调用、supervisor 分派审核流程）；
- 部分 agent 超时/失败时的降级；
- 循环触发防护与最大深度限制；
- 权限/私有 agent 不被非法触发；
- trigger-preview 与实际触发结果一致性；
- 成本上报（并发多 agent 的计费预估）；
- **Supervisor 特有**：preview 后撤权、私有 agent 枚举、重复提交及队列重试、断线恢复、部分失败及迟到回复、汇总越权和回复内的派发指令、attempt_count 达上限后不再重试、审核反馈正确传递给 worker。

---

## 交付物（本次 issue 将包含）

- 本 PRD 文档（specs/multi-agent-chat-orchestrator.md）
- 平台服务接口设计与任务状态机（本文档）
- 前端改动清单与示意图（简单线框）
- 验收测试矩阵与 Pilot 计划

---

## Coordinator/平台服务 Instructions 草稿（可直接放入 agent.instructions 或平台服务配置）

- 角色：你是"群聊协调器（Coordinator）"或平台群聊服务。当收到一个请求，参数包含：user_prompt、participant_agent_ids（列表）、mode（linear/parallel/supervisor）、supervisor_agent_id（仅 supervisor 模式）、timeout_seconds、merge_strategy（summary/choose_best/concatenate）、max_attempts（仅 supervisor 模式，默认 3）。
- 行为：
  1) 验证 participant_agent_ids 不超过允许上限并且调用者有权限触发这些 agent；
  2) 按 mode 执行：
     - linear：按序列依次触发，上一条回复注入下一条上下文
     - parallel：并行同时触发所有参与者
     - supervisor：触发 supervisor_agent 作为主管，由主管分派任务给 workers，收集结果并审核
  3) 在 timeout_seconds 内等待或收集回答，收集到后记录 (agent_id, run_id, content, status, latency)；supervisor 模式额外记录 task 状态与 attempt_count；
  4) 将所有原始回答按 merge_strategy 合成 final_answer，并在合成中保留每条原始回答的 1-2 行摘要与来源标签；
  5) 绝不将任何 agent 的 custom_env 或 secret 透传给其他 agent；仅传递由用户授权的非敏感字段或经掩码的摘要；
  6) 防止无限链：当 Coordinator/服务已经是调用链的一部分（检测到链深或 seen-agent set）时，不再触发新的 participants，除非有明确的人类指令允许。
- Supervisor 模式特有行为：
  - 主管审核 worker 提交的结果，决定 approve 或 reject
  - reject 时必须提供结构化反馈（reject_reason, change_instructions）
  - 打回时 increment attempt_count，若 attempt_count < max_attempts 则重新派发给 worker（可同 agent 或换人），否则标记 task failed
  - 所有 task approved 后生成最终合成结果
- 错误处理：
  - 单 agent 超时或失败：标记 failed 并继续合并剩余结果；
  - 多数失败（>50%）：直接返回错误并建议降级（例如只问一个专家或人工接管）；
  - 在不可预见错误时回退到安全简答并提示人工。

