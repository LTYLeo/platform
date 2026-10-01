"""Typed response objects for the TAI Assistant API.

These are deliberately small: hand-written dataclasses (no pydantic) that give
you attribute access to the fields documented in ``API_CONTRACT.md`` while
keeping the untouched payload on :attr:`Model.raw`.

Every object also has a ``.raw`` dict, so you can always reach a field the SDK
does not model explicitly::

    assistant = client.assistants.create(model="tfmf")
    assistant.id                  # "asst_..."
    assistant.raw["created_at"]   # "2026-09-30T15:04:05+00:00"
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional

__all__ = [
    "Model",
    "Usage",
    "MessageExchange",
    "Message",
    "MessagesPage",
    "Assistant",
    "AssistantsPage",
    "Thread",
    "ThreadsPage",
    "ModelInfo",
    "ModelsPage",
    "UsageSummary",
    "ChatMessage",
    "ChatCompletion",
    "StreamEvent",
    "MessageStart",
    "MessageDelta",
    "MessageDone",
    "ListPage",
]


def _dump(value: Any) -> Any:
    """Convert dataclasses (recursively) back into plain JSON-able data."""
    if isinstance(value, Model):
        return value.raw
    if is_dataclass(value):
        return {f.name: _dump(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    return value


def _body(value: Any) -> Dict[str, Any]:
    """Accept either a decoded body or an object that wraps one."""
    if isinstance(value, Model):
        return value.raw
    if isinstance(value, dict):
        return value
    raise TypeError("expected a dict payload, got %r" % type(value).__name__)


class Model:
    """Base class for every response object."""

    #: The untouched decoded JSON payload.
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Return the raw payload as a plain dict (a copy-free reference)."""
        return self.raw

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):  # type: ignore[arg-type]
            try:
                parts.append("%s=%r" % (f.name, getattr(self, f.name)))
            except AttributeError:  # pragma: no cover - defensive
                continue
        return "%s(%s)" % (type(self).__name__, ", ".join(parts))


