# 多智能体群聊 API 与集成规范

> 配套文档：`specs/multi-agent-chat-orchestrator.md`（PRD / 状态机 / 权限原则）。本文定义前端可调用的 HTTP 契约、与现有 run 机制的集成方式，以及幂等/恢复语义。本文是 MARS-22 的交付物，依赖 MARS-20 平台服务提供的请求记录、状态机与持久化能力。

## 1. 范围与分层

```
前端 Composer
   │  POST preview / trigger, GET status/runs, POST review
   ▼
API 层（本文）         ← 身份解析、参数校验、授权落点、幂等、响应聚合、订阅句柄
   │  enqueue / list / subscribe / steer（复用现有机制）
   ▼
平台群聊服务（MARS-20） ← 请求记录、状态机、派发、收集、超时、任务持久化、审计
   │  mention→enqueue、trigger-preview、run 列表/订阅/Steer
   ▼
现有 Multica Chat run 基础设施
```

**设计约束（来自 Codex 对父提案的审核结论，API 层必须体现）：**

1. Coordinator 是平台服务，不是前端可直连的 agent；API 只暴露请求级操作，不暴露"触发某 agent 的 run"的裸接口。
2. 授权与数据隔离在服务端执行：身份来自已认证会话；preview 仅供提示，以实际派发结果为准；对调用者不可见的 agent 不暴露存在性或在线状态。
3. 提交返回稳定 `request_id` 与逐参与者状态；提供请求级批量读与批量订阅接口，断线可恢复。
4. 参与者上限、并发配额、每请求预算在服务端执行；首发禁用参与者输出触发新的群聊调用。
5. 汇总把 agent 回复当作不可信文本，输出引用可核对来源，保留分歧。

**首发范围**：三种模式（linear/parallel/supervisor）的触发、查询、审核；手动汇总；run-listing/trace。自动汇总、自动降级、参与者输出二次触发留待后续版本（与父提案一致）。

---

## 2. 通用约定

### 2.1 基址与版本
- 所有端点前缀：`/api/v1/chat/multi-trigger`
- 编码：UTF-8；请求/响应 `Content-Type: application/json`
- 时间：RFC 3339，UTC（如 `2026-11-22T05:16:37Z`）

### 2.2 认证与身份
- 所有端点要求已认证会话。服务端从会话解析三元组：`initiator_user_id`、`workspace_id`、`session_id`，作为调用链顶与授权依据。
- API 层不接受客户端传入的 `initiator_user_id` / `workspace_id` 覆盖；请求体中若出现这些字段一律忽略并记录审计告警。
- 匿名会话、跨 workspace 的 `request_id` 访问一律 `401`/`404`（对越权资源返回 404 而非 403，避免资源枚举）。

### 2.3 通用响应包装
成功响应直接返回资源对象（不加 envelope）；错误响应统一为：

```json
{
  "error": {
    "code": "string",          // 稳定机器码，见 2.5
    "message": "string",       // 人类可读，不得包含内部堆栈/密钥
    "request_id": "string?",   // 关联的请求 ID（如已知）
    "details": { ... }         // 可选结构化补充
  }
}
```

### 2.4 版本与并发控制
- `GET` 状态/列表端点支持 `ETag` 与 `If-None-Match`，未变更返回 `304`。
- 每个请求记录带单调递增的 `version`；客户端轮询/长轮询携带 `?since_version=N`，服务端仅在 `version > N` 时返回完整快照，否则 `304`。
- 审核端点要求 `If-Match: "task:<version>"`，防止并发审核覆盖（乐观锁）。

### 2.5 错误码表
| HTTP | code | 场景 |
|------|------|------|
| 400 | `invalid_argument` | 参数缺失/类型错/模式专属字段冲突 |
| 400 | `too_many_participants` | 超过参与者上限 |
| 400 | `invalid_review_transition` | 对非 submitted 任务审核 |
| 400 | `review_feedback_required` | reject 缺少结构化反馈 |
| 401 | `unauthenticated` | 无有效会话 |
| 403 | `agent_invoke_denied` | 对某 agent 无 invoke 权限 |
| 404 | `request_not_found` | 不存在/越权的 request_id |
| 404 | `task_not_found` | 不存在/越权的 task_id |
| 409 | `idempotency_key_reused` | 同 key 不同请求体 |
| 409 | `request_already_final` | 对终态请求做非法操作 |
| 422 | `budget_exceeded` | 并发配额/每请求预算耗尽 |
| 429 | `rate_limited` | 触发频率超限 |
| 500 | `internal_error` | 未预期错误（回退安全简答） |

