"""The public clients: :class:`TAI` (sync) and :class:`AsyncTAI` (async)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ._config import ClientConfig, resolve_api_key, resolve_base_url
from ._http import AsyncTransport, SyncTransport
from .resources.assistants import Assistants
from .resources.chat import Chat
from .resources.models import Models
from .resources.threads import Threads
from .resources.usage import Usage

__all__ = ["TAI", "AsyncTAI", "DEFAULT_TIMEOUT", "DEFAULT_MAX_RETRIES"]

#: Contract §9 / task spec: 30 s connect+read by default.
DEFAULT_TIMEOUT = 30.0

#: Task spec: retry twice by default.
DEFAULT_MAX_RETRIES = 2


class _BaseClient:
    """Settings + resource namespaces shared by the sync and async clients."""

    _async = False

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Any = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Dict[str, str]] = None,
        discover: bool = True,
        discovery_url: Optional[str] = None,
        discovery_timeout: float = 3.0,
        http_client: Any = None,
        transport: Any = None,
    ) -> None:
        resolved_base_url, source = resolve_base_url(
            base_url,
            discover=discover,
            discovery_url=discovery_url,
            discovery_timeout=discovery_timeout,
            http_client=http_client,
        )
        self._config = ClientConfig(
            base_url=resolved_base_url,
            base_url_source=source,
            api_key=resolve_api_key(api_key),
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            discover=discover,
        )
        self._transport = self._build_transport(http_client=http_client, transport=transport)
        self._closed = False
        self._init_resources()

    # -- subclass hooks ----------------------------------------------------- #
    def _build_transport(self, *, http_client: Any, transport: Any) -> Any:
        raise NotImplementedError

    def _init_resources(self) -> None:
        self.assistants = Assistants(self._transport)
        self.threads = Threads(self._transport)
        self.chat = Chat(self._transport)
        self.models = Models(self._transport)
        self.usage = Usage(self._transport)

    def _shutdown(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def messages(self):
        """Shortcut for :attr:`tai_sdk.resources.Threads.messages`."""
        return self.threads.messages

    # -- introspection ------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        """The resolved base URL (never has a trailing slash)."""
        return self._config.base_url

    @property
    def base_url_source(self) -> str:
        """Where :attr:`base_url` came from: argument/env/config/discovery/default."""
        return self._config.base_url_source

    @property
    def api_key(self) -> Optional[str]:
        """The resolved API key, if any."""
        return self._config.api_key

    @property
    def max_retries(self) -> int:
        return self._config.max_retries

    @property
    def timeout(self) -> Any:
        return self._config.timeout

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(
                "%s is closed. Create a new client instance." % type(self).__name__
            )


class TAI(_BaseClient):
    """Synchronous client for the TAI Assistant API.

    ``TAI()`` works with zero configuration: the base URL is resolved from the
    argument, ``TAI_BASE_URL``, ``~/.tai/config.json``, the discovery document
    (12 h cache) and finally ``https://api.tai-research.dev``; the API key comes
    from the argument or ``TAI_API_KEY``::

        from tai_sdk import TAI

        with TAI() as client:
            reply = client.chat.create(
                model="tfmf",
                messages=[{"role": "user", "content": "Hi"}],
            )
            print(reply.content)
    """

    def _build_transport(self, *, http_client: Any, transport: Any) -> SyncTransport:
        return SyncTransport(self._config, http_client=http_client, transport=transport)

    def _shutdown(self) -> None:
        self._transport.close()

    # -- context manager ---------------------------------------------------- #
    def __enter__(self) -> "TAI":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool. Idempotent."""
        if not self._closed:
            self._closed = True
            self._shutdown()

    # -- sync entry points -------------------------------------------------- #
    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Low-level escape hatch: perform a raw API request."""
        self._ensure_open()
        return self._transport.request(method, path, **kwargs)

    def stream(self, method: str, path: str, **kwargs: Any):
        """Low-level escape hatch: open a raw SSE stream."""
        self._ensure_open()
        return self._transport.stream(method, path, **kwargs)


class AsyncTAI(_BaseClient):
    """Asynchronous twin of :class:`TAI`, with the same namespaces and options.

    ::

        import asyncio
        from tai_sdk import AsyncTAI

        async def main():
            async with AsyncTAI() as client:
                async for event in client.chat.stream(
                    model="tfmf",
                    messages=[{"role": "user", "content": "Hi"}],
                ):
                    if event.event == "message.delta":
                        print(event.delta, end="", flush=True)

        asyncio.run(main())
    """

    _async = True

    def _build_transport(self, *, http_client: Any, transport: Any) -> AsyncTransport:
        return AsyncTransport(self._config, http_client=http_client, transport=transport)

    async def _ashutdown(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> "AsyncTAI":
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool. Idempotent."""
        if not self._closed:
            self._closed = True
            await self._ashutdown()

    # -- async entry points ------------------------------------------------- #
    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Low-level escape hatch: perform a raw API request."""
        self._ensure_open()
        return await self._transport.request(method, path, **kwargs)

    async def astream(self, method: str, path: str, **kwargs: Any):
        """Low-level escape hatch: open a raw SSE stream.

        ``await`` it to get the async iterator, then ``async for`` over it.
        """
        self._ensure_open()
        return await self._transport.stream(method, path, **kwargs)
