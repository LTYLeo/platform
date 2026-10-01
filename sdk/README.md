# tai-sdk

Official Python SDK for the **TAI Assistant API** (contract v1).

* Sync (`TAI`) and async (`AsyncTAI`) clients — both are context managers.
* Assistants, threads, messages, stateless chat, models and usage.
* Incremental SSE streaming with typed events.
* Zero-config endpoints: the API base URL is discovered from a GitHub Pages
  discovery document and cached for 12 hours, so a rotating ngrok tunnel does
  not need a code change.
* One runtime dependency: `httpx`. Python 3.9+.

---

## Install

```bash
pip install tai-sdk
```

From a checkout:

```bash
pip install -e ./sdk            # or: pip install ./sdk
pip install -e "./sdk[dev]"     # plus pytest + pytest-asyncio
```

## Quickstart

```bash
export TAI_API_KEY="sk-tai-..."     # developer dashboard → API keys
```

```python
from tai_sdk import TAI

with TAI() as client:
    print(client.base_url, client.base_url_source)   # what it resolved, and why

    reply = client.chat.create(
        model="tfmf",
        messages=[{"role": "user", "content": "Explain tail recursion."}],
    )
    print(reply.content)                 # attribute access
    print(reply.usage.output_tokens)     # typed nested object
    print(reply.raw)                     # the untouched JSON payload
```

Async is the same shape, awaited:

```python
import asyncio
from tai_sdk import AsyncTAI

async def main():
    async with AsyncTAI() as client:
        page = await client.models.list()
        for model in page.data:
            print(model.id, model.context_window)

        async for event in client.chat.stream(
            model=page.data[0].id,
            messages=[{"role": "user", "content": "Count to five."}],
        ):
            if event.event == "message.delta":
                print(event.delta, end="", flush=True)

asyncio.run(main())
```

Runnable examples live in [`examples/`](examples):
[`quickstart.py`](examples/quickstart.py), [`streaming.py`](examples/streaming.py),
[`async_chat.py`](examples/async_chat.py),
[`assistant_thread.py`](examples/assistant_thread.py).

---

## Configuration

### Base URL resolution

Checked in order; the first usable value wins. `client.base_url_source` tells
you which one was used (`"argument"`, `"env"`, `"config"`, `"discovery"` or
`"default"`).

| # | Source | Notes |
|---|---|---|
| 1 | `TAI(base_url="https://…")` | always wins |
| 2 | `TAI_BASE_URL` environment variable | |
| 3 | `~/.tai/config.json` | `{"base_url": "https://…"}` |
| 4 | Discovery document | see below; 3 s timeout, cached 12 h |
| 5 | `https://api.tai-research.dev` | last-resort default |

A value that is not a URL (no `scheme://`) is skipped, not used. Any trailing
slashes are stripped.

### Endpoint discovery (ngrok tunnels)

GitHub Pages cannot proxy requests, so it hosts a **discovery document** that
simply *points at* the current tunnel:

```
GET https://ltyleo.github.io/platform/api-endpoint.json
{
  "object": "endpoint",
  "base_url": "https://xxxx.ngrok-free.app",
  "updated_at": "2026-09-30T15:04:05+00:00",
  "note": "TAI Developer Platform API. Update this file when the tunnel changes."
}
```

* Short timeout (3 s). Any failure (offline, 404, bad JSON) is non-fatal — the
  SDK falls through to the next source, ending at `https://api.tai-research.dev`.
* The result is cached in `~/.tai/endpoint-cache.json` for **12 hours**, so the
  SDK works offline and does not hit Pages on every client construction.
* `TAI_DISCOVERY_URL` points the SDK at a different document.
* `TAI(discover=False)` disables discovery entirely.
* `TAI_DISCOVERY=0` disables it for the whole process.
* Free ngrok tunnels inject an interstitial HTML page unless they see
  `ngrok-skip-browser-warning: true` — **the SDK sends that header on every
  request**, so you never have to think about it.

```python
client = TAI(discover=False, base_url="http://127.0.0.1:8000")  # local server
```

### API key resolution

1. `TAI(api_key="sk-tai-…")`
2. `TAI_API_KEY` environment variable

If neither is set, the first request raises `TAIError` **before** touching the
network, with a message telling you to set `TAI_API_KEY`. (The server also
enforces this, with `401 missing_api_key` / `401 invalid_api_key`.)

### Timeouts and retries

```python
client = TAI(timeout=60.0, max_retries=4)
client = TAI(timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0))
```

* `timeout` defaults to **30 s** connect/read for normal requests. Streams keep
  the connect timeout but **disable the read timeout**, so a slow generation is
  never cut off mid-reply.
* `max_retries` defaults to **2**.

| Failure | Retried? |
|---|---|
| Connection error, `GET`/`DELETE`/`HEAD`/`OPTIONS`/`PUT` | yes |
| Connection error, `POST` | yes — it provably never left the client |
| Timeout (connect or read) | **no** — the server may have already processed it |
| HTTP `429`, `5xx`, idempotent method | yes, honouring `Retry-After` |
| HTTP `429`, `5xx`, `POST` | **no** — a generation may already have been recorded |
| HTTP `4xx` (other than 429) | no |