---

## 3. API 数据模型（对外视图）

### 3.1 MultiTriggerRequest
```json
{
  "request_id": "string",
  "status": "pending | queued | running | completed | partial | failed | cancelled",
  "version": 42,
  "mode": "linear | parallel | supervisor",
  "initiator_user_id": "string",   // 来自会话，只读
  "workspace_id": "string",        // 来自会话，只读
  "session_id": "string",          // 来自会话，只读
  "user_prompt": "string",
  "merge_strategy": "summary | choose_best | concatenate",
  "timeout_seconds": 120,
  "max_attempts": 3,               // supervisor 模式
  "idempotency_key": "string",
  "participants": [ /* Participant[] */ ],
  "tasks": [ /* SupervisorTask[]，仅 supervisor */ ],
  "run_ids": ["string"],           // 本次请求关联的所有 run，供一次性订阅
  "final_answer": {                // 合成完成后出现
    "content": "string",
    "sources": [                   // 可核对来源，保留分歧
      {"agent_id": "string", "run_id": "string", "summary": "string"}
    ],
    "strategy": "summary | choose_best | concatenate"
  },
  "created_at": "2026-11-22T05:16:37Z",
  "updated_at": "2026-11-22T05:16:37Z"
}
```

### 3.2 Participant
```json
{
  "agent_id": "string",
  "name": "string",
  "avatar_url": "string?",
  "run_id": "string?",             // 分配后出现
  "status": "pending | queued | running | completed | failed | timeout | cancelled",
  "content": "string?",            // 已完成且调用者有读权限时返回
  "latency_ms": 1200,
  "error": {"code": "string", "message": "string"}?,
  "trigger_outcome": "triggered | denied | offline | budget"  // 与 preview 一致
}
```
- `content` 仅当调用者对该回复有读权限时返回；否则该字段省略且不暴露 agent 存在性（对不可见 agent 整条 participant 省略）。

### 3.3 SupervisorTask
```json
{
  "task_id": "string",
  "parent_request_id": "string",
  "supervisor_agent_id": "string",
  "worker_agent_id": "string",
  "status": "pending | assigned | in_progress | submitted | approved | rejected | failed | completed",
  "attempt_count": 1,
  "max_attempts": 3,
  "remaining_attempts": 2,
  "run_ids": ["string"],           // 各次 attempt 的 run，便于订阅
  "payload": { ... },              // 字段白名单过滤后的任务输入
  "result": "string?",
  "feedback": {"reject_reason": "string", "change_instructions": "string"}?,
  "created_at": "string",
  "updated_at": "string"
}
```

### 3.4 TriggerPreviewOutcome
```json
{
  "agent_id": "string",
  "name": "string",
  "avatar_url": "string?",
  "status": "triggerable | denied | offline | budget",
  "reason": "string?",             // denied/offline/budget 时给出明确原因
  "estimated_cost": 0.02
}
```

### 3.5 RunRef（run-listing 返回项）
```json
{
  "run_id": "string",
  "agent_id": "string",
  "kind": "participant | supervisor | worker",
  "task_id": "string?",
  "attempt": 1,
  "status": "queued | running | completed | failed | timeout | cancelled",
  "created_at": "string",
  "finished_at": "string?"
}
```

### 3.6 TraceEvent（审计链）
```json
{
  "seq": 7,
  "ts": "2026-11-22T05:17:02Z",
  "type": "request_created | authorized | dispatched | run_assigned | reply_received | task_submitted | task_reviewed | task_redi_spatched | merged | timeout | failed | cancelled",
  "actor": {"type": "user | agent | system", "id": "string"},
  "ref": {"request_id": "string", "task_id": "string?", "run_id": "string?", "agent_id": "string?"},
  "data": { ... }                  // 脱敏字段，不含 secret/custom_env/mcp_config
}
```

### 3.7 状态机

**请求状态**
```
pending → queued → running → completed | partial | failed | cancelled
```
- `partial`：部分参与者成功、部分失败/超时，且有可展示回复。
- `failed`：多数失败（>50%）或服务级错误，返回降级建议（单一专家 / 人工接管）。
- 终态：`completed | partial | failed | cancelled`。

