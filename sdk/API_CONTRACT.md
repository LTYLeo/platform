# TAI Assistant API — contract v1

This is the interface that **both** the server implementation and the Python SDK
must follow exactly. Nothing here is OpenAI-compatible on purpose; it is our own
shape.

Base path: `/api/v1`
Auth: `Authorization: Bearer sk-tai-...` on every endpoint.
Content type: `application/json` (except SSE streams).

---

## 1. Conventions

### 1.1 Error shape

Every non-2xx response is:

```json
{ "code": "invalid_request", "message": "Human readable sentence", "param": "model" }
```

`param` is optional. Stable codes:

| code | HTTP | meaning |
|---|---|---|
| `missing_api_key` | 401 | no `Authorization: Bearer` header |
| `invalid_api_key` | 401 | unknown or revoked key |
| `account_disabled` | 403 | the owning account is disabled |
| `insufficient_balance` | 402 | balance exhausted (reserved; not enforced yet) |
| `not_found` | 404 | no such object, or it belongs to another account |
| `invalid_request` | 422 | validation failed |
| `model_not_available` | 409 | the model exists but is not live (e.g. still training) |
| `model_not_found` | 404 | unknown model id |
| `rate_limited` | 429 | too many requests |
| `backend_unavailable` | 502 | the model backend failed |
| `stream_aborted` | 499 | client disconnected mid-stream (server-side only) |

### 1.2 Object ids

Opaque strings with a type prefix, generated with `secrets.token_urlsafe(18)`:

```
asst_<token>   assistant
thrd_<token>   thread
msg_<token>    message
chat_<token>   stateless chat completion
```

### 1.3 Timestamps

UTC ISO-8601 with a `Z`-less `+00:00` offset, second precision:
`2026-09-30T15:04:05+00:00`. Field names are `created_at` / `updated_at`.

### 1.4 Pagination

List endpoints accept `limit` (1–100, default 20) and `after` (an object id).
They return:

```json
{ "object": "list", "data": [ ... ], "has_more": false, "first_id": "asst_…", "last_id": "asst_…" }
```

`data` is newest-first.

---

## 2. Assistants

A reusable configuration: model + system instructions + a name.

```
POST   /api/v1/assistants
GET    /api/v1/assistants?limit=&after=
GET    /api/v1/assistants/{assistant_id}
PATCH  /api/v1/assistants/{assistant_id}
DELETE /api/v1/assistants/{assistant_id}          -> 204
```

**Assistant object**

```json
{
  "id": "asst_…",
  "object": "assistant",
  "name": "Study Buddy",
  "model": "tfmf",
  "instructions": "You are a patient tutor.",
  "metadata": {},
  "created_at": "2026-09-30T15:04:05+00:00",
  "updated_at": "2026-09-30T15:04:05+00:00"
}
```

**Create body**

| field | type | required | notes |
|---|---|---|---|
| `model` | string | yes | must be a **live** model id |
| `name` | string | no | 1–80 chars, default `"Assistant"` |
| `instructions` | string | no | ≤ 8000 chars, default `""` |
| `metadata` | object | no | ≤ 16 keys, flat string values |

**PATCH body** — any subset of `name`, `model`, `instructions`, `metadata`.

---

## 3. Threads

A persistent conversation.

```
POST   /api/v1/threads
GET    /api/v1/threads?limit=&after=
GET    /api/v1/threads/{thread_id}
PATCH  /api/v1/threads/{thread_id}
DELETE /api/v1/threads/{thread_id}                -> 204
```

**Thread object**

```json
{
  "id": "thrd_…",
  "object": "thread",
  "title": "Factorial help",
  "assistant_id": "asst_…",
  "metadata": {},
  "message_count": 4,
  "created_at": "…",
  "updated_at": "…"
}
```

**Create body**

| field | type | required | notes |
|---|---|---|---|
| `assistant_id` | string | no | if omitted, `model` is required at message time |
| `model` | string | no | default model for this thread when no assistant is set |
| `title` | string | no | default `"New thread"` |
| `metadata` | object | no | |

**PATCH body** — any subset of `title`, `assistant_id`, `model`, `metadata`.

Deleting a thread deletes its messages (cascade).

---

## 4. Messages

```
POST /api/v1/threads/{thread_id}/messages        append + generate
GET  /api/v1/threads/{thread_id}/messages?limit=&after=&order=asc|desc
```

### 4.1 POST body

| field | type | default | notes |
|---|---|---|---|
| `content` | string | — | required, 1–32000 chars |
| `stream` | bool | `false` | see §6 |
| `temperature` | float | `0.7` | 0.0–2.0 |
| `max_output_tokens` | int | `512` | 1–8192 |

Also accepts an optional `role` (only `"user"` is allowed from clients).

### 4.2 Non-streaming response (200)

Both messages are returned so the caller does not need a second round trip:

```json
{
  "object": "message.exchange",
  "thread_id": "thrd_…",
  "user_message": { …message… },
  "assistant_message": { …message… },
  "usage": {
    "model": "tfmf",
    "input_tokens": 128,
    "output_tokens": 42,
    "cost_cny": 0.00019,
    "estimated": true
  }
}
```

### 4.3 Message object

```json
{
  "id": "msg_…",
  "object": "message",
  "thread_id": "thrd_…",
  "role": "user",
  "content": "How do I reverse a list?",
  "model": null,
  "usage": null,
  "created_at": "…"
}
```

`model` and `usage` are populated on assistant messages only.

### 4.4 GET

