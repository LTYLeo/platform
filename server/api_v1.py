"""The public ``/api/v1`` API (see ``sdk/API_CONTRACT.md``).

Conventions that apply to every route in this module:

* **Auth** is always ``Authorization: Bearer sk-tai-...`` (:func:`server.deps.
  require_api_key`). There is no cookie path here, so there is no CSRF surface
  and no origin check.
* **Scoping**: every lookup is ``WHERE id = ? AND user_id = ?``. Someone else's
  object is indistinguishable from a missing one and returns ``not_found``.
* **Errors** are raised with :func:`server.deps.fail`, which produces the
  documented ``{code, message, param?}`` body via the handler in ``main.py``.
* **Bodies** are parsed as plain ``dict`` and validated here, field by field.
  That is deliberate: it is the only way to emit ``invalid_request`` with an
  accurate ``param`` for every field instead of Pydantic's generic message.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Iterator

from fastapi import APIRouter, Body, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from server import db, model_backend, pricing
from server.deps import (
    ASSISTANT_PREFIX,
    CHAT_PREFIX,
    MESSAGE_PREFIX,
    THREAD_PREFIX,
    check_model,
    fail,
    new_id,
    require_api_key,
)

router = APIRouter(prefix="/api/v1", tags=["v1"])

DEFAULT_LIMIT = 20
MIN_LIMIT = 1
MAX_LIMIT = 100

DEFAULT_NAME = "Assistant"
DEFAULT_TITLE = "New thread"
NAME_MAX = 80
TITLE_MAX = 200
INSTRUCTIONS_MAX = 8000
CONTENT_MAX = 32000
METADATA_MAX_KEYS = 16

DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_OUTPUT_TOKENS = 512
MAX_OUTPUT_TOKENS_LIMIT = 8192

MAX_PROMPT_MESSAGES = 100
MAX_CHAT_MESSAGES = 100

#: The contract's model objects advertise a context window. The pricing catalogue
#: does not carry one, so this documented default is used, with a per-model
#: override table for when a real backend needs a different value.
DEFAULT_CONTEXT_WINDOW = 8192
CONTEXT_WINDOWS: dict[str, int] = {}

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Tell nginx (and friends) not to buffer the stream.
    "X-Accel-Buffering": "no",
}


# --------------------------------------------------------------------------- #
# Body validation
# --------------------------------------------------------------------------- #

def _object_body(payload: dict | None) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict):  # pragma: no cover - FastAPI already checks
        raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_request",
                   "Request body must be a JSON object")
    return payload


def _invalid(param: str, message: str) -> Exception:
    return fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_request", message, param=param)


def _string(body: dict, key: str, *, default: Any = None, min_len: int = 0,
            max_len: int | None = None, strip: bool = True, required: bool = False) -> Any:
    if key not in body:
        if required:
            raise _invalid(key, f"`{key}` is required")
        return default
    value = body[key]
    if value is None:
        if required:
            raise _invalid(key, f"`{key}` must be a string")
        return default
    if not isinstance(value, str):
        raise _invalid(key, f"`{key}` must be a string")
    if strip:
        value = value.strip()
    if len(value) < min_len:
        raise _invalid(key, f"`{key}` must be at least {min_len} character(s)")
    if max_len is not None and len(value) > max_len:
        raise _invalid(key, f"`{key}` must be at most {max_len} characters")
    return value


def _boolean(body: dict, key: str, default: bool) -> bool:
    if key not in body or body[key] is None:
        return default
    value = body[key]
    if not isinstance(value, bool):
        raise _invalid(key, f"`{key}` must be a boolean")
    return value


def _number(body: dict, key: str, default: float, low: float, high: float) -> float:
    if key not in body or body[key] is None:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(key, f"`{key}` must be a number")
    number = float(value)
    if not math.isfinite(number) or not (low <= number <= high):
        raise _invalid(key, f"`{key}` must be between {low} and {high}")
    return number


def _integer(body: dict, key: str, default: int, low: int, high: int) -> int:
    if key not in body or body[key] is None:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(key, f"`{key}` must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise _invalid(key, f"`{key}` must be an integer")
    number = int(value)
    if not (low <= number <= high):
        raise _invalid(key, f"`{key}` must be between {low} and {high}")
    return number


def _metadata(body: dict, key: str = "metadata") -> dict[str, str]:
    if key not in body or body[key] is None:
        return {}
    value = body[key]
    if not isinstance(value, dict):
        raise _invalid(key, f"`{key}` must be an object")
    if len(value) > METADATA_MAX_KEYS:
        raise _invalid(key, f"`{key}` must have at most {METADATA_MAX_KEYS} keys")
    for name, entry in value.items():
        if not isinstance(entry, str):
            raise _invalid(f"{key}.{name}", f"`{key}` values must be strings")
    return {str(name): entry for name, entry in value.items()}


def _dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:  # pragma: no cover - only if a row was hand-edited
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #

def _assistant_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "object": "assistant",
        "name": row["name"],
        "model": row["model"],
        "instructions": row["instructions"],
        "metadata": _loads(row["metadata_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _thread_out(row: sqlite3.Row, message_count: int) -> dict:
    return {
        "id": row["id"],
        "object": "thread",
        "title": row["title"],
        "assistant_id": row["assistant_id"],
        # `threads.model` is a real column (contract §8) and is accepted by both
        # create and patch, so it is reported back rather than hidden.
        "model": row["model"],
        "metadata": _loads(row["metadata_json"]),
        "message_count": int(message_count),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _message_out(row: sqlite3.Row) -> dict:
    usage = None
    if row["usage_json"]:
        usage = _loads(row["usage_json"])
    return {
        "id": row["id"],
        "object": "message",
        "thread_id": row["thread_id"],
        "role": row["role"],
        "content": row["content"],
        "model": row["model"],
        "usage": usage,
        "created_at": row["created_at"],
    }


def _usage_out(input_tokens: int, output_tokens: int, cost_cny: float, estimated: bool) -> dict:
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_cny": round(float(cost_cny), 6),
        "estimated": bool(estimated),
    }


def _list_envelope(objects: list[dict], has_more: bool) -> dict:
    return {
        "object": "list",
        "data": objects,
        "has_more": has_more,
        "first_id": objects[0]["id"] if objects else None,
        "last_id": objects[-1]["id"] if objects else None,
    }


def _backend_label() -> str:
    """The generation backend's name, for the ``backend`` field on responses."""
    try:
        return model_backend.backend_name()
    except model_backend.BackendUnavailable:  # pragma: no cover - generate() raises first
        return "unknown"