Backoff is exponential (0.5 s, 1 s, 2 s … capped at 60 s) with full jitter, and
`Retry-After` wins when the server sends it.

### Other options

```python
TAI(
    api_key=None,          # or TAI_API_KEY
    base_url=None,         # see the table above
    timeout=30.0,
    max_retries=2,
    default_headers={"X-Trace-Id": "abc"},   # merged into every request
    discover=True,
    discovery_url=None,    # or TAI_DISCOVERY_URL
    discovery_timeout=3.0,
    http_client=None,      # bring your own httpx.Client
    transport=None,        # bring your own httpx transport
)
```

`http_client=` / `transport=` are how the test suite injects
`httpx.MockTransport`; they are also useful for custom proxies, TLS settings
and connection pooling. When you pass your own client, the SDK does **not**
close it for you.

---

## API tour

All paths are relative to the resolved base URL and prefixed with `/api/v1`.
Timestamps are UTC ISO-8601 (`2026-09-30T15:04:05+00:00`). Object ids are
prefixed: `asst_`, `thrd_`, `msg_`, `chat_`.

### Assistants

```python
assistant = client.assistants.create(
    model="tfmf",                                   # required, must be live
    name="Study Buddy",                             # ≤ 80 chars
    instructions="You are a patient tutor.",        # ≤ 8000 chars
    metadata={"course": "algorithms"},              # ≤ 16 flat string values
)

page = client.assistants.list(limit=20, after=None)   # newest first
page.data, page.has_more, page.first_id, page.last_id

client.assistants.get(assistant.id)
client.assistants.update(assistant.id, name="Tutor", instructions="Be brief.")
client.assistants.delete(assistant.id)              # -> None (HTTP 204)
```

`update()` sends **only** the fields you pass, so you can PATCH one field.
Passing `None` explicitly sends a JSON `null`.

### Threads

```python
thread = client.threads.create(assistant_id=assistant.id, title="Factorial help")
# `model=` instead of `assistant_id=` is the other option; if you set neither,
# you must pass `model` when you send a message.

client.threads.get(thread.id)
client.threads.list(limit=20)
client.threads.update(thread.id, title="Renamed")       # title / assistant_id / model / metadata
client.threads.update(thread.id, assistant_id=None)     # clear the assistant
client.threads.delete(thread.id)                        # messages cascade
```

### Messages (`client.threads.messages`)

One POST appends your message **and** generates the reply, returning both plus
usage — no second round trip:

```python
exchange = client.threads.messages.create(
    thread.id,
    content="How do I reverse a list?",
    temperature=0.7,             # 0.0–2.0
    max_output_tokens=512,       # 1–8192
)
exchange.user_message.id         # msg_…
exchange.assistant_message.content
exchange.assistant_message.model
exchange.assistant_message.usage.output_tokens
exchange.usage.cost_cny
exchange.content                 # convenience: assistant_message.content
```

Reading them back (`order` defaults to `desc`):

```python
page = client.threads.messages.list(thread.id, limit=20, after=None, order="asc")
for message in page.data:
    print(message.role, message.content)
```

`client.messages` is a shortcut for `client.threads.messages`.

### Stateless chat

Nothing is persisted; `thread_id` is `null` in stream events.

```python
completion = client.chat.create(
    model="tfmf",                 # required unless you pass assistant_id
    messages=[                    # 1–100 items
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Hi"},
    ],
)
completion.id, completion.model, completion.output, completion.usage, completion.created_at
completion.content                # convenience: output["content"]

# An assistant supplies both model and instructions:
client.chat.create(assistant_id="asst_…", messages=[{"role": "user", "content": "Hi"}])
```

Messages are accepted as plain dicts, `(role, content)` tuples, or
`ChatMessage` objects.

### Models and usage

```python
for model in client.models.list().data:
    model.id, model.name, model.live, model.context_window
    model.input_cny_per_1m, model.output_cny_per_1m

client.models.retrieve("tfmf")    # client-side filter of list(); v1 has no single-model route

summary = client.usage.retrieve() # the dashboard usage_summary() payload
summary.raw["total_input_tokens"]
```

---

## Streaming

When `stream` is true the server sends `text/event-stream`:

```
event: message.start
data: {"id":"msg_…","model":"tfmf","thread_id":"thrd_…"}

event: message.delta
data: {"delta":"Hello"}

event: message.done
data: {"id":"msg_…","content":"Hello there","usage":{…}}

event: error
data: {"code":"backend_unavailable","message":"…"}
```

The SDK parses this **incrementally** (splits inside a JSON payload or even
inside a multi-byte UTF-8 character are fine) and yields typed events:

| Event | Object | Useful attributes |
|---|---|---|
| `message.start` | `MessageStart` | `.id`, `.model`, `.thread_id` (`None` for `/chat`) |
| `message.delta` | `MessageDelta` | `.delta` |
| `message.done` | `MessageDone` | `.id`, `.content`, `.usage` |
| `error` | *raises* | `APIError` subclass with `.code`, `.message` |

