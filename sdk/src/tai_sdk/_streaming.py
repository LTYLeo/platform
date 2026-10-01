"""Incremental Server-Sent Events parsing for sync and async streams.

The event grammar is the one in contract §6: frames separated by a blank line,
an ``event:`` line and exactly one single-line ``data:`` JSON object.

Both readers are *truly* incremental — bytes are emitted as soon as a frame is
complete, never buffered until the response ends. Both also guarantee cleanup:
the sync iterator closes the HTTP response when the caller breaks out of the
loop or an exception propagates, and the async iterator cancels its reader task
and acloses the response the same way.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator, Optional

from .errors import APIError, TAIError, error_from_body
from .types import StreamEvent, parse_stream_event

__all__ = [
    "SSEFrame",
    "SSEDecoder",
    "iter_stream",
    "aiter_stream",
    "sync_events",
    "async_events",
]

#: Terminator some SSE producers append; we stop reading on it.
_DONE_SENTINEL = "[DONE]"

#: Returned by ``_decode_frame`` when the stream terminator was seen.
_END = object()


@dataclass
class SSEFrame:
    """One decoded ``event:`` / ``data:`` frame."""

    event: Optional[str]
    data: str
    raw: Optional[str] = None


class SSEDecoder:
    """Byte-level incremental SSE decoder.

    Feed it bytes with :meth:`feed`; it returns every frame that completed. Any
    trailing partial frame stays buffered until more bytes (or a final
    :meth:`feed` call) complete it. Splitting mid-frame — even in the middle of
    a UTF-8 sequence or a ``data:`` payload — is safe.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._event: Optional[str] = None
        self._data: list = []

    def feed(self, chunk: bytes) -> list:
        """Add bytes and return the list of newly completed frames."""
        if chunk:
            self._buffer.extend(chunk)
        return list(self._drain(final=False))

    def feed_text(self, text: str) -> list:
        return self.feed(text.encode("utf-8"))

    def close(self) -> list:
        """Flush any final frame that was not terminated by a blank line."""
        return list(self._drain(final=True))

    # -- internals ---------------------------------------------------------- #
    def _drain(self, final: bool) -> Iterator[SSEFrame]:
        while True:
            index = self._buffer.find(b"\n")
            if index == -1:
                if final and self._buffer:
                    # A last line with no trailing newline.
                    raw = bytes(self._buffer).decode("utf-8", "replace")
                    self._buffer.clear()
                    for frame in self._handle_line(raw):
                        yield frame
                    if self._event is not None or self._data:
                        frame = self._emit()
                        if frame is not None:
                            yield frame
                return
            line = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            for frame in self._handle_line(line.decode("utf-8", "replace")):
                yield frame

    def _handle_line(self, line: str) -> Iterator[SSEFrame]:
        if line == "":
            frame = self._emit()
            if frame is not None:
                yield frame
            return
        if line.startswith(":"):
            # Comment / keep-alive heartbeat.
            return
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value.strip()
        elif name == "data":
            self._data.append(value)

    def _emit(self) -> Optional[SSEFrame]:
        event = self._event
        payload = "\n".join(self._data)
        had_event = event is not None
        had_data = bool(self._data)
        self._event = None
        self._data = []
        if not had_event and not had_data:
            return None
        return SSEFrame(event=event, data=payload, raw=payload or None)


def _error_from_frame(data: str, event: Optional[str]) -> APIError:
    try:
        payload: Any = json.loads(data)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return error_from_body(payload, fallback_message=data.strip() or None)
    return error_from_body(
        {"message": data.strip() or "stream failed", "code": "stream_error"},
    )


def _decode_frame(frame: SSEFrame) -> Any:
    data = frame.data.strip()
    if not data:
        # A bare ``data: [DONE]`` (no event name) does not reach _decode_frame,
        # but be explicit about the other spellings.
        return None
    if data == _DONE_SENTINEL:
        return _END
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise TAIError(
            "Could not decode the SSE frame for event %r: %s" % (frame.event, exc)
        ) from exc
    if not isinstance(payload, dict):
        payload = {"data": payload}
    name = (frame.event or "").strip()
    if name == "error":
        raise _error_from_frame(data, frame.event)
    return parse_stream_event(name, payload)


def sync_events(response: Any) -> Iterator[StreamEvent]:
    """Typed events from an open streaming response, closing it in all cases.

    ``finally`` (not ``except``) matters: when the consumer abandons the loop the
    garbage collector closes this generator, which raises ``GeneratorExit`` at the
    ``yield``. Closing through :func:`_close` keeps that teardown off httpx while
    the interpreter is finalising.
    """
    try:
        for event in iter_stream(response):
            yield event
    finally:
        _close(response)


async def async_events(response: Any) -> AsyncIterator[StreamEvent]:
    """Typed events from an open streaming response, aclosing it in all cases."""
    try:
        async for event in aiter_stream(response):
            yield event
    finally:
        await _aclose(response)


def iter_stream(response: Any) -> Iterator[StreamEvent]:
    """Yield typed events from an open httpx streaming response.

    The response is always closed, including when the consumer stops early.
    """
    decoder = SSEDecoder()
    try:
        for chunk in response.iter_bytes():
            for frame in decoder.feed(chunk):
                event = _decode_frame(frame)
                if event is _END:
                    return
                if event is not None:
                    yield event
        for frame in decoder.close():
            event = _decode_frame(frame)
            if event is None or event is _END:
                continue
            yield event
    finally:
        _close(response)


async def aiter_stream(response: Any) -> AsyncIterator[StreamEvent]:
    """Async twin of :func:`iter_stream`."""

    queue: "asyncio.Queue" = asyncio.Queue()

    async def pump() -> None:
        decoder = SSEDecoder()
        try:
            async for chunk in response.aiter_bytes():
                for frame in decoder.feed(chunk):
                    await queue.put((frame, None))
            for frame in decoder.close():
                await queue.put((frame, None))
            await queue.put((None, None))
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancel
            raise
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            await queue.put((None, exc))

    task = asyncio.ensure_future(pump())
    try:
        while True:
            item, error = await queue.get()
            if error is not None:
                raise error
            if item is None:
                return
            event = _decode_frame(item)
            if event is _END:
                return
            if event is not None:
                yield event
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - cleanup only
            pass
        await _aclose(response)


def _close(response: Any) -> None:
    """Close a streaming response, tolerating interpreter shutdown.

    When the caller breaks out of the loop the abandoned generator is closed by
    the garbage collector, which lands here. If that happens *while the
    interpreter is finalising*, httpx's client teardown walks objects that are
    already being destroyed and can segfault the whole process (observed on
    CPython 3.13: Response.close -> Client.close inside GC). The OS reclaims the
    socket either way, so skipping the finaliser-time close is strictly safer.
    """
    if sys.is_finalizing():
        return
    try:
        response.close()
    except Exception:  # pragma: no cover - defensive
        pass


async def _aclose(response: Any) -> None:
    if sys.is_finalizing():  # pragma: no cover - shutdown only
        return
    try:
        await response.aclose()
    except Exception:  # pragma: no cover - defensive
        pass