Returns a list envelope (§1.4) of message objects. `order` defaults to `desc`.

---

## 5. Stateless chat

```
POST /api/v1/chat
```

| field | type | default | notes |
|---|---|---|---|
| `model` | string | — | required unless `assistant_id` given |
| `assistant_id` | string | — | supplies model + instructions |
| `messages` | array | — | 1–100 items, each `{role, content}`, role in `user`/`assistant`/`system` |
| `stream` | bool | `false` | |
| `temperature` | float | `0.7` | |
| `max_output_tokens` | int | `512` | |

**Response (200)**

```json
{
  "id": "chat_…",
  "object": "chat.completion",
  "model": "tfmf",
  "output": { "role": "assistant", "content": "…" },
  "usage": { "input_tokens": 12, "output_tokens": 40, "cost_cny": 0.000126, "estimated": true },
  "created_at": "…"
}
```

---

## 6. Streaming (SSE)

When `stream` is `true` the response is `text/event-stream` with these events.
Every `data:` line is a JSON object on a single line.

```
event: message.start
data: {"id":"msg_…","model":"tfmf","thread_id":"thrd_…"}

event: message.delta
data: {"delta":"Hello"}

event: message.done
data: {"id":"msg_…","content":"Hello there","usage":{"input_tokens":12,"output_tokens":40,"cost_cny":0.000126,"estimated":true}}

event: error
data: {"code":"backend_unavailable","message":"…"}
```

Rules:

* `message.start` is always first; `message.done` or `error` is always last.
* `thread_id` is `null` for `/api/v1/chat` streams.
* On completion the assistant message is persisted (thread streams only) and
  `usage` is recorded exactly once, then the dashboard reflects it.
* A client disconnect must still persist whatever was generated and record usage.

---

## 7. Models and usage

```
GET /api/v1/models     -> list of live models
GET /api/v1/usage      -> same body as the cookie-authenticated /api/usage
```

`GET /api/v1/models` response:

```json
{
  "object": "list",
  "data": [
    {
      "id": "tfmf",
      "object": "model",
      "name": "TFMF",
      "live": true,
      "context_window": 8192,
      "input_cny_per_1m": 0.5,
      "output_cny_per_1m": 3.0
    }
  ]
}
```

`GET /api/v1/usage` returns the `db.usage_summary()` payload plus
`"object": "usage"`.

---

## 8. Server implementation notes

* New tables: `assistants`, `threads`, `messages`. All scoped by `user_id` and
  cascading on user delete. New columns `threads.model`, `messages.model`,
  `messages.usage_json`.
* Every generation path must call `db.record_usage(...)` **exactly once**, so the
  existing dashboard totals stay correct. `GET /api/usage` must not break.
* The model backend is pluggable, selected by `TAI_MODEL_BACKEND`:
  * `stub` (default) — deterministic, offline, no model. Responses carry
    `"backend": "stub"` so nobody mistakes them for real output.
  * `openai_compatible` — POSTs `TAI_MODEL_BASE_URL` + `/chat/completions` with
    `TAI_MODEL_API_KEY`. Works with vLLM, llama.cpp server, Ollama, LM Studio.
* Token counts: use the backend's reported usage when present, otherwise the
  documented estimate `max(1, ceil(len(text)/4))`, and set `"estimated": true`.
* `stub` must never claim to be a real model in the response.

## 9. SDK notes

* Package `tai-sdk`, import name `tai_sdk`, `src/` layout, `pyproject.toml`
  with hatchling.
* Runtime dependency: `httpx` only.
* Public surface: `TAI` (sync) and `AsyncTAI` (async), plus `errors` types.
* Resource namespaces: `client.assistants`, `client.threads`,
  `client.threads.messages`, `client.chat`, `client.models`, `client.usage`.
* Base URL resolution order:
  1. `base_url=` argument
  2. `TAI_BASE_URL` environment variable
  3. `~/.tai/config.json` (`{"base_url": "…"}`)
  4. the discovery document (§10), cached under `~/.tai/endpoint-cache.json`
  5. `https://api.tai-research.dev` (last-resort default)
* API key resolution: `api_key=` argument, then `TAI_API_KEY`.
* Send `ngrok-skip-browser-warning: true` on every request so free ngrok tunnels
  do not inject their interstitial HTML into API responses.
* Retry idempotent requests (GET/DELETE) and connection errors up to
  `max_retries` times with exponential backoff + jitter. Never retry a
  non-idempotent POST unless the failure happened before the request was sent.
* Streaming must be incremental, and the sync iterator must close the response
  when the caller breaks out of the loop.

## 10. Endpoint discovery

GitHub Pages cannot proxy requests, so it is used as a **discovery document**
instead. `TAI()` with no `base_url` fetches:

```
GET https://ltyleo.github.io/platform/api-endpoint.json
```

```json
{
  "object": "endpoint",
  "base_url": "https://xxxx.ngrok-free.app",
  "updated_at": "2026-09-30T15:04:05+00:00",
  "note": "TAI Developer Platform API. Update this file when the tunnel changes."
}
```

Rules:

* Short timeout (3 s). Failure is non-fatal — fall through to the next source.
* Cache the result in `~/.tai/endpoint-cache.json` and reuse it for 12 hours, so
  the SDK works offline and does not hit Pages on every client construction.
* `TAI_DISCOVERY_URL` overrides the document location; `discover=False`
  disables it entirely.
* Because the ngrok URL can change, every SDK error message about connectivity
  should mention the resolved base URL so users can see what it tried.