**任务状态（supervisor）**
```
pending → assigned → in_progress → submitted → approved → completed
                                      ↓
                                   rejected → (attempt_count < max_attempts) → assigned
                                      ↓
                                   (attempt_count >= max_attempts) → failed
```
- `remaining_attempts = max_attempts - attempt_count`，达 0 后不再重试并保持 `failed`。

---

## 4. 端点定义

### 4.1 触发群聊请求

`POST /api/v1/chat/multi-trigger`

**请求头**：`Idempotency-Key: <string>`（必填）、可选 `Prefer: respond-async`。

**请求体**
```json
{
  "user_prompt": "string（必填，1..8000 字符）",
  "participant_agent_ids": ["string（1..N，N = workspace 上限，默认 5，去重）"],
  "mode": "linear | parallel | supervisor（默认 parallel）",
  "supervisor_agent_id": "string（仅 mode=supervisor 必填；不得与 participants 冲突）",
  "timeout_seconds": 120,          // 1..600，默认 120
  "merge_strategy": "summary | choose_best | concatenate（默认 summary）",
  "max_attempts": 3,               // 仅 supervisor，1..10，默认 workspace 默认 3
  "idempotency_key": "string（可选；优先取 Idempotency-Key 头）"
}
```

**校验顺序与落点**
1. 身份解析（会话）→ 无会话 `401`。
2. 参数结构校验 → `invalid_argument`；参与者数 > N → `too_many_participants`。
3. 预算校验（并发配额 / 每请求预算）→ `budget_exceeded` / `rate_limited`。
4. 逐参与者授权：会话身份对每个 agent 的 invoke 权限 + 会话对目标会话的访问权 → 任一失败 `403 agent_invoke_denied`；对调用者不可见的 agent 不返回其 ID。
5. 幂等键查重：命中同 key 同 body → 直接返回原 `request_id` 与当前快照（`200`，`Idempotent-Replay: true`）；命中同 key 不同 body → `409 idempotency_key_reused`。
6. 校验通过后创建请求记录（`pending`），落库幂等键、参与者、派发意图、下游幂等键，返回 `202`。

**响应 `202`**
```json
{
  "request_id": "string",
  "status": "pending",
  "version": 1,
  "participants": [
    {"agent_id": "...", "run_id": null, "status": "pending", "trigger_outcome": "triggered"}
  ],
  "tasks": [ /* supervisor 模式：task_id, worker_agent_id, status=pending, attempt_count=0, max_attempts */ ],
  "run_ids": [],
  "subscribe": {                    // 前端据此一次性订阅本次请求所有 run
    "runs_endpoint": "/api/v1/chat/multi-trigger/{request_id}/runs",
    "status_endpoint": "/api/v1/chat/multi-trigger/{request_id}?since_version=1",
    "trace_endpoint": "/api/v1/chat/multi-trigger/{request_id}/trace?since_seq=0"
  },
  "created_at": "string"
}
```
- `run_id` 在平台分配后异步填充；前端用 `subscribe` 中的端点订阅，而非为每个 run 单独订阅。

**副作用**：创建请求记录；写入幂等索引；按 mode 派发（linear 串行注入上下文 / parallel 并行 / supervisor 触发主管分派）。首发禁用参与者输出触发新群聊。

---

### 4.2 查询请求状态

`GET /api/v1/chat/multi-trigger/{request_id}?since_version=N&include=participants,tasks,final_answer`

**查询参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| `since_version` | int | 仅当 `version > N` 返回完整快照，否则 `304`（长轮询/断线补拉） |
| `include` | csv | 需要展开的字段，默认全部可见字段 |

**响应 `200`**：完整的 `MultiTriggerRequest` 视图（见 3.1），含：
- 所有参与者当前状态、回复内容、run_id、latency、error；
- supervisor 任务的 `attempt_count` / `remaining_attempts` / `status` / `feedback`；
- `run_ids`（一次性订阅用）；
- `final_answer`（合成完成后，带来源与摘要）；
- `status` 反映实时进度（每次状态变更递增 `version`）。

**授权**：校验会话身份对 `request_id` 的读权限；越权返回 `404 request_not_found`。对无读权限的参与者回复省略 `content`；对不可见 agent 省略整条 `participants` 项。