```python
stream = client.chat.stream(
    model="tfmf",
    messages=[{"role": "user", "content": "Tell me a story."}],
)
with stream:                                  # optional, but explicit
    for event in stream:
        if event.event == "message.delta":
            print(event.delta, end="", flush=True)
        elif event.event == "message.done":
            print("\n", event.usage)
```

Async:

```python
async for event in client.chat.stream(model="tfmf", messages=[...]):
    ...
```

Thread streams persist the assistant message and record usage exactly once:

```python
for event in client.threads.messages.create(thread.id, content="Hi", stream=True):
    ...
```

**Cleanup is guaranteed.** The HTTP response is closed when the iterator is
exhausted, when it is closed (`.close()` / `aclose()` / `with`), when the caller
`break`s out of the loop, when an exception propagates, and when the iterator is
garbage-collected. The request is opened *eagerly*, so HTTP errors (`401`, `404`,
`422`, …) raise from the `stream(...)` call itself rather than at first
iteration — with the async client, that means when you start iterating.

---

## Errors

Every failure is a `TAIError`. Anything from a server response is also an
`APIError` carrying `code`, `message`, `param`, `status_code`, `request_id`
(when the server sent one) and the decoded `body`.

```python
from tai_sdk import errors

try:
    client.assistants.create(model="does-not-exist")
except errors.ModelNotFoundError as exc:          # 404 model_not_found
    print(exc.code, exc.message, exc.status_code)
except errors.InvalidRequestError as exc:         # 422 invalid_request
    print("bad field:", exc.param)
except errors.RateLimitError as exc:              # 429 rate_limited
    print("retry after", exc.retry_after, exc.request_id)
except errors.APIConnectionError as exc:          # never reached the server
    print("tried:", exc.base_url)
except errors.APIError as exc:                    # any other non-2xx
    print(exc.status_code, exc.code, exc.message)
except errors.TAIError as exc:                    # anything the SDK raises
    print(exc)
```

| Code | HTTP | Exception |
|---|---|---|
| `missing_api_key`, `invalid_api_key` | 401 | `AuthenticationError` |
| `account_disabled` | 403 | `PermissionDeniedError` |
| `insufficient_balance` | 402 | `InsufficientBalanceError` |
| `not_found` | 404 | `NotFoundError` |
| `model_not_found` | 404 | `ModelNotFoundError` (a `NotFoundError`) |
| `model_not_available` | 409 | `ModelNotAvailableError` (a `ConflictError`) |
| `invalid_request` | 422 | `InvalidRequestError` |
| `rate_limited` | 429 | `RateLimitError` |
| `backend_unavailable` | 502 | `BackendUnavailableError` |
| `stream_aborted` | 499 | `StreamAbortedError` |
| anything unmapped | any | `UnknownAPIError` (keeps the server's `.code`) |

`APIConnectionError` and `APITimeoutError` are `TransportError`s whose message
**includes the resolved base URL**, so you can see exactly which tunnel was
tried.

---

## Response objects

Small hand-written dataclasses (no pydantic). Every one supports attribute
access, `.raw` (the untouched dict), `repr()` and `.to_dict()`:

```python
assistant.id            # "asst_…"
assistant.raw           # the full JSON payload from the server
repr(assistant)         # Assistant(id='asst_…', object='assistant', …)
page = client.assistants.list()
len(page), list(page), page[0]     # list envelopes are sequences
```

Nested objects are typed too (`message.usage.output_tokens`,
`exchange.assistant_message.model`). Unknown fields are never dropped — they
stay in `.raw`, so a server-side addition cannot break your code.

`ChatMessage` is the one object that is *input*, not output:

```python
from tai_sdk import ChatMessage
ChatMessage(role="user", content="Hi")
```

### Low-level escape hatch

If the contract grows a route before the SDK does:

```python
payload = client.request("GET", "/api/v1/models")                 # -> raw dict
stream = client.stream("POST", "/api/v1/chat", json_body={...})   # -> events

await client.request("GET", "/api/v1/models")                     # AsyncTAI
async for event in await client.astream("POST", "/api/v1/chat", json_body={...}):
    ...
```

---

## Resource lifecycle

```python
with TAI() as client:      # closes the connection pool on exit
    ...

client = TAI()
try:
    ...
finally:
    client.close()         # idempotent

async with AsyncTAI() as client:   # aclose() on exit
    ...

await client.aclose()
```

Using a closed client raises `RuntimeError`.

---

## Development

```bash
python -m pytest tests -q          # no server needed: httpx.MockTransport
python -m build                    # -> dist/tai_sdk-0.1.0-py3-none-any.whl
```

The suite covers URL and header construction, base-URL/API-key resolution
order, discovery caching, every resource happy path, SSE parsing (including a
split chunk boundary and a mid-stream `error` event), error mapping for
401/404/409/422/429, retry-then-succeed, and stream cleanup on early exit.

## License

MIT © TAI Research — see [LICENSE](LICENSE).
