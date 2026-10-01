"""``client.chat`` — stateless completions (contract §5 and §6)."""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterable, Iterator, Optional, Union

from ..types import ChatCompletion, ChatMessage, StreamEvent
from ._base import Resource, awaited, open_events as _open_events

__all__ = ["Chat"]

_PATH = "/api/v1/chat"


class Chat(Resource):
    """Stateless chat. Nothing is persisted server-side; ``thread_id`` is null."""

    def create(
        self,
        *,
        messages: Iterable[Any],
        model: Optional[str] = None,
        assistant_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        **extra: Any,
    ) -> ChatCompletion:
        """``POST /api/v1/chat`` with ``stream`` false."""
        body = self._body(
            messages=messages,
            model=model,
            assistant_id=assistant_id,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            extra=extra,
        )
        payload = self._transport.request("POST", _PATH, json_body=body)
        return awaited(payload, lambda value: ChatCompletion.parse(value or {}))

    def stream(
        self,
        *,
        messages: Iterable[Any],
        model: Optional[str] = None,
        assistant_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        **extra: Any,
    ) -> Union[Iterator[StreamEvent], AsyncIterator[StreamEvent]]:
        """``POST /api/v1/chat`` with ``stream: true``.

        Yields :class:`~tai_sdk.types.MessageStart`, then
        :class:`~tai_sdk.types.MessageDelta` frames, then
        :class:`~tai_sdk.types.MessageDone`. An ``error`` frame raises the
        matching :class:`~tai_sdk.errors.APIError` subclass.

        On :class:`~tai_sdk.AsyncTAI` the very same call returns an async
        iterator, so you ``async for`` over it.
        """
        body = self._body(
            messages=messages,
            model=model,
            assistant_id=assistant_id,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            extra=extra,
        )
        body["stream"] = True
        # The transparent wrapper closes the HTTP response when the caller breaks
        # out of the loop, when an exception propagates, or when it is collected.
        return _open_events(self._transport, "POST", _PATH, json_body=body)

    # -- helpers ------------------------------------------------------------ #
    def _body(
        self,
        *,
        messages: Iterable[Any],
        model: Optional[str],
        assistant_id: Optional[str],
        temperature: Optional[float],
        max_output_tokens: Optional[int],
        extra: Optional[dict] = None,
    ) -> dict:
        if messages is None:
            raise ValueError("messages is required")
        normalised = [ChatMessage.coerce(item).to_dict() for item in messages]
        if not normalised:
            raise ValueError("messages must contain at least one message")
        if not model and not assistant_id:
            raise ValueError("chat needs either model= or assistant_id=")
        body = self._clean(
            {
                "model": model,
                "assistant_id": assistant_id,
                "messages": normalised,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            }
        )
        body.update(self._clean(extra or {}))
        return body