**实时性与迟到回复**：
- 超时后迟到的回复：若请求进入终态后到达，记录到 trace 但不改变 `content`；`include=late_replies` 可显式查看，UI 标注"迟到回复（未纳入汇总）"。
- 何时可发起新版汇总：仅在 `status ∈ {completed, partial}` 且距上次合成的所有来源稳定后；重复汇总生成新 `final_answer` 版本并在 source 中标注。

**长轮询**：支持 `Prefer: wait=30`（秒），在 `version` 无变化时保持连接最长 30s，到期返回 `304`。

---

### 4.3 主管审核

`POST /api/v1/chat/multi-trigger/{request_id}/tasks/{task_id}/review`

**请求头**：`If-Match: "task:<version>"`（乐观锁，必填）。

**请求体**
```json
{
  "action": "approve | reject",
  "feedback": {                    // action=reject 时必填
    "reject_reason": "string（1..200）",
    "change_instructions": "string（1..4000）"
  }
}
```

**校验与状态推进**
1. 授权：仅该请求的 supervisor agent（或代表主管的人类管理员）可审核 → 否则 `403`。
2. 任务必须处于 `submitted` → 否则 `400 invalid_review_transition`。
3. `If-Match` 版本不匹配 → `412 Precondition Failed`，要求客户端重读后再审。
4. `reject` 缺少 `feedback.reject_reason` 或 `change_instructions` → `400 review_feedback_required`。

**状态更新（平台服务执行，API 触发并回读）**
- `approve`：`submitted → approved → completed`；若所有任务 completed，请求进入 `completed` 并可合成。
- `reject`：`submitted → rejected`，`attempt_count += 1`：
  - `attempt_count < max_attempts` → `assigned`，携带反馈重新派发给 worker（可同 agent 或换人），生成新 `run_id` 并追加到 task 的 `run_ids`；
  - `attempt_count >= max_attempts` → `failed`，不再重试。

**响应 `200`**
```json
{
  "task_id": "string",
  "status": "assigned | completed | failed",
  "attempt_count": 2,
  "max_attempts": 3,
  "remaining_attempts": 1,
  "feedback": { ... },
  "new_run_id": "string?",         // 重新派发时出现，前端据此订阅
  "version": 9,
  "request_status": "running"
}
```

**并发**：审核与 worker 提交并发时，以 `If-Match` 版本仲裁；同一任务重复审核返回 `409 request_already_final` 或 `412`。

---

### 4.4 trigger-preview（复用现有机制）

`POST /api/v1/chat/multi-trigger/preview`

供 Composer 发送前展示将触发的 agent chips 与预计成本。**不创建请求、不派发 run。**

**请求体**：与 4.1 相同的字段（不需要 `idempotency_key`）。

**响应 `200`**
```json
{
  "participants": [ /* TriggerPreviewOutcome[]，见 3.4 */ ],
  "estimated_total_cost": 0.06,
  "within_budget": true,
  "warnings": ["agent X 不可见，已省略"],
  "expires_at": "2026-11-22T05:20:00Z"
}
```

**语义**
- `status=denied/offline/budget` 的参与者给出明确 `reason`，前端 chip 标注不可触发原因。
- **preview 仅供提示**：权限撤回或状态变化以 4.1 实际派发结果为准；4.1 的 `participants[].trigger_outcome` 与 preview 对齐，不一致项在 trace 中记录。
- 对调用者不可见的 agent：preview 中不出现其 ID 或在线状态（不暴露存在性）。
- `expires_at` 后预览结果可能失效，发送前建议重新 preview。

---

### 4.5 run-listing（一次性关联本次请求的所有 run）

`GET /api/v1/chat/multi-trigger/{request_id}/runs?since_version=N`

这是"前端能一次性订阅本次请求所有 run"的核心端点。前端拿到 `RunRef[]` 后，用现有 run 订阅机制批量订阅（见 5.2），无需逐 agent 单独拉 run 列表。

**响应 `200`**
```json
{
  "request_id": "string",
  "version": 12,
  "runs": [ /* RunRef[]，见 3.5 */ ],
  "pagination": {"next_cursor": "string?"}
}
```
- `run_id` 异步分配，分配后进入列表并递增 `version`；客户端用 `since_version` 增量拉取新增 run。
- supervisor 模式包含 supervisor run、各 worker 各次 attempt 的 run（`kind` + `task_id` + `attempt` 标注）。
- 每个 run 带 `run_id`，可点击跳转查看完整日志（沿用既有 run 日志的独立访问控制）。

