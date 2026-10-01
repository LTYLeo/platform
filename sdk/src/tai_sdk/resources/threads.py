"""``client.threads`` and ``client.threads.messages`` (contract §3 and §4)."""

from __future__ import annotations

from typing import Any, Iterator, Optional, Union

from ..types import MessageExchange, MessagesPage, StreamEvent, Thread, ThreadsPage
from ._base import Resource, awaited, open_events

__all__ = ["Threads", "Messages"]

_UNSET = object()

_THREADS_PATH = "/api/v1/threads"


class Messages(Resource):
    """Append messages to a thread, and read them back."""

    def create(
        self,
        thread_id: str,
        *,
        content: str = "",
        stream: bool = False,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        role: Optional[str] = None,
    ) -> Union[MessageExchange, Iterator[StreamEvent]]:
        """``POST /api/v1/threads/{thread_id}/messages`` — append + generate.

        With ``stream=True`` this returns an iterator of typed SSE events; the
        HTTP response is closed for you when you stop iterating.
        """
        if not thread_id:
            raise ValueError("thread_id is required")
        body = self._clean(
            {
                "content": content,
                "stream": True if stream else None,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
                "role": role,
            }
        )
        path = "%s/%s/messages" % (_THREADS_PATH, thread_id)
        if stream:
            return open_events(self._transport, "POST", path, json_body=body)
        payload = self._transport.request("POST", path, json_body=body)
        return awaited(payload, lambda value: MessageExchange.parse(value or {}))

    def list(
        self,
        thread_id: str,
        *,
        limit: Optional[int] = None,
        after: Optional[str] = None,
        order: Optional[str] = None,
    ) -> MessagesPage:
        """``GET /api/v1/threads/{thread_id}/messages`` — ``order`` defaults to ``desc``."""
        if not thread_id:
            raise ValueError("thread_id is required")
        if order is not None and order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc', got %r" % (order,))
        payload = self._transport.request(
            "GET",
            "%s/%s/messages" % (_THREADS_PATH, thread_id),
            params={"limit": limit, "after": after, "order": order},
        )
        return awaited(payload, lambda value: MessagesPage.parse(value or {}))

    # Explicit aliases, so both ``messages.create`` and ``messages.add`` read well.
    def add(self, thread_id: str, **kwargs: Any):
        """Alias for :meth:`create` — append a user message and get the reply."""
        return self.create(thread_id, **kwargs)


class Threads(Resource):
    """A persistent conversation, plus its :class:`Messages` namespace."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.messages = Messages(transport)

    def create(
        self,
        *,
        assistant_id: Optional[str] = None,
        model: Optional[str] = None,
        title: Optional[str] = None,
        metadata: Optional[dict] = None,
        **extra: Any,
    ) -> Thread:
        """``POST /api/v1/threads``.

        If ``assistant_id`` is omitted, ``model`` is required at message time.
        """
        body = self._clean(
            {
                "assistant_id": assistant_id,
                "model": model,
                "title": title,
                "metadata": self._validate_metadata(metadata),
            }
        )
        body.update(extra)
        payload = self._transport.request("POST", _THREADS_PATH, json_body=body)
        return awaited(payload, lambda value: Thread.parse(value or {}))

    def list(self, *, limit: Optional[int] = None, after: Optional[str] = None) -> ThreadsPage:
        """``GET /api/v1/threads?limit=&after=`` — newest first."""
        payload = self._transport.request(
            "GET", _THREADS_PATH, params={"limit": limit, "after": after}
        )
        return awaited(payload, lambda value: ThreadsPage.parse(value or {}))

    def get(self, thread_id: str) -> Thread:
        """``GET /api/v1/threads/{thread_id}``."""
        if not thread_id:
            raise ValueError("thread_id is required")
        payload = self._transport.request("GET", "%s/%s" % (_THREADS_PATH, thread_id))
        return awaited(payload, lambda value: Thread.parse(value or {}))

    def update(
        self,
        thread_id: str,
        *,
        title: Any = _UNSET,
        assistant_id: Any = _UNSET,
        model: Any = _UNSET,
        metadata: Any = _UNSET,
        **extra: Any,
    ) -> Thread:
        """``PATCH /api/v1/threads/{thread_id}`` with any subset of fields."""
        if not thread_id:
            raise ValueError("thread_id is required")
        body: dict = {}
        for key, value in (
            ("title", title),
            ("assistant_id", assistant_id),
            ("model", model),
        ):
            if value is not _UNSET:
                body[key] = value
        if metadata is not _UNSET:
            body["metadata"] = self._validate_metadata(metadata)
        body.update(extra)
        if not body:
            raise ValueError("update() needs at least one of title, assistant_id, model, metadata")
        payload = self._transport.request(
            "PATCH", "%s/%s" % (_THREADS_PATH, thread_id), json_body=body
        )
        return awaited(payload, lambda value: Thread.parse(value or {}))

    def delete(self, thread_id: str) -> None:
        """``DELETE /api/v1/threads/{thread_id}`` -> 204 (messages cascade)."""
        if not thread_id:
            raise ValueError("thread_id is required")
        return awaited(self._transport.request("DELETE", "%s/%s" % (_THREADS_PATH, thread_id)))