# --------------------------------------------------------------------------- #
# Lookups scoped to the authenticated account
# --------------------------------------------------------------------------- #

def _user_id(key: sqlite3.Row) -> int:
    return int(key["user_id"])


def _get_assistant_or_404(conn: sqlite3.Connection, assistant_id: str, user_id: int) -> sqlite3.Row:
    row = db.get_assistant(conn, assistant_id, user_id)
    if row is None:
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such assistant")
    return row


def _get_thread_or_404(conn: sqlite3.Connection, thread_id: str, user_id: int) -> sqlite3.Row:
    row = db.get_thread(conn, thread_id, user_id)
    if row is None:
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such thread")
    return row


def _anchor(conn: sqlite3.Connection, kind: str, after: str | None, user_id: int,
            thread_id: str | None = None) -> sqlite3.Row | None:
    """Resolve the ``after`` cursor. An unknown or foreign id is a 422."""
    if not after:
        return None
    if kind == "assistant":
        row = db.get_assistant(conn, after, user_id)
    elif kind == "thread":
        row = db.get_thread(conn, after, user_id)
    else:
        row = db.get_message(conn, after, user_id)
        if row is not None and thread_id is not None and row["thread_id"] != thread_id:
            row = None
    if row is None:
        raise _invalid(
            "after",
            f"`after` must be the id of one of your {kind}s on this page",
        )
    return row


