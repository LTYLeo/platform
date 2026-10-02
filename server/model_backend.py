"""Pluggable model backend.

Both generation paths -- ``POST /api/v1/threads/{id}/messages`` and
``POST /api/v1/chat`` -- go through this module, so the interface is deliberately
tiny:

.. code-block:: python

    generate(model, messages, *, temperature, max_output_tokens) -> GenerationResult
    stream(model, messages, *, temperature, max_output_tokens)   -> StreamHandle

``StreamHandle`` *is* an ``Iterator[str]`` (deltas), and additionally exposes
``.result`` -- a :class:`GenerationResult` summarising the whole stream once it
has finished (or been closed early). Without that, an SSE caller could not use
upstream-reported token usage, which the contract requires when it is available.

Two implementations, picked by ``TAI_MODEL_BACKEND``:

``stub`` (default)
    Deterministic and fully offline. It echoes the last user message and labels
    itself with the literal marker ``[stub backend]``; it never pretends to be a
    real model.

``openai_compatible``
    ``POST {TAI_MODEL_BASE_URL}/chat/completions`` with an optional
    ``Authorization: Bearer {TAI_MODEL_API_KEY}``. This is the shape spoken by
    vLLM, llama.cpp's server, Ollama and LM Studio, so pointing the platform at
    a real model is a configuration change, not a code change. ``stream=true``
    is passed straight through and the upstream SSE is translated to deltas.

Environment
-----------
==========================  =========================  ==========================
``TAI_MODEL_BACKEND``       ``stub``                   ``stub`` | ``openai_compatible``
``TAI_MODEL_BASE_URL``      *(none, required)*         e.g. ``http://127.0.0.1:8001/v1``
``TAI_MODEL_API_KEY``       *(none)*                   sent as a Bearer token when set
``TAI_MODEL_TIMEOUT_S``     ``120``                    read timeout for one request
``TAI_MODEL_STREAM_USAGE``  ``false``                  ask upstream for usage in streams
``TAI_STUB_STREAM_DELAY_MS`` ``0``                     artificial per-chunk delay (tests)
==========================  =========================  ==========================

Token accounting
----------------
One documented rule, used everywhere: an upstream-reported ``usage`` block wins
(``estimated=False``); otherwise both directions are estimated with
:func:`estimate_tokens` and ``estimated`` is ``True``.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Iterator

try:  # pragma: no cover - exercised only when httpx is missing
    import httpx
except Exception:  # noqa: BLE001 - a missing httpx must only break the HTTP backend
    httpx = None  # type: ignore[assignment]

STUB = "stub"
OPENAI_COMPATIBLE = "openai_compatible"
SIGMA = "sigma"          # talk to Sigma's own /generate (kept for rollback)
INFERENCE = "inference"  # talk to the dedicated inference service
KNOWN_BACKENDS = (STUB, OPENAI_COMPATIBLE, SIGMA, INFERENCE)

#: The literal marker every stub reply carries, so nothing can be mistaken for a
#: real model answer (contract §8).
STUB_MARKER = "[stub backend]"

#: Characters per delta in the fake stream. Small enough that a caller can see
#: incremental delivery, large enough not to be silly.
STUB_CHUNK_CHARS = 24

#: How much of the user's text the stub echoes back.
STUB_ECHO_CHARS = 160


class BackendUnavailable(RuntimeError):
    """The model backend could not produce a completion.

    Every transport error, upstream HTTP error and malformed upstream payload
    becomes this, which the API surfaces as ``backend_unavailable`` (502).
    """


@dataclass(frozen=True)
class GenerationResult:
    """One completed generation, with the token counts that get billed."""

    text: str
    input_tokens: int
    output_tokens: int
    estimated: bool


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def backend_name() -> str:
    """The configured backend id, lower-cased. Unknown values are rejected."""
    name = (os.getenv("TAI_MODEL_BACKEND") or STUB).strip().lower()
    if name not in KNOWN_BACKENDS:
        raise BackendUnavailable(
            f"Unknown TAI_MODEL_BACKEND={name!r}; expected one of: {', '.join(KNOWN_BACKENDS)}"
        )
    return name


# --------------------------------------------------------------------------- #
# Token accounting -- the single documented rule
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    """The documented fallback estimate: ``max(1, ceil(len(text) / 4))``.

    Used whenever the backend does not report real usage. Roughly four
    characters per token for English; it is deliberately simple and stable so
    that the same input always bills the same amount.
    """
    return max(1, math.ceil(len(text) / 4))


def prompt_text(messages: list[dict[str, Any]]) -> str:
    """Flatten a message list the same way for every backend (used to estimate
    input tokens when the upstream reports nothing)."""
    return "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)


def _usage_from_upstream(usage: Any) -> tuple[int, int] | None:
    """``(input, output)`` from an OpenAI-style usage block, or ``None``."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if prompt is None or completion is None:
        return None
    try:
        return int(prompt), int(completion)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# StreamHandle
