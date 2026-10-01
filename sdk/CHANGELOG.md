# Changelog

All notable changes to `tai-sdk` are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0 — 2026-09-30

First public release, implementing the TAI Assistant API contract v1.

### Added

- `TAI` (sync) and `AsyncTAI` (async) clients, both usable as context managers.
- Resource namespaces: `client.assistants`, `client.threads`,
  `client.threads.messages` (also `client.messages`), `client.chat`,
  `client.models`, `client.usage`.
- Assistants: create, list (with `limit`/`after` pagination), get, update
  (`PATCH`), delete.
- Threads: create, list, get, update, delete (messages cascade server-side).
- Messages: append + generate (returns both the user and assistant message plus
  usage in one round trip), and list with `limit`/`after`/`order`.
- Stateless chat: `client.chat.create(...)`.
- Models list and account usage summary.
- Incremental SSE streaming via `client.chat.stream(...)` and
  `client.threads.messages.create(..., stream=True)`, yielding typed
  `MessageStart`, `MessageDelta` and `MessageDone` events and raising on
  `error` frames. Works with `for` (sync) and `async for` (async); the HTTP
  response is always closed, including when the caller breaks out early.
- Base URL resolution: `base_url=` argument, `TAI_BASE_URL`,
  `~/.tai/config.json`, the GitHub Pages discovery document (3 s timeout,
  cached 12 h in `~/.tai/endpoint-cache.json`), then
  `https://api.tai-research.dev`. `discover=False` disables discovery and
  `TAI_DISCOVERY_URL` relocates the document.
- API key resolution: `api_key=` argument, then `TAI_API_KEY`.
- `ngrok-skip-browser-warning: true` on every request.
- Full error hierarchy: every non-2xx becomes the matching exception carrying
  `code`, `message`, `param`, `status_code` and `request_id`. Connectivity
  failures include the resolved base URL in the message.
- Retries with exponential backoff and jitter on connection errors and
  HTTP 429/5xx, honouring `Retry-After`. Only idempotent methods (plus POSTs
  that provably never left the client) are retried; `max_retries` defaults to 2.
- Configurable timeouts (30 s connect/read by default; the read timeout is
  disabled for streams).
- Response objects are small dataclasses with attribute access, a `.raw` dict
  and a `__repr__`. No pydantic.
- Typed package (`py.typed`), Python 3.9+, runtime dependency: `httpx` only.
