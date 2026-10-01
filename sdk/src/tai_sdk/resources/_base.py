"""Shared plumbing for the resource namespaces."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncIterator, Dict, Iterator, Optional, TypeVar

__all__ = ["Resource", "wrap_stream", "open_events", "awaited"]

T = TypeVar("T")


def awaited(value: Any, parser: Any = None) -> Any:
    """Bridge one resource method across the sync and async clients.

    ``TAI.assistants.create()`` returns an ``Assistant`` right away;
    ``AsyncTAI.assistants.create()`` must be awaited and return the same object.
    Instead of duplicating every resource class, the parser is applied directly
    to a dict and lazily to a coroutine.
    """
    if inspect.isawaitable(value):
        if parser is None:
            return value

        async def _await_and_parse() -> Any:
            return parser(await value)

        return _await_and_parse()
    return value if parser is None else parser(value)


def wrap_stream(transport: Any, opened: Any) -> Any:
    """Turn an opened streaming response into a safe-to-abandon event iterator.

    * sync — a transparent iterator that is also a context manager, so ``with``,
      ``.close()``, exhaustion, an exception and garbage collection all close
      the HTTP response.
    * async — an async iterator with ``aclose()`` giving the same guarantees.
      There is deliberately no ``close()`` method, so a sync ``with`` fails
      loudly instead of silently leaking the connection.

    ``opened`` is what the transport returned: the open response for sync
    transports, an awaitable of it for async ones. The HTTP request has already
    been sent by the time this is called, so HTTP errors have already surfaced.
    """
    if getattr(transport, "is_async", False):
        return _aclosing(opened)
    return _closing(opened)


def open_events(transport: Any, method: str, path: str, **kwargs: Any) -> Any:
    """Open an SSE request through ``transport`` and return its typed events.

    The request is opened eagerly, so an HTTP error (401/404/422/...) raises
    from the ``stream()`` call itself rather than on first iteration. The
    returned iterator always closes the HTTP response, however the caller
    leaves the loop.
    """
    if getattr(transport, "is_async", False):
        return _aclosing(transport.open_stream(method, path, **kwargs), transport.events_from)
    response = transport.open_stream(method, path, **kwargs)
    return _closing(transport.events_from(response))


class _closing:
    """``contextlib.closing`` for an iterator: transparent *and* a context manager."""

    def __init__(self, stream: Iterator[Any]) -> None:
        self.__wrapped__ = stream
        self._stream = stream

    def __iter__(self) -> "_closing":
        return self

    def __next__(self) -> Any:
        return next(self._stream)

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> "_closing":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


async def _aclosing(opened: Any, events_from: Any) -> AsyncIterator[Any]:
    """Async twin of :class:`_closing`, driven by an awaitable response."""
    stream: Any = None
    try:
        response = await opened if inspect.isawaitable(opened) else opened
        stream = events_from(response)
        async for item in stream:
            yield item
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - cleanup only
                pass


class Resource:
    """Base class holding a reference to the HTTP transport."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    @property
    def _base_url(self) -> str:
        return getattr(self._transport, "base_url", "")

    @staticmethod
    def _clean(body: Dict[str, Any]) -> Dict[str, Any]:
        """Drop keys whose value is ``None`` so servers see only real fields."""
        return {key: value for key, value in body.items() if value is not None}

    def _validate_metadata(self, metadata: Optional[dict]) -> Optional[dict]:
        """Validate the contract's metadata rule: at most 16 flat string values."""
        if metadata is None:
            return None
        if not isinstance(metadata, dict):
            raise TypeError("metadata must be a dict of flat string values")
        if len(metadata) > 16:
            raise ValueError("metadata supports at most 16 keys, got %d" % len(metadata))
        for key, value in metadata.items():
            if not isinstance(key, str):
                raise TypeError("metadata keys must be strings")
            if isinstance(value, (dict, list, tuple, set)) or value is None:
                raise TypeError(
                    "metadata values must be flat strings (key %r has %r)" % (key, value)
                )
        return metadata
