# MARS-20 / MARS-22 契约对齐记录（离线）

对照来源：
- MARS-22 API 规范：分支 `agent/workbuddy/6b486440fdb3`，commit `9f0e97f`，`specs/multi-agent-chat-api.md`
- Codex 草案：issue 附件 `mars20-22-contract.md`
- 基线原型：MARS-20 commit `881c52f`

本文只覆盖离线契约与状态机对齐；未启动产品代码接入、run 订阅/Steer 实测或 Pilot。

## 契约差异与修改

| 契约点 | 原型 `881c52f` | 对齐后 | 证据 |
| --- | --- | --- | --- |
| 幂等键作用域 | 仅 `idempotency_key` 全局唯一，同键异载荷也返回旧请求 | `(workspace, initiator, idempotency_key)` 索引 + 请求体哈希；同键同体重放、异体 `409 idempotency_key_reused` | `storage.create_request_atomic` / `service._body_hash`；`test_idempotency_and_restart`、`test_same_key_different_body_is_conflict` |
| attempt 记录 | 无独立 attempt 实体，仅 `attempt_count` 整数 | `Attempt` 实体 + 稳定键 `mt:<request>:<agent-or-task>:<attempt_no>`，与待派发记录同事务创建；重放同键返回同一记录与 run_id | `models.Attempt`、`storage.get_or_create_attempt`、`service._attempt`；`test_enqueue_ack_loss_replays_same_key_only` |
| 审核并发 | 进程内 Lock，跨实例可双写；重复审核抛 400 | SQLite `BEGIN IMMEDIATE` 读改写 + `task.version` / `If-Match` 乐观锁；重复同决策返回原结果，陈旧决策 `412`，不同决策不落副作用 | `storage.update_atomic`、`service.review_task`、`api.review_task`；`test_concurrent_reviews_land_single_decision`、`test_repeated_decision_is_idempotent_and_conflict_rejected` |
| 尝试上限语义 | 达上限后拒绝新审核但语义未绑定 attempt | 决策绑定 `attempt_no` 与版本；第三次拒绝 `attempt_count=3 >= max_attempts` 直接 failed，不创建第四次派发 | `service.review_task`；`test_third_reject_never_creates_fourth_attempt` |
| 终态与迟到 | `partial → completed` 可被迟到回复翻转 | `completed/partial/failed/cancelled` 均为终态；迟到只写入 `late_replies` + trace，不改正文/状态/汇总 | `state_machine.REQUEST_TRANSITIONS`、`service.ingest_late_reply`；`test_timeout_then_late_reply_does_not_change_terminal`、`test_partial_is_terminal_even_on_late_reply` |
| 取消 | 无 | `POST /{request_id}/cancel`；非终态可取消并停止后续派发，终态 `409` | `service.cancel_request`、`api.cancel`；`test_cancel_is_terminal_and_late_reply_cannot_reopen` |
| Linear 依赖失败 | 下游标 `failed(dependency_failed)` | 下游标 `skipped`，不再尝试派发 | `service._run_linear`；`test_linear_injects_context_and_skips_downstream` |
| 多数失败降级 | 无 | 分母为计划执行单元（非 attempt），>50% 失败给出 `human_takeover_or_single_expert` 建议且保留合法结果 | `service._degradation_hint` |
| 断线恢复 | 全量 GET + audit 列表 | 请求带 `version`、快照 `since_version`/`304`、`GET /runs`、`GET /trace?since_seq`、`run_ids` 一次性订阅 | `service.get_status/get_runs/get_trace`、`api` 路由；`test_snapshot_runs_trace_and_incremental_reads` |
| 错误契约 | 裸 `detail` 字符串，429 表示参与者超限 | `{"error":{"code","message","details"}}`；401/400/403/404/409/412/422/429 按 MARS-22 码表 | `errors.PlatformError`、`api._error`；`test_error_status_mapping` |
| 审核反馈校验 | 仅要求 `reject_reason` | `reject_reason` + `change_instructions` 均必填，并注入下一次 worker 载荷 | `service.review_task`；`test_review_feedback_required`、`test_supervisor_reject_retry_passes_feedback_to_worker` |

## 四项优先验证（结果）

1. **第三次拒绝不创建第四次尝试**：`test_third_reject_never_creates_fourth_attempt`
   - 观察：`attempt_count=3`、`status=failed`、worker 实际 enqueue 次数 = 3；第四次审核被 `InvalidReviewTransition` 拒绝，enqueue 次数仍为 3。
2. **入队确认丢失只重放同一键**：`test_enqueue_ack_loss_replays_same_key_only`
   - 观察：`downstream_key` 与 `run_id` 不变；重复 `_attempt` / 重复 `enqueue(same key)` 后 dispatcher 调用数仍为 1。
3. **并发审核只落一个决定**：`test_concurrent_reviews_land_single_decision`
   - 观察：8 线程并发，`task_reviewed` 审计事件恰好 1 条；`version` 只递增 1 次，worker enqueue 次数仍为 1；其余请求被 `412`/冲突短路。
4. **超时或取消后迟到不改终态**：`test_timeout_then_late_reply_does_not_change_terminal`、`test_cancel_is_terminal_and_late_reply_cannot_reopen`
   - 观察：超时后 `partial`、取消后 `cancelled` 均保持；迟到内容进入 `late_replies` 且 `included_in_summary=false`，participant 正文与汇总来源数不变。

## 测试

```
python3 -m pytest -q
38 passed
```

## 未对齐 / 仍需外部条件

- `Dispatcher` / `AuthorizationProvider` 仍为 `FakeDispatcher` / `StaticAuthorizationProvider`；真实 mention→enqueue、run 订阅/Steer、跨请求读权限需产品仓库与联调环境（MARS-13 待确认）。
- preview 成本估算、ETag/If-None-Match、长轮询、幂等键 TTL、workspace 级配置尚未实现（契约中标注为可选/后续）。
- 越权 request_id 统一返回 404 的资源枚举防护在 API 层尚未按 3.2 全部落地。