def _resolve_model(model_id: Any, param: str = "model") -> str:
    if not isinstance(model_id, str) or not model_id.strip():
        raise _invalid(param, f"`{param}` must be a non-empty model id")
    return check_model(model_id.strip())


def _model_for_thread(conn: sqlite3.Connection, thread: sqlite3.Row,
                      user_id: int) -> tuple[str, str]:
    """``(model, instructions)`` for a thread, from its assistant or its own model."""
    if thread["assistant_id"]:
        assistant = db.get_assistant(conn, thread["assistant_id"], user_id)
        if assistant is not None:
            return check_model(assistant["model"]), assistant["instructions"]
    if thread["model"]:
        return check_model(thread["model"]), ""
    raise _invalid(
        "model",
        "This thread has no model. Set `model` or `assistant_id` on the thread "
        "before posting a message.",
    )


# --------------------------------------------------------------------------- #
# Models and usage (contract §7)
# --------------------------------------------------------------------------- #

@router.get("/models")
def list_models(key: sqlite3.Row = Depends(require_api_key)) -> dict:
    """The live model catalogue. Cheap smoke test for a freshly minted key."""
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "name": entry["name"],
                "live": entry["live"],
                "context_window": CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW),
                "input_cny_per_1m": entry["input"],
                "output_cny_per_1m": entry["output"],
            }
            for model, entry in pricing.MODELS.items()
            if entry["live"]
        ],
    }


