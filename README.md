# multi-agent-chat-orchestrator

Platform service for multi-agent group chat (MARS-20): durable request records,
request & supervisor-task state machines, three execution strategies, invoke-gate
security, budgets and audit.

## Layout

```
src/multica_chat/
  config.py          server-side limits (participant cap, depth, budgets)
  models.py          request / participant / task / audit domain models
  state_machine.py   request & task transition tables + guards
  storage.py         SQLite persistence, request idempotency, audit trail
  security.py        session-derived identity, invoke gate, payload whitelist,
                     loop detection, untrusted-text helpers
  dispatcher.py      Dispatcher interface + deterministic FakeDispatcher
  service.py         orchestration: linear / parallel / supervisor, aggregation
  api.py             FastAPI routes
tests/               acceptance suite (pytest)
```

## Key invariants

- **Identity is server-derived.** `initiator_id`, `workspace_id`, `session_id`
  come from the authenticated session, never from the request body. Only a human
  session can originate a group chat; agent delegation never becomes the chain top.
- **Preview is advisory.** Every participant's `can_invoke` is re-checked at actual
  dispatch. Revoked or invisible agents are not dispatched and their existence /
  online status is not exposed.
- **No secret propagation.** Payloads are reduced to a whitelist
  (`user_prompt`, `task`, `context`, `attachments`, `metadata`, `feedback`) and
  scrubbed recursively; `custom_env`, `mcp_config`, credentials, connector config,
  tool logs and private history never cross an agent boundary.
- **Idempotency at two levels.** A unique `idempotency_key` collapses client
  resends onto one request; deterministic downstream keys
  (`mt:<request>:<agent>:<attempt>`) cover enqueue-ack loss, restarts and retries.
- **State machines are enforced.** Requests:
  `pending → queued → running → completed | failed | partial` (with a controlled
  `partial → completed` recovery for late replies). Supervisor tasks:
  `pending → assigned → in_progress → submitted → reviewed → approved → completed`,
  with `rejected → assigned` only while `attempt_count < max_attempts`.
- **Aggregation treats agent text as untrusted.** Replies are cited by `run_id`,
  summarized, disagreement preserved, read permission re-checked before inclusion,
  and embedded "start a new group chat" instructions are ignored (first release
  disables participant-triggered chats).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/chat/trigger-preview` | advisory preview of invocable agents |
| POST | `/api/v1/chat/multi-trigger` | create & run a group-chat request |
| GET | `/api/v1/chat/multi-trigger/{request_id}` | status / disconnect recovery |
| GET | `/api/v1/chat/multi-trigger/{request_id}/audit` | audit trail |
| POST | `/api/v1/chat/multi-trigger/{request_id}/tasks/{task_id}/review` | supervisor approve / reject |
| POST | `/api/v1/chat/multi-trigger/{request_id}/late-reply` | ingest a late/timed-out reply |

All endpoints require the `X-Session-Token` header. Errors map to
403 (authorization), 429 (budget), 409 (invalid transition), 400 (validation),
404 (not found).

## Run & test

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest -q
python3 -m uvicorn multica_chat.main:app --reload --app-dir src
```

`AuthorizationProvider` and `Dispatcher` are interfaces: swap
`StaticAuthorizationProvider` / `FakeDispatcher` for the platform's real session
gate and mention→enqueue adapter in production.