---

### 4.6 trace / 审计

`GET /api/v1/chat/multi-trigger/{request_id}/trace?since_seq=0`

**响应 `200`**
```json
{
  "request_id": "string",
  "events": [ /* TraceEvent[]，按 seq 升序，见 3.6 */ ],
  "next_seq": 18
}
```
- 记录完整链：请求创建 → 授权 → 派发 → run 分配 → 回复到达 → 任务提交/审核/打回/重派 → 合成 → 终态。
- 字段白名单：不含 `custom_env`、`mcp_config`、连接器配置、工具日志、私有历史、密钥。
- 完整 run 日志仍走独立访问控制；本端点只返回脱敏摘要。
- 支持 `since_seq` 增量拉取与 `Prefer: wait=30` 长轮询，供 UI/调试实时展示。

---

## 5. 与现有机制的集成

### 5.1 复用映射表

| 能力 | 复用现有机制 | API 层职责 |
|------|--------------|------------|
| 发送前预览 | trigger-preview | 4.4 包装为群聊 preview，补充逐参与者 outcome 与成本 |
| 派发 agent | mention → enqueue 路径 | 透传原始调用者、请求 ID、参与者关联与下游幂等键 |
| run 订阅 | run 列表 / 订阅 / Steer | 4.5 聚合 run_ids；前端批量订阅；Steer 用于取消/重试 |
| 状态追踪 | issue run 列表 + 过滤 | 4.2 按 request_id 聚合快照，提供 ETag/增量 |
| 审计 | invocation graph（长期方案） | 4.6 trace 端点输出脱敏事件流 |

### 5.2 一次性订阅所有 run 的时序

```
前端                    API 层                 平台服务              run 基础设施
 │ POST preview          │                        │                     │
 │──────────────────────▶│ trigger-preview 校验   │                     │
 │◀──────────────────────│ outcomes/chips/cost    │                     │
 │ POST trigger (+key)   │                        │                     │
 │──────────────────────▶│ 身份/授权/预算/幂等    │                     │
 │                       │────create request─────▶│ enqueue (下游key)    │
 │                       │                        │────────────────────▶│ run 分配
 │◀──202 request_id──────│                        │◀────run_ids─────────│
 │ GET /runs?since=0     │                        │                     │
 │──────────────────────▶│────list runs───────────▶│                     │
 │◀──RunRef[]────────────│                        │                     │
 │ 批量订阅所有 run_id（复用现有 run 订阅）        │                     │
 │══════════════════════════════════════════════════════════════════════▶│
 │ GET status?since_version / trace?since_seq（断线补拉 / 长轮询）        │
 │◀──完整快照 / 事件流────│                        │                     │
```

- 前端从 202 响应的 `subscribe` 句柄获取三个端点；`run_ids` 一旦分配即进入 4.5 列表。
- 断线恢复：重连后先 `GET /runs?since_version=last` 补拉新 run，再 `GET status?since_version=last` 补拉状态，最后 `GET trace?since_seq=last` 补拉审计事件。

### 5.3 Steer / 取消 / 重试集成
- 取消：对 `pending/queued/running` 请求，`POST /{request_id}/cancel`（终态返回 `409 request_already_final`），平台通过 Steer 取消关联 run。
- 重试：仅对 `failed/timeout` 参与者，由 supervisor 审核重派（4.3）触发，不重新创建顶层请求。
- 客户端重发 trigger：靠幂等键短路，不产生新 run（见 6）。

---

## 6. 幂等与恢复

### 6.1 幂等键生命周期
- 作用域：`(workspace_id, initiator_user_id, idempotency_key)`；有效期建议 24h（可配置）。
- 索引落库：存储 `request_id` + 请求体哈希 + 首次响应快照。
- 重放：同 key 同 body → 返回原 `request_id` 与当前快照（`200` + `Idempotent-Replay: true`），不创建新请求/新 run；同 key 不同 body → `409`。
- 幂等键优先取 `Idempotency-Key` 头，body 中 `idempotency_key` 作为兼容回退；两者不一致以头为准并记录告警。