@router.get("/usage")
def get_usage(
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    """The same figures the dashboard shows, for the key's account."""
    summary = db.usage_summary(conn, _user_id(key))
    return {"object": "usage", **summary}


# --------------------------------------------------------------------------- #
# Assistants (contract §2)
# --------------------------------------------------------------------------- #

@router.post("/assistants", status_code=status.HTTP_201_CREATED)
def create_assistant(
    payload: dict | None = Body(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    body = _object_body(payload)
    user_id = _user_id(key)

    model = _resolve_model(body.get("model"))
    name = _string(body, "name", default=DEFAULT_NAME, min_len=1, max_len=NAME_MAX)
    instructions = _string(body, "instructions", default="", max_len=INSTRUCTIONS_MAX)
    metadata = _metadata(body)

    assistant_id = new_id(ASSISTANT_PREFIX)
    now = db.iso(db.utcnow())
    db.create_assistant(conn, assistant_id, user_id, name, model, instructions,
                        _dumps(metadata), now)
    return _assistant_out(_get_assistant_or_404(conn, assistant_id, user_id))


@router.get("/assistants")
def list_assistants(
    limit: int = Query(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    after: str | None = Query(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    user_id = _user_id(key)
    anchor = _anchor(conn, "assistant", after, user_id)
    rows = db.list_assistants(conn, user_id, limit + 1, anchor)
    has_more = len(rows) > limit
    return _list_envelope([_assistant_out(r) for r in rows[:limit]], has_more)


@router.get("/assistants/{assistant_id}")
def retrieve_assistant(
    assistant_id: str,
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    return _assistant_out(_get_assistant_or_404(conn, assistant_id, _user_id(key)))


@router.patch("/assistants/{assistant_id}")
def update_assistant(
    assistant_id: str,
    payload: dict | None = Body(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    body = _object_body(payload)
    user_id = _user_id(key)
    _get_assistant_or_404(conn, assistant_id, user_id)

    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = _string(body, "name", required=True, min_len=1, max_len=NAME_MAX)
    if "model" in body:
        fields["model"] = _resolve_model(body.get("model"))
    if "instructions" in body:
        fields["instructions"] = _string(body, "instructions", required=True, min_len=0,
                                         max_len=INSTRUCTIONS_MAX)
    if "metadata" in body:
        fields["metadata_json"] = _dumps(_metadata(body))

    row = db.update_assistant(conn, assistant_id, user_id, fields, db.iso(db.utcnow()))
    if row is None:  # pragma: no cover - deleted between the two statements
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such assistant")
    return _assistant_out(row)


@router.delete("/assistants/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assistant(
    assistant_id: str,
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> Response:
    if not db.delete_assistant(conn, assistant_id, _user_id(key)):
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such assistant")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Threads (contract §3)
# --------------------------------------------------------------------------- #

@router.post("/threads", status_code=status.HTTP_201_CREATED)
def create_thread(
    payload: dict | None = Body(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    body = _object_body(payload)
    user_id = _user_id(key)

    assistant_id: str | None = None
    assistant_row: sqlite3.Row | None = None
    if "assistant_id" in body and body["assistant_id"] is not None:
        assistant_id = _string(body, "assistant_id", min_len=1, max_len=128)
        assistant_row = _get_assistant_or_404(conn, assistant_id, user_id)

    model: str | None = None
    if "model" in body and body["model"] is not None:
        model = _resolve_model(body.get("model"))
    elif assistant_row is not None:
        # Inherit the assistant's model. Deleting an assistant only clears
        # threads.assistant_id, so without this the thread would be left with
        # neither an assistant nor a model and could never generate again.
        model = assistant_row["model"]

    title = _string(body, "title", default=DEFAULT_TITLE, min_len=1, max_len=TITLE_MAX)
    metadata = _metadata(body)

    thread_id = new_id(THREAD_PREFIX)
    now = db.iso(db.utcnow())
    db.create_thread(conn, thread_id, user_id, title, assistant_id, model,
                     _dumps(metadata), now)
    return _thread_out(_get_thread_or_404(conn, thread_id, user_id), 0)


@router.get("/threads")
def list_threads(
    limit: int = Query(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    after: str | None = Query(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    user_id = _user_id(key)
    anchor = _anchor(conn, "thread", after, user_id)
    rows = db.list_threads(conn, user_id, limit + 1, anchor)
    has_more = len(rows) > limit
    page = rows[:limit]
    counts = db.message_counts(conn, [r["id"] for r in page])
    return _list_envelope([_thread_out(r, counts.get(r["id"], 0)) for r in page], has_more)


@router.get("/threads/{thread_id}")
def retrieve_thread(
    thread_id: str,
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    thread = _get_thread_or_404(conn, thread_id, _user_id(key))
    counts = db.message_counts(conn, [thread["id"]])
    return _thread_out(thread, counts.get(thread["id"], 0))


@router.patch("/threads/{thread_id}")
def update_thread(
    thread_id: str,
    payload: dict | None = Body(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    body = _object_body(payload)
    user_id = _user_id(key)
    current = _get_thread_or_404(conn, thread_id, user_id)

    fields: dict[str, Any] = {}
    if "title" in body:
        fields["title"] = _string(body, "title", required=True, min_len=1, max_len=TITLE_MAX)
    if "assistant_id" in body:
        # `null` detaches the assistant; the thread keeps its own model.
        if body["assistant_id"] is None:
            fields["assistant_id"] = None
        else:
            linked = _string(body, "assistant_id", required=True, min_len=1, max_len=128)
            linked_row = _get_assistant_or_404(conn, linked, user_id)
            fields["assistant_id"] = linked
            # Same reasoning as in create_thread, but only fill a model the
            # thread does not already have so an explicit choice is never lost.
            if "model" not in body and not current["model"]:
                fields["model"] = linked_row["model"]
    if "model" in body:
        fields["model"] = None if body["model"] is None else _resolve_model(body.get("model"))
    if "metadata" in body:
        fields["metadata_json"] = _dumps(_metadata(body))

    row = db.update_thread(conn, thread_id, user_id, fields, db.iso(db.utcnow()))
    if row is None:  # pragma: no cover
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such thread")
    counts = db.message_counts(conn, [row["id"]])
    return _thread_out(row, counts.get(row["id"], 0))


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(
    thread_id: str,
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> Response:
    if not db.delete_thread(conn, thread_id, _user_id(key)):
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such thread")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Generation helpers
# --------------------------------------------------------------------------- #

def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _generate_or_502(model: str, messages: list[dict], temperature: float,
                     max_output_tokens: int) -> model_backend.GenerationResult:
    try:
        return model_backend.generate(
            model, messages, temperature=temperature, max_output_tokens=max_output_tokens
        )
    except model_backend.BackendUnavailable as exc:
        raise fail(status.HTTP_502_BAD_GATEWAY, "backend_unavailable", str(exc)) from exc


def _stream_or_502(model: str, messages: list[dict], temperature: float,
                   max_output_tokens: int) -> model_backend.StreamHandle:
    try:
        return model_backend.stream(
            model, messages, temperature=temperature, max_output_tokens=max_output_tokens
        )
    except model_backend.BackendUnavailable as exc:
        raise fail(status.HTTP_502_BAD_GATEWAY, "backend_unavailable", str(exc)) from exc


def _prompt_messages(conn: sqlite3.Connection, thread: sqlite3.Row, user_id: int,
                     instructions: str) -> list[dict]:
    """System instructions (if any) followed by the conversation, oldest first."""
    rows = db.all_messages(conn, thread["id"], user_id, limit=MAX_PROMPT_MESSAGES)
    messages: list[dict] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    messages.extend({"role": r["role"], "content": r["content"]} for r in rows)
    return messages


def _streaming_response(handle: model_backend.StreamHandle, *, stream_id: str, model: str,
                        thread_id: str | None, prompt: list[dict], user_id: int,
                        api_key_id: int | None, backend: str) -> StreamingResponse:
    generator = _sse_generator(
        handle,
        stream_id=stream_id,
        model=model,
        thread_id=thread_id,
        prompt=prompt,
        user_id=user_id,
        api_key_id=api_key_id,
        backend=backend,
    )
    return StreamingResponse(
        generator,
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
        # Runs even when the client vanished mid-stream, so a disconnect still
        # lands the assistant message and exactly one usage row.
        background=BackgroundTask(_close_quietly, generator, handle),
    )


def _close_quietly(generator: Iterator[str], handle: model_backend.StreamHandle) -> None:
    """Release the SSE iterator and the upstream connection, whatever happened."""
    try:
        generator.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - the generator also closes itself on GC
        pass
    handle.close()


def _sse_generator(handle: model_backend.StreamHandle, *, stream_id: str, model: str,
                   thread_id: str | None, prompt: list[dict], user_id: int,
                   api_key_id: int | None, backend: str) -> Iterator[str]:
    """The SSE body. Guarantees exactly one ``db.record_usage`` call.

    A dedicated connection is opened here because this generator outlives the
    request-scoped dependency, and the ``finally`` block runs on completion, on
    a backend error, *and* on a client disconnect (Starlette closes the
    iterator), which is what keeps the dashboard honest.
    """
    conn = db.connect()
    parts: list[str] = []
    state = {"recorded": False}

    def finalize(text: str, result: model_backend.GenerationResult | None) -> dict | None:
        if state["recorded"]:
            return None
        if result is None:
            if not text:
                return None  # nothing was generated, so nothing is billable
            result = model_backend.GenerationResult(
                text=text,
                input_tokens=model_backend.estimate_tokens(model_backend.prompt_text(prompt)),
                output_tokens=model_backend.estimate_tokens(text),
                estimated=True,
            )
        state["recorded"] = True
        cost = db.record_usage(conn, user_id, api_key_id, model,
                               result.input_tokens, result.output_tokens)
        usage = _usage_out(result.input_tokens, result.output_tokens, cost, result.estimated)
        if thread_id is not None:
            now = db.iso(db.utcnow())
            db.create_message(conn, stream_id, thread_id, user_id, "assistant", text,
                              model, _dumps(usage), now)
            db.touch_thread(conn, thread_id, now)
        return usage

    try:
        yield _sse("message.start",
                   {"id": stream_id, "model": model, "thread_id": thread_id,
                    "backend": backend})
        try:
            for chunk in handle:
                parts.append(chunk)
                yield _sse("message.delta", {"delta": chunk})
        except model_backend.BackendUnavailable as exc:
            text = "".join(parts)
            if text:
                finalize(text, None)
            yield _sse("error", {"code": "backend_unavailable", "message": str(exc)})
            return

        result = handle.result
        usage = finalize(result.text, result)
        yield _sse("message.done",
                   {"id": stream_id, "content": result.text, "usage": usage})
    finally:
        if not state["recorded"]:
            finalize("".join(parts), None)
        handle.close()
        conn.close()


# --------------------------------------------------------------------------- #
# Messages (contract §4, §6)
# --------------------------------------------------------------------------- #

def _ensure_funds(conn: sqlite3.Connection, user_id: int) -> None:
    """Refuse to generate when the free allowance is gone and the balance is empty.

    Checked before any model work so a broke account costs us nothing, and
    before the stream opens so the client gets a clean 402 instead of a stream
    that dies mid-flight.
    """
    allowed, reason, state = db.can_generate(conn, user_id)
    if allowed:
        return
    raise fail(
        status.HTTP_402_PAYMENT_REQUIRED,
        "insufficient_balance",
        "Your free tokens are used up and your balance is empty. Top up or redeem "
        "a code to keep generating.",
        extra={
            "balance_cny": state["balance_cny"],
            "free_tokens_remaining": state["free_remaining"],
        },
    )


@router.post("/threads/{thread_id}/messages")
def create_message(
    thread_id: str,
    payload: dict | None = Body(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
):
    body = _object_body(payload)
    user_id = _user_id(key)
    _ensure_funds(conn, user_id)
    thread = _get_thread_or_404(conn, thread_id, user_id)

    content = _string(body, "content", required=True, min_len=1, max_len=CONTENT_MAX,
                      strip=False)
    role = _string(body, "role", default="user", min_len=1, max_len=16)
    if role != "user":
        raise _invalid("role", "`role` must be 'user'; assistant messages are generated")
    wants_stream = _boolean(body, "stream", False)
    temperature = _number(body, "temperature", DEFAULT_TEMPERATURE, 0.0, 2.0)
    max_output_tokens = _integer(body, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS,
                                 1, MAX_OUTPUT_TOKENS_LIMIT)

    model, instructions = _model_for_thread(conn, thread, user_id)

    # The user's turn is stored first, so it is part of the prompt and it stays
    # even if the backend then fails.
    now = db.iso(db.utcnow())
    user_message = db.create_message(
        conn, new_id(MESSAGE_PREFIX), thread_id, user_id, "user", content, None, None, now
    )
    db.touch_thread(conn, thread_id, now)
    prompt = _prompt_messages(conn, thread, user_id, instructions)

    backend = _backend_label()

    if wants_stream:
        handle = _stream_or_502(model, prompt, temperature, max_output_tokens)
        return _streaming_response(
            handle,
            stream_id=new_id(MESSAGE_PREFIX),
            model=model,
            thread_id=thread_id,
            prompt=prompt,
            user_id=user_id,
            api_key_id=int(key["id"]),
            backend=backend,
        )

    result = _generate_or_502(model, prompt, temperature, max_output_tokens)
    cost = db.record_usage(conn, user_id, int(key["id"]), model,
                           result.input_tokens, result.output_tokens)
    usage = _usage_out(result.input_tokens, result.output_tokens, cost, result.estimated)

    assistant_message = db.create_message(
        conn, new_id(MESSAGE_PREFIX), thread_id, user_id, "assistant", result.text,
        model, _dumps(usage), db.iso(db.utcnow())
    )
    db.touch_thread(conn, thread_id, db.iso(db.utcnow()))

    return {
        "object": "message.exchange",
        "thread_id": thread_id,
        "user_message": _message_out(user_message),
        "assistant_message": _message_out(assistant_message),
        "usage": {**usage, "model": model},
        "backend": backend,
    }


@router.get("/threads/{thread_id}/messages")
def list_messages(
    thread_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    after: str | None = Query(default=None),
    order: str | None = Query(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict:
    user_id = _user_id(key)
    _get_thread_or_404(conn, thread_id, user_id)

    direction = (order or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        raise _invalid("order", "`order` must be 'asc' or 'desc'")
    ascending = direction == "asc"

    anchor = _anchor(conn, "message", after, user_id, thread_id=thread_id)
    rows = db.list_messages(conn, thread_id, user_id, limit + 1, anchor,
                            ascending=ascending)
    has_more = len(rows) > limit
    return _list_envelope([_message_out(r) for r in rows[:limit]], has_more)


# --------------------------------------------------------------------------- #
# Stateless chat (contract §5, §6)
# --------------------------------------------------------------------------- #

def _chat_messages(body: dict) -> list[dict]:
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise _invalid("messages", "`messages` must be a non-empty array")
    if len(raw) > MAX_CHAT_MESSAGES:
        raise _invalid("messages", f"`messages` must have at most {MAX_CHAT_MESSAGES} items")

    messages: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _invalid(f"messages[{index}]", "each message must be an object")
        role = item.get("role")
        if role not in {"user", "assistant", "system"}:
            raise _invalid(f"messages[{index}].role",
                           "role must be one of: user, assistant, system")
        content = item.get("content")
        if not isinstance(content, str) or not content:
            raise _invalid(f"messages[{index}].content", "content must be a non-empty string")
        if len(content) > CONTENT_MAX:
            raise _invalid(f"messages[{index}].content",
                           f"content must be at most {CONTENT_MAX} characters")
        messages.append({"role": role, "content": content})
    return messages


@router.post("/chat")
def chat(
    payload: dict | None = Body(default=None),
    key: sqlite3.Row = Depends(require_api_key),
    conn: sqlite3.Connection = Depends(db.get_db),
):
    body = _object_body(payload)
    user_id = _user_id(key)
    _ensure_funds(conn, user_id)

    instructions = ""
    model: str | None = None
    if body.get("assistant_id") is not None:
        assistant_id = _string(body, "assistant_id", min_len=1, max_len=128)
        assistant = _get_assistant_or_404(conn, assistant_id, user_id)
        instructions = assistant["instructions"]
        model = assistant["model"]

    # An explicit `model` wins over the assistant's, which makes
    # "assistant for the prompt, model for this call" possible.
    if body.get("model") is not None:
        model = _resolve_model(body.get("model"))
    elif model is not None:
        model = check_model(model)

    if model is None:
        raise _invalid("model", "`model` is required unless `assistant_id` is given")

    messages = _chat_messages(body)
    if instructions:
        messages = [{"role": "system", "content": instructions}, *messages]

    wants_stream = _boolean(body, "stream", False)
    temperature = _number(body, "temperature", DEFAULT_TEMPERATURE, 0.0, 2.0)
    max_output_tokens = _integer(body, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS,
                                 1, MAX_OUTPUT_TOKENS_LIMIT)
    backend = _backend_label()

    if wants_stream:
        handle = _stream_or_502(model, messages, temperature, max_output_tokens)
        return _streaming_response(
            handle,
            stream_id=new_id(CHAT_PREFIX),
            model=model,
            thread_id=None,
            prompt=messages,
            user_id=user_id,
            api_key_id=int(key["id"]),
            backend=backend,
        )

    result = _generate_or_502(model, messages, temperature, max_output_tokens)
    cost = db.record_usage(conn, user_id, int(key["id"]), model,
                           result.input_tokens, result.output_tokens)
    usage = _usage_out(result.input_tokens, result.output_tokens, cost, result.estimated)

    return {
        "id": new_id(CHAT_PREFIX),
        "object": "chat.completion",
        "model": model,
        "output": {"role": "assistant", "content": result.text},
        "usage": usage,
        "created_at": db.iso(db.utcnow()),
        "backend": backend,
    }
