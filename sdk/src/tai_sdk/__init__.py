"""Official Python SDK for the TAI Assistant API.

Quick start::

    from tai_sdk import TAI

    with TAI() as client:                       # api_key from TAI_API_KEY
        models = client.models.list()
        reply = client.chat.create(
            model=models[0].id,
            messages=[{"role": "user", "content": "Explain tail recursion"}],
        )
        print(reply.content)

Streaming::

    with TAI() as client:
        for event in client.chat.stream(
            model="tfmf",
            messages=[{"role": "user", "content": "Count to five"}],
        ):
            if event.event == "message.delta":
                print(event.delta, end="", flush=True)

Async::

    from tai_sdk import AsyncTAI

    async with AsyncTAI() as client:
        thread = await client.threads.create(assistant_id="asst_...")
        exchange = await client.threads.messages.create(thread.id, content="Hi")

The public surface is :class:`TAI`, :class:`AsyncTAI`, the response types in
:mod:`tai_sdk.types` and the exceptions in :mod:`tai_sdk.errors`.
"""

from __future__ import annotations

from . import errors, types
from ._client import TAI, AsyncTAI
from ._version import __version__
from .errors import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BackendUnavailableError,
    BadRequestError,
    ConflictError,
    InsufficientBalanceError,
    InternalServerError,
    InvalidRequestError,
    ModelNotAvailableError,
    ModelNotFoundError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    StreamAbortedError,
    StreamError,
    TAIError,
    TransportError,
    UnknownAPIError,
)
from .types import (
    Assistant,
    AssistantsPage,
    ChatCompletion,
    ChatMessage,
    ListPage,
    Message,
    MessageDelta,
    MessageDone,
    MessageExchange,
    MessageStart,
    MessagesPage,
    Model,
    ModelInfo,
    ModelsPage,
    StreamEvent,
    Thread,
    ThreadsPage,
    Usage,
    UsageSummary,
)

__all__ = [
    "__version__",
    # clients
    "TAI",
    "AsyncTAI",
    # submodules
    "errors",
    "types",
    # response types
    "Model",
    "ListPage",
    "Usage",
    "Message",
    "Assistant",
    "AssistantsPage",
    "Thread",
    "ThreadsPage",
    "MessageExchange",
    "MessagesPage",
    "ModelInfo",
    "ModelsPage",
    "UsageSummary",
    "ChatMessage",
    "ChatCompletion",
    "StreamEvent",
    "MessageStart",
    "MessageDelta",
    "MessageDone",
    # errors
    "TAIError",
    "TransportError",
    "APIConnectionError",
    "APITimeoutError",
    "APIError",
    "APIStatusError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "InsufficientBalanceError",
    "NotFoundError",
    "ModelNotFoundError",
    "ConflictError",
    "ModelNotAvailableError",
    "InvalidRequestError",
    "RateLimitError",
    "InternalServerError",
    "BackendUnavailableError",
    "StreamAbortedError",
    "StreamError",
    "UnknownAPIError",
]