# --------------------------------------------------------------------------- #
# Shared value objects
# --------------------------------------------------------------------------- #
@dataclass(repr=False)
class Usage(Model):
    """Token accounting. ``estimated`` is true unless the backend reported usage."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_cny: Optional[float] = None
    estimated: Optional[bool] = None
    model: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> Optional["Usage"]:
        if payload is None:
            return None
        if isinstance(payload, Usage):
            return payload
        if not isinstance(payload, dict):
            return None
        return cls(
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            cost_cny=payload.get("cost_cny"),
            estimated=payload.get("estimated"),
            model=payload.get("model"),
            raw=payload,
        )


@dataclass(repr=False)
class Message(Model):
    """A persisted message in a thread (contract §4.3)."""

    id: str = ""
    object: str = "message"
    thread_id: Optional[str] = None
    role: Optional[str] = None
    content: Optional[str] = None
    model: Optional[str] = None
    usage: Optional[Usage] = None
    created_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "Message":
        data = _body(payload)
        return cls(
            id=data.get("id") or "",
            object=data.get("object") or "message",
            thread_id=data.get("thread_id"),
            role=data.get("role"),
            content=data.get("content"),
            model=data.get("model"),
            usage=Usage.parse(data.get("usage")),
            created_at=data.get("created_at"),
            raw=data,
        )


@dataclass(repr=False)
class Assistant(Model):
    """A reusable model + instructions + name configuration (contract §2)."""

    id: str = ""
    object: str = "assistant"
    name: Optional[str] = None
    model: Optional[str] = None
    instructions: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "Assistant":
        data = _body(payload)
        return cls(
            id=data.get("id") or "",
            object=data.get("object") or "assistant",
            name=data.get("name"),
            model=data.get("model"),
            instructions=data.get("instructions"),
            metadata=data.get("metadata") or {},
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            raw=data,
        )


@dataclass(repr=False)
class Thread(Model):
    """A persistent conversation (contract §3)."""

    id: str = ""
    object: str = "thread"
    title: Optional[str] = None
    assistant_id: Optional[str] = None
    model: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    message_count: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "Thread":
        data = _body(payload)
        return cls(
            id=data.get("id") or "",
            object=data.get("object") or "thread",
            title=data.get("title"),
            assistant_id=data.get("assistant_id"),
            model=data.get("model"),
            metadata=data.get("metadata") or {},
            message_count=data.get("message_count"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            raw=data,
        )


@dataclass(repr=False)
class ModelInfo(Model):
    """A live model advertised by ``GET /api/v1/models`` (contract §7)."""

    id: str = ""
    object: str = "model"
    name: Optional[str] = None
    live: Optional[bool] = None
    context_window: Optional[int] = None
    input_cny_per_1m: Optional[float] = None
    output_cny_per_1m: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "ModelInfo":
        data = _body(payload)
        return cls(
            id=data.get("id") or "",
            object=data.get("object") or "model",
            name=data.get("name"),
            live=data.get("live"),
            context_window=data.get("context_window"),
            input_cny_per_1m=data.get("input_cny_per_1m"),
            output_cny_per_1m=data.get("output_cny_per_1m"),
            raw=data,
        )


@dataclass(repr=False)
class ChatMessage(Model):
    """One ``{role, content}`` item of a stateless chat request."""

    role: str = "user"
    content: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:  # type: ignore[override]
        return {"role": self.role, "content": self.content}

    @classmethod
    def coerce(cls, value: Any) -> "ChatMessage":
        """Accept a :class:`ChatMessage`, an ``{"role": ..., "content": ...}`` dict,
        or a ``(role, content)`` tuple, and normalise to a ChatMessage."""
        if isinstance(value, ChatMessage):
            return value
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return cls(role=str(value[0]), content=str(value[1]))
        if isinstance(value, dict):
            if "content" not in value:
                raise ValueError("chat message dict needs a 'content' key: %r" % (value,))
            return cls(
                role=value.get("role") or "user",
                content=value["content"],
                raw=value,
            )
        raise TypeError(
            "chat messages must be ChatMessage, {'role', 'content'} or (role, content); got %r"
            % type(value).__name__
        )


# --------------------------------------------------------------------------- #
# Envelopes
# --------------------------------------------------------------------------- #
@dataclass(repr=False)
class ListPage(Model):
    """The pagination envelope from contract §1.4."""

    object: str = "list"
    data: List[Any] = field(default_factory=list)
    has_more: bool = False
    first_id: Optional[str] = None
    last_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return len(self.data)

    def __iter__(self):
        return iter(self.data)

    def __getitem__(self, index: int) -> Any:
        return self.data[index]

    def __repr__(self) -> str:
        return "%s(count=%d, has_more=%r, first_id=%r, last_id=%r)" % (
            type(self).__name__,
            len(self.data),
            self.has_more,
            self.first_id,
            self.last_id,
        )


class AssistantsPage(ListPage):
    """``GET /api/v1/assistants`` envelope."""

    @classmethod
    def parse(cls, payload: Any) -> "AssistantsPage":
        data = _body(payload)
        return cls(
            object=data.get("object") or "list",
            data=[Assistant.parse(item) for item in (data.get("data") or [])],
            has_more=bool(data.get("has_more", False)),
            first_id=data.get("first_id"),
            last_id=data.get("last_id"),
            raw=data,
        )


class ThreadsPage(ListPage):
    """``GET /api/v1/threads`` envelope."""

    @classmethod
    def parse(cls, payload: Any) -> "ThreadsPage":
        data = _body(payload)
        return cls(
            object=data.get("object") or "list",
            data=[Thread.parse(item) for item in (data.get("data") or [])],
            has_more=bool(data.get("has_more", False)),
            first_id=data.get("first_id"),
            last_id=data.get("last_id"),
            raw=data,
        )


class MessagesPage(ListPage):
    """``GET /api/v1/threads/{thread_id}/messages`` envelope."""

    @classmethod
    def parse(cls, payload: Any) -> "MessagesPage":
        data = _body(payload)
        return cls(
            object=data.get("object") or "list",
            data=[Message.parse(item) for item in (data.get("data") or [])],
            has_more=bool(data.get("has_more", False)),
            first_id=data.get("first_id"),
            last_id=data.get("last_id"),
            raw=data,
        )


class ModelsPage(ListPage):
    """``GET /api/v1/models`` envelope (a plain list, no pagination fields)."""

    @classmethod
    def parse(cls, payload: Any) -> "ModelsPage":
        data = _body(payload)
        return cls(
            object=data.get("object") or "list",
            data=[ModelInfo.parse(item) for item in (data.get("data") or [])],
            has_more=bool(data.get("has_more", False)),
            first_id=data.get("first_id"),
            last_id=data.get("last_id"),
            raw=data,
        )


@dataclass(repr=False)
class UsageSummary(Model):
    """``GET /api/v1/usage`` — the dashboard ``usage_summary()`` payload + object."""

    object: str = "usage"
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "UsageSummary":
        data = _body(payload)
        inner = {key: value for key, value in data.items() if key != "object"}
        # Expose the summary fields directly on the instance as well as in ``.raw``.
        instance = cls(object=data.get("object") or "usage", raw=data)
        instance.__dict__.update(inner)
        return instance


@dataclass(repr=False)
class MessageExchange(Model):
    """The non-streaming response of ``POST /threads/{id}/messages`` (contract §4.2)."""

    object: str = "message.exchange"
    thread_id: Optional[str] = None
    user_message: Optional[Message] = None
    assistant_message: Optional[Message] = None
    usage: Optional[Usage] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def content(self) -> Optional[str]:
        """Convenience: the assistant reply text."""
        return self.assistant_message.content if self.assistant_message else None

    @classmethod
    def parse(cls, payload: Any) -> "MessageExchange":
        data = _body(payload)
        user = data.get("user_message")
        assistant = data.get("assistant_message")
        return cls(
            object=data.get("object") or "message.exchange",
            thread_id=data.get("thread_id"),
            user_message=Message.parse(user) if user else None,
            assistant_message=Message.parse(assistant) if assistant else None,
            usage=Usage.parse(data.get("usage")),
            raw=data,
        )


@dataclass(repr=False)
class ChatCompletion(Model):
    """The response of ``POST /api/v1/chat`` (contract §5)."""

    id: str = ""
    object: str = "chat.completion"
    model: Optional[str] = None
    output: Dict[str, Any] = field(default_factory=dict)
    usage: Optional[Usage] = None
    created_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def content(self) -> Optional[str]:
        """Convenience: the assistant text from ``output``."""
        if isinstance(self.output, dict):
            return self.output.get("content")
        return None

    @classmethod
    def parse(cls, payload: Any) -> "ChatCompletion":
        data = _body(payload)
        output = data.get("output")
        return cls(
            id=data.get("id") or "",
            object=data.get("object") or "chat.completion",
            model=data.get("model"),
            output=output if isinstance(output, dict) else {},
            usage=Usage.parse(data.get("usage")),
            created_at=data.get("created_at"),
            raw=data,
        )


# --------------------------------------------------------------------------- #
# Streaming events (contract §6)
# --------------------------------------------------------------------------- #
@dataclass(repr=False)
class StreamEvent(Model):
    """Base class for the typed SSE events yielded by ``chat.stream``."""

    event: str = ""
    data: Dict[str, Any] = field(default_factory=dict, repr=False)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(repr=False)
class MessageStart(StreamEvent):
    """``event: message.start`` — always the first frame of a stream."""

    event: str = "message.start"
    id: str = ""
    model: Optional[str] = None
    thread_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "MessageStart":
        data = _body(payload)
        return cls(
            event="message.start",
            id=data.get("id") or "",
            model=data.get("model"),
            thread_id=data.get("thread_id"),
            data=data,
            raw=data,
        )


@dataclass(repr=False)
class MessageDelta(StreamEvent):
    """``event: message.delta`` — one incremental text fragment."""

    event: str = "message.delta"
    delta: str = ""
    data: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "MessageDelta":
        data = _body(payload)
        return cls(
            event="message.delta",
            delta=data.get("delta") or "",
            data=data,
            raw=data,
        )


@dataclass(repr=False)
class MessageDone(StreamEvent):
    """``event: message.done`` — always the last frame of a successful stream."""

    event: str = "message.done"
    id: str = ""
    content: str = ""
    usage: Optional[Usage] = None
    data: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: Any) -> "MessageDone":
        data = _body(payload)
        return cls(
            event="message.done",
            id=data.get("id") or "",
            content=data.get("content") or "",
            usage=Usage.parse(data.get("usage")),
            data=data,
            raw=data,
        )


_STREAM_EVENTS = {
    "message.start": MessageStart,
    "message.delta": MessageDelta,
    "message.done": MessageDone,
}


def parse_stream_event(event: Optional[str], data: Dict[str, Any]) -> StreamEvent:
    """Turn a raw SSE frame into one of the typed events above.

    Unknown event names are surfaced as plain :class:`StreamEvent` objects
    rather than dropped, so a server that adds a frame type cannot silently
    change behaviour.
    """
    name = (event or "").strip()
    cls = _STREAM_EVENTS.get(name)
    if cls is None:
        return StreamEvent(event=name, data=data, raw=data)
    return cls.parse(data)  # type: ignore[return-value]