# --------------------------------------------------------------------------- #

class StreamHandle(Iterator[str]):
    """An iterator of text deltas that also knows its final token usage.

    ``result`` is computed lazily, so calling it after a partial read (a client
    that disconnected mid-stream) still returns a billable estimate for exactly
    the text that was produced.
    """

    def __init__(self, inner: Iterator[str], state: dict[str, Any], messages: list[dict[str, Any]]):
        self._inner = inner
        self._state = state
        self._messages = messages
        self._result: GenerationResult | None = None

    # -- Iterator[str] ----------------------------------------------------- #

    def __iter__(self) -> "StreamHandle":
        return self

    def __next__(self) -> str:
        chunk = next(self._inner)
        self._state.setdefault("parts", []).append(chunk)
        return chunk

    def close(self) -> None:
        """Release the upstream connection, if any. Safe to call twice."""
        closer = getattr(self._inner, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass

    # -- Accounting -------------------------------------------------------- #

    @property
    def text(self) -> str:
        return "".join(self._state.get("parts", []))

    @property
    def result(self) -> GenerationResult:
        if self._result is None:
            text = self.text
            reported = _usage_from_upstream(self._state.get("usage"))
            if reported is not None:
                self._result = GenerationResult(
                    text, reported[0], reported[1], estimated=False
                )
            else:
                self._result = GenerationResult(
                    text,
                    estimate_tokens(prompt_text(self._messages)),
                    estimate_tokens(text),
                    estimated=True,
                )
        return self._result


# --------------------------------------------------------------------------- #
# stub backend
# --------------------------------------------------------------------------- #

def _stub_reply(model: str, messages: list[dict[str, Any]]) -> str:
    last_user = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            last_user = " ".join(str(message.get("content", "")).split())
            break
    if len(last_user) > STUB_ECHO_CHARS:
        last_user = last_user[:STUB_ECHO_CHARS] + "..."

    return (
        f"{STUB_MARKER} No language model is connected, so this is an echo and not "
        f"a model answer. model={model}; messages={len(messages)}. "
        f"Last user message: {last_user!r}"
    )


def _stub_generate(
    model: str, messages: list[dict[str, Any]], *, temperature: float, max_output_tokens: int
) -> GenerationResult:
    text = _stub_reply(model, messages)
    return GenerationResult(
        text=text,
        input_tokens=estimate_tokens(prompt_text(messages)),
        output_tokens=estimate_tokens(text),
        estimated=True,
    )


def _stub_stream(
    model: str, messages: list[dict[str, Any]], *, temperature: float, max_output_tokens: int
) -> Iterator[str]:
    delay = max(0.0, _env_float("TAI_STUB_STREAM_DELAY_MS", 0.0)) / 1000.0
    text = _stub_reply(model, messages)
    for start in range(0, len(text), STUB_CHUNK_CHARS):
        if delay:
            time.sleep(delay)
        yield text[start : start + STUB_CHUNK_CHARS]


# --------------------------------------------------------------------------- #
# openai_compatible backend
# --------------------------------------------------------------------------- #

def _base_url() -> str:
    url = (os.getenv("TAI_MODEL_BASE_URL") or "").strip().rstrip("/")
    if not url:
        raise BackendUnavailable(
            "TAI_MODEL_BACKEND=openai_compatible requires TAI_MODEL_BASE_URL "
            "(for example http://127.0.0.1:8001/v1)"
        )
    return url


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = (os.getenv("TAI_MODEL_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _require_httpx() -> None:
    if httpx is None:  # pragma: no cover
        raise BackendUnavailable(
            "TAI_MODEL_BACKEND=openai_compatible needs the 'httpx' package "
            "(pip install httpx)"
        )


def _timeout() -> Any:
    read = max(1.0, _env_float("TAI_MODEL_TIMEOUT_S", 120.0))
    return httpx.Timeout(connect=5.0, read=read, write=15.0, pool=5.0)


def _payload(
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_output_tokens: int,
    stream: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
        ],
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "stream": stream,
    }
    if stream and _env_bool("TAI_MODEL_STREAM_USAGE", False):
        # Only sent when explicitly enabled: some OpenAI-compatible servers
        # reject the field, and the estimate is an honest fallback.
        payload["stream_options"] = {"include_usage": True}
    return payload


def _generate_openai(
    model: str, messages: list[dict[str, Any]], *, temperature: float, max_output_tokens: int
) -> GenerationResult:
    _require_httpx()
    url = f"{_base_url()}/chat/completions"
    try:
        with httpx.Client(timeout=_timeout(), headers=_headers()) as client:
            response = client.post(
                url,
                json=_payload(
                    model,
                    messages,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    stream=False,
                ),
            )
            if response.status_code >= 400:
                raise BackendUnavailable(
                    f"upstream {url} returned HTTP {response.status_code}: "
                    f"{response.text[:400]}"
                )
            data = response.json()
    except BackendUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - transport, TLS, timeout, bad JSON
        raise BackendUnavailable(f"could not reach {url}: {exc}") from exc

    try:
        choice = (data.get("choices") or [])[0]
        text = choice["message"]["content"] or ""
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise BackendUnavailable(
            f"upstream {url} returned no choices[0].message.content"
        ) from exc

    reported = _usage_from_upstream(data.get("usage"))
    if reported is not None:
        return GenerationResult(str(text), reported[0], reported[1], estimated=False)
    return GenerationResult(
        text=str(text),
        input_tokens=estimate_tokens(prompt_text(messages)),
        output_tokens=estimate_tokens(str(text)),
        estimated=True,
    )


def _iter_openai_sse(response: Any, url: str, state: dict[str, Any]) -> Iterator[str]:
    """Translate an OpenAI-style SSE body into text deltas, capturing usage."""
    try:
        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
            else:
                continue
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("usage"):
                state["usage"] = event["usage"]
            if not isinstance(event, dict):
                continue
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield str(content)
    except BackendUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - connection dropped mid-stream
        raise BackendUnavailable(f"stream from {url} failed: {exc}") from exc


def _stream_openai(
    model: str, messages: list[dict[str, Any]], *, temperature: float, max_output_tokens: int
) -> tuple[Iterator[str], dict[str, Any]]:
    """Open the upstream stream *eagerly* so transport errors can still be a 502.

    Returns ``(iterator, state)``; ``state`` receives the ``usage`` block if the
    upstream sends one.
    """
    _require_httpx()
    url = f"{_base_url()}/chat/completions"
    payload = _payload(
        model,
        messages,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        stream=True,
    )
    state: dict[str, Any] = {}

    client = httpx.Client(timeout=_timeout(), headers=_headers())
    try:
        request = client.build_request("POST", url, json=payload)
        response = client.send(request, stream=True)
        if response.status_code >= 400:
            body = response.read().decode("utf-8", "replace")[:400]
            response.close()
            raise BackendUnavailable(
                f"upstream {url} returned HTTP {response.status_code}: {body}"
            )
    except BackendUnavailable:
        client.close()
        raise
    except Exception as exc:  # noqa: BLE001
        client.close()
        raise BackendUnavailable(f"could not reach {url}: {exc}") from exc

    def _closing_iterator() -> Iterator[str]:
        try:
            yield from _iter_openai_sse(response, url, state)
        finally:
            response.close()
            client.close()

    return _closing_iterator(), state


# --------------------------------------------------------------------------- #
# Public interface
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# sigma backend
# --------------------------------------------------------------------------- #
# Sigma is the chat platform; it holds the GTC models in memory on the same box.
# Rather than loading a second copy of every checkpoint, the API calls Sigma's
# /generate. Sigma has no per-token accounting of its own, so usage is estimated
# and flagged, exactly as the stub does - it is never passed off as measured.

# Platform model id -> the name Sigma's /generate expects. Sigma's own model
# selector calls GTC-2.5 mini "gtc25" and GTC-2.5 "gtc25_400m".
SIGMA_MODEL_ALIASES: dict[str, str] = {
    "gtc-2.5-mini": "gtc25",
    "gtc-2.5": "gtc25_400m",
    "gtc-2.5v": "gtc25v",
    "gtc-2.5o": "gtc25o",
    "esft-gtc-2": "esft",
}


def _sigma_url() -> str:
    """Where to send generations.

    ``sigma`` points at the chat platform's own /generate; ``inference`` points at
    the dedicated inference service. Same wire format, so they share this code.
    """
    if backend_name() == INFERENCE:
        return (os.getenv("TAI_INFERENCE_URL") or "http://127.0.0.1:8001").rstrip("/")
    return (os.getenv("TAI_SIGMA_URL") or "http://127.0.0.1:5001").rstrip("/")


def _sigma_model(model: str) -> str:
    return SIGMA_MODEL_ALIASES.get(model, model)


def _sigma_headers() -> dict[str, str]:
    """Auth for the generation call.

    The dedicated inference service is protected by a shared secret; Sigma's own
    /generate reads the caller's JWT and tolerates none.
    """
    if backend_name() == INFERENCE:
        token = (os.getenv("TAI_INFERENCE_TOKEN") or "").strip()
        if token:
            return {"Authorization": "Bearer " + token}
    return {}


def _sigma_payload(model: str, messages: list[dict[str, Any]], temperature: float,
                   max_output_tokens: int, stream: bool,
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The generation request.

    ``extra`` carries backend-specific switches the caller passed through, such
    as ``enable_thinking`` for TFMF. Dropping them here is silent: the request
    still succeeds, it just ignores what the caller asked for.
    """
    payload = {
        # `text` is the flattened form the torch backends expect. `messages` is
        # the structured form: a chat model handed "user: hi" as a single user
        # turn sees the role prefix as part of the sentence, which is not what
        # the caller wrote and shows up in its reasoning.
        "text": prompt_text(messages),
        "messages": messages,
        "model": _sigma_model(model),
        "max_len": max_output_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _generate_sigma(
    model: str, messages: list[dict[str, Any]], *, temperature: float, max_output_tokens: int,
    extra: dict[str, Any] | None = None,
) -> GenerationResult:
    _require_httpx()
    import httpx

    url = _sigma_url() + "/generate"
    try:
        with httpx.Client(timeout=_timeout()) as client:
            response = client.post(
                url,
                json=_sigma_payload(model, messages, temperature, max_output_tokens, False, extra),
                headers=_sigma_headers(),
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 - every failure becomes a 502 upstream
        raise BackendUnavailable(f"sigma backend at {url} failed: {exc}") from exc

    if isinstance(data, dict) and data.get("error"):
        raise BackendUnavailable(f"sigma: {data.get('error')}")

    text = str((data or {}).get("generated_text") or "")
    return GenerationResult(
        text=text,
        input_tokens=estimate_tokens(prompt_text(messages)),
        output_tokens=estimate_tokens(text),
        estimated=True,
    )


def _iter_sigma_sse(response: Any) -> Iterator[str]:
    """Turn Sigma's SSE frames into plain text deltas.

    Frames are ``data: {"chunk": "..."}`` and a terminating ``data: {"done": true}``.
    """
    import json as _json

    for line in response.iter_lines():
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body:
            continue
        try:
            frame = _json.loads(body)
        except ValueError:
            continue
        if isinstance(frame, dict):
            if frame.get("done"):
                return
            chunk = frame.get("chunk")
            if chunk:
                yield str(chunk)
        elif isinstance(frame, str):
            yield frame


def _stream_sigma(
    model: str, messages: list[dict[str, Any]], *, temperature: float, max_output_tokens: int,
    extra: dict[str, Any] | None = None,
) -> tuple[Iterator[str], dict[str, Any]]:
    _require_httpx()
    import httpx

    url = _sigma_url() + "/generate"
    client = httpx.Client(timeout=_timeout())
    try:
        # Opened eagerly so a dead backend raises here, before any SSE byte has
        # been sent, and the caller can still answer 502.
        response = client.send(
            client.build_request(
                "POST", url,
                json=_sigma_payload(model, messages, temperature, max_output_tokens, True, extra),
                headers=_sigma_headers(),
            ),
            stream=True,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        client.close()
        raise BackendUnavailable(f"sigma backend at {url} failed: {exc}") from exc

    def inner() -> Iterator[str]:
        try:
            yield from _iter_sigma_sse(response)
        finally:
            try:
                response.close()
            finally:
                client.close()

    return inner(), {}


def generate(
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_output_tokens: int = 512,
    extra: dict[str, Any] | None = None,
) -> GenerationResult:
    """Run one completion. Raises :class:`BackendUnavailable` on any failure."""
    name = backend_name()
    if name == STUB:
        return _stub_generate(
            model, messages, temperature=temperature, max_output_tokens=max_output_tokens
        )
    if name in (SIGMA, INFERENCE):
        return _generate_sigma(
            model, messages, temperature=temperature, max_output_tokens=max_output_tokens,
            extra=extra,
        )
    return _generate_openai(
        model, messages, temperature=temperature, max_output_tokens=max_output_tokens
    )


def stream(
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_output_tokens: int = 512,
    extra: dict[str, Any] | None = None,
) -> StreamHandle:
    """Start one streaming completion.

    The upstream connection is opened here, so a backend that is down raises
    :class:`BackendUnavailable` *before* any SSE bytes are sent and the caller
    can still answer 502. The returned :class:`StreamHandle` is an
    ``Iterator[str]`` whose ``.result`` holds the token accounting.
    """
    if backend_name() == STUB:
        inner: Iterator[str] = _stub_stream(
            model, messages, temperature=temperature, max_output_tokens=max_output_tokens
        )
        return StreamHandle(inner, {}, messages)
    if backend_name() in (SIGMA, INFERENCE):
        sigma_inner, sigma_state = _stream_sigma(
            model, messages, temperature=temperature, max_output_tokens=max_output_tokens,
            extra=extra,
        )
        return StreamHandle(sigma_inner, sigma_state, messages)

    inner, state = _stream_openai(
        model, messages, temperature=temperature, max_output_tokens=max_output_tokens
    )
    return StreamHandle(inner, state, messages)