### 6.2 下游幂等键
- 平台为每个参与者/每次派发生成稳定的下游幂等键（由 `request_id + agent_id + attempt` 派生），覆盖"入队后确认丢失"场景。
- 同一 `(request_id, agent_id, attempt)` 的 enqueue 至多一次，避免队列重试造成重复 run。

### 6.3 服务重启恢复
- 请求记录持久化：幂等键、参与者/派发意图、run_id 关联、任务状态与 attempt_count、终态与用量。
- 重启后平台扫描非终态请求，依据持久化状态继续收集/超时判定；API 层 `GET status` 与 `GET /runs` 从存储重建视图。
- 已进入终态的请求不受重启影响；迟到回复按 4.2 规则写入 trace 但不改写终态内容。

---

## 7. 权限与安全在 API 层的落点

| 风险（父审核结论） | API 层落点 |
|--------------------|------------|
| 越权触发 | 4.1/4.4 逐参与者 invoke 权限校验（会话身份，链顶为人类）；403 基于会话，不基于客户端声明 |
| 不可见 agent 枚举 | 4.1/4.2/4.4 对无读权限的 agent 省略整条记录及其在线状态；越权 request_id 返回 404 |
| preview 后撤权 | 4.4 标注 expires_at；4.1 实际派发重新校验，`trigger_outcome=denied` 并在 trace 记录 |
| secret 透传 | 4.3 payload 字段白名单；3.6 trace 排除 custom_env/mcp_config/工具日志；内部错误码不外泄详情 |
| 回复内派发指令 / 汇总越权 | 4.2 final_answer 仅含可核对来源；3.1 sources 标注 agent_id/run_id；汇总前重新校验请求者对输入回复的读权限 |
| 并发审核覆盖 | 4.3 `If-Match` 乐观锁 + 服务端状态机仲裁 |
| 二次触发新群聊 | 首发：API 不提供由参与者输出直接触发新请求的端点；平台侧禁用 |

---

## 8. 验收测试矩阵

| # | 验收标准（MARS-22） | 验证端点 / 方法 | 通过条件 |
|---|--------------------|-----------------|----------|
| 1 | trigger-preview 准确展示 agent chips 与 trigger_outcomes | 4.4 → 4.1 | preview 的 `participants[].status` 与 trigger 的 `trigger_outcome` 对齐；不可触发 agent 给出明确 reason；preview 不创建 run |
| 2 | 前端能一次性订阅本次请求所有 run | 4.1 `subscribe` + 4.5 `/runs` | 202 返回订阅句柄；`/runs` 返回完整 `run_ids`（含 supervisor 各 attempt）；`since_version` 可增量拉取 |
| 3 | 请求状态查询实时反映各参与者/任务进度 | 4.2 `GET status` | 每次状态变更递增 version；`since_version`/ETag/长轮询生效；迟到回复按规则标注且不改写终态 |
| 4 | Supervisor 审核正确更新 task 状态与 attempt_count | 4.3 `/review` | approve→completed；reject 且 `<max`→assigned 且 attempt_count+1 且新 run_id；reject 且 `>=max`→failed 不再重试；非 submitted/`If-Match` 失配被拒 |

**补充零容忍测试**（与父提案一致）：越权读取回复、私有 agent 枚举、secret 透传、回复内派发指令触发新群聊 —— 均为独立测试用例，出现任意一例即不通过。

**幂等/恢复测试**：重复 key 重放不产生新 request/run；同 key 不同 body 返回 409；重启后非终态请求可恢复，`GET status`/`GET /runs` 视图一致。

---

## 9. 依赖与开放问题

- **依赖 MARS-20**：请求记录、状态机、任务持久化、派发/收集/超时由平台服务实现；本文 API 层在其上提供 HTTP 契约与聚合订阅。若平台服务字段命名不同，以平台服务为准，本文视图字段做映射适配。
- **依赖前端 MARS-19**：前端按 `subscribe` 句柄与 `since_version`/`since_seq` 增量语义实现订阅与断线恢复。
- **可配置项**：参与者上限（默认 5）、并发配额、每请求预算、timeout 上限、workspace 默认 max_attempts（默认 3）、幂等键 TTL —— 均为 workspace 级配置，API 仅负责执行与校验。
- **待确认**：汇总按钮是否触发新的合成 run（当前设计：复用平台合成能力，不新增长周期 run）；取消端点 `POST /{request_id}/cancel` 是否纳入首发（建议纳入）。
