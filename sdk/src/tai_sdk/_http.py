"""Sync/async HTTP transport: retries, headers, error mapping, streaming.

Design notes
------------

* Every request carries ``Authorization: Bearer sk-tai-...`` and
  ``ngrok-skip-browser-warning: true`` (contract §9), so free ngrok tunnels do
  not inject their interstitial HTML into API responses.
* Retries use exponential backoff with jitter and honour ``Retry-After``.
  Retryable: connection errors, timeouts, HTTP 429 and 5xx — but only for
  idempotent methods (GET/DELETE/HEAD/OPTIONS/PUT) or a POST that provably
  never left the client (a connection error raised before any response bytes,
  i.e. connect failures on a fresh connection). A POST that timed out after
  being sent is **never** retried, because the server may have processed it.
* Every non-2xx becomes the mapped exception from :mod:`tai_sdk.errors`.
* Streaming uses a long read timeout (disabled by default) and is not retried.
"""

from __future__ import annotations

import random
import time
from typing import Any, AsyncIterator, Dict, Iterator, Optional, Tuple, Union

import httpx

from ._config import ClientConfig
from ._streaming import async_events, sync_events
from .errors import (
    APIConnectionError,
    APITimeoutError,
    APIError,
    error_from_response,
)

__all__ = ["BaseTransport", "SyncTransport", "AsyncTransport", "build_headers"]

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE", "PUT", "TRACE"})

#: ``ngrok-skip-browser-warning: true`` on every single request (contract §9).
NGROK_HEADER = "ngrok-skip-browser-warning"

USER_AGENT = "tai-sdk-python"

_DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def build_headers(api_key: Optional[str], extra: Optional[dict] = None) -> Dict[str, str]:
    """Build the default header set for a request."""
    headers = {
        "Accept": "application/json",
        NGROK_HEADER: "true",
        "User-Agent": USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            headers[key] = str(value)
    return headers


def _parse_retry_after(response: Any) -> Optional[float]:
    value = None
    try:
        value = response.headers.get("retry-after")
    except Exception:  # pragma: no cover - defensive
        return None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _backoff(attempt: int, retry_after: Optional[float], max_retries: int) -> float:
    """Exponential backoff with full jitter, capped, or the server's Retry-After."""
    if retry_after is not None:
        return retry_after
    base = min(0.5 * (2 ** attempt), 60.0)
    return random.uniform(0.0, base)


def _decode_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _text(response: Any) -> Optional[str]:
    try:
        return response.text
    except Exception:  # pragma: no cover - defensive
        return None


class BaseTransport:
    """State shared by the sync and async transports."""

    #: True on :class:`AsyncTransport`; used to pick the right stream wrapper.
    is_async = False

    def __init__(
        self,
        config: ClientConfig,
        *,
        http_client: Any = None,
        transport: Any = None,
    ) -> None:
        self.config = config
        self.base_url: str = (config.base_url or "").rstrip("/")
        self.max_retries = max(0, int(config.max_retries))
        self.timeout = config.timeout
        self.default_headers = dict(config.default_headers or {})
        # A caller-supplied client is theirs to close; ours is not.
        self._provided_client = http_client is not None
        self._client = http_client
        self._transport = transport

    # -- constants ---------------------------------------------------------- #
    @property
    def timeout_value(self) -> Union[float, Tuple[Any, Any, Any, Any]]:
        return 30.0 if self.timeout is None else self.timeout

    @property
    def stream_timeout(self) -> Union[float, Tuple[Any, Any, Any, Any]]:
        """Connect timeout applies; the read timeout is disabled so streams can idle."""
        timeout = self.timeout_value
        if isinstance(timeout, (int, float)):
            return (float(timeout), None, float(timeout), float(timeout))
        if isinstance(timeout, tuple) and len(timeout) == 4:
            return (timeout[0], None, timeout[2], timeout[3])
        return (30.0, None, 30.0, 30.0)

    # -- helpers ------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return "%s%s" % (self.base_url, path)

    def _headers(self, extra: Optional[dict]) -> Dict[str, str]:
        merged = dict(self.default_headers)
        if extra:
            merged.update({k: v for k, v in extra.items() if v is not None})
        return build_headers(self.config.api_key, merged)

    def _should_retry_status(self, status_code: int) -> bool:
        return status_code in _DEFAULT_RETRY_STATUSES or 500 <= status_code <= 599

    def _retryable_method(self, method: str) -> bool:
        return method.upper() in IDEMPOTENT_METHODS

    def _raise_for_response(
        self,
        status_code: int,
        payload: Any,
        *,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
        raw_text: Optional[str] = None,
    ) -> None:
        raise error_from_response(
            status_code,
            payload,
            request_id=request_id,
            retry_after=retry_after,
            raw_text=raw_text,
        )

    def _connection_error(self, exc: BaseException) -> APIError:
        if isinstance(exc, httpx.TimeoutException):
            return APITimeoutError(self.base_url, str(exc) or "timed out", cause=exc)
        return APIConnectionError(self.base_url, str(exc) or type(exc).__name__, cause=exc)


class SyncTransport(BaseTransport):
    """Synchronous transport used by :class:`tai_sdk.TAI`."""

    def __init__(self, config: ClientConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout_value,
                transport=self._transport,
                follow_redirects=True,
                headers=None,
            )

    def close(self) -> None:
        client = self._client
        if client is not None and not self._provided_client:
            try:
                client.close()
            except Exception:  # pragma: no cover - defensive
                pass

    # -- core --------------------------------------------------------------- #
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        headers: Optional[dict] = None,
    ) -> Any:
        url = self._url(path)
        method = method.upper()
        self.config.require_api_key()
        clean_params = self._clean_params(params)
        attempt = 0
        while True:
            request_headers = self._headers(headers)
            if json_body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            try:
                response = self._client.request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                raise self._connection_error(exc) from exc
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    self._sleep(_backoff(attempt, None, self.max_retries))
                    attempt += 1
                    continue
                raise self._connection_error(exc) from exc
            except httpx.HTTPError as exc:  # pragma: no cover - defensive
                raise self._connection_error(exc) from exc

            status_code = response.status_code
            retry_after = _parse_retry_after(response)
            if 200 <= status_code < 300:
                return self._success_payload(response)
            payload = _decode_json(response)
            body_text = _text(response)
            if (
                attempt < self.max_retries
                and self._retryable_method(method)
                and self._should_retry_status(status_code)
            ):
                response.close()
                self._sleep(_backoff(attempt, retry_after, self.max_retries))
                attempt += 1
                continue
            self._raise_for_response(
                status_code,
                payload,
                request_id=self._request_id(response),
                retry_after=retry_after,
                raw_text=body_text,
            )

    def open_stream(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        headers: Optional[dict] = None,
    ) -> Any:
        """Send an SSE request and return the open response.

        This happens *eagerly* so that an HTTP error (422, 404, ...) is raised
        by ``client.chat.stream(...)`` itself rather than on first iteration.
        The read timeout is disabled so long streams are not cut off mid-reply.
        """
        url = self._url(path)
        method = method.upper()
        self.config.require_api_key()
        request_headers = self._headers(headers)
        if json_body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        clean_params = self._clean_params(params)
        attempt = 0
        while True:
            try:
                http_request = self._client.build_request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    headers=request_headers,
                    timeout=self.stream_timeout,
                )
                response = self._client.send(http_request, stream=True)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    self._sleep(_backoff(attempt, None, self.max_retries))
                    attempt += 1
                    continue
                raise self._connection_error(exc) from exc
            break

        if response.status_code < 200 or response.status_code >= 300:
            try:
                payload = _decode_json(response)
                body_text = _text(response)
                self._raise_for_response(
                    response.status_code,
                    payload,
                    request_id=self._request_id(response),
                    retry_after=_parse_retry_after(response),
                    raw_text=body_text,
                )
            finally:
                response.close()
        return response

    def stream(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        headers: Optional[dict] = None,
    ) -> Iterator[Any]:
        """Open an SSE request and yield typed events.

        The request is opened eagerly (so HTTP errors raise here) and the
        response is always closed: on exhaustion, on an exception, when the
        caller breaks out of the loop, or when the iterator is collected.
        """
        response = self.open_stream(
            method, path, params=params, json_body=json_body, headers=headers
        )
        return self.events_from(response)

    @staticmethod
    def events_from(response: Any) -> Iterator[Any]:
        """Yield typed events from an already-open streaming response."""
        return sync_events(response)

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _clean_params(params: Optional[dict]) -> Optional[dict]:
        if not params:
            return None
        return {key: value for key, value in params.items() if value is not None}

    @staticmethod
    def _request_id(response: Any) -> Optional[str]:
        for header in ("x-request-id", "request-id", "x-tai-request-id"):
            try:
                value = response.headers.get(header)
            except Exception:  # pragma: no cover - defensive
                return None
            if value:
                return value
        return None

    @staticmethod
    def _success_payload(response: Any) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        return _decode_json(response)

    @staticmethod
    def _sleep(seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class AsyncTransport(BaseTransport):
    """Asynchronous transport used by :class:`tai_sdk.AsyncTAI`."""

    is_async = True

    def __init__(self, config: ClientConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_value,
                transport=self._transport,
                follow_redirects=True,
                headers=None,
            )

    async def aclose(self) -> None:
        client = self._client
        if client is not None and not self._provided_client:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - defensive
                pass

    # -- core --------------------------------------------------------------- #
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        headers: Optional[dict] = None,
    ) -> Any:
        import asyncio

        url = self._url(path)
        method = method.upper()
        self.config.require_api_key()
        clean_params = SyncTransport._clean_params(params)
        attempt = 0
        while True:
            request_headers = self._headers(headers)
            if json_body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                raise self._connection_error(exc) from exc
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(_backoff(attempt, None, self.max_retries))
                    attempt += 1
                    continue
                raise self._connection_error(exc) from exc
            except httpx.HTTPError as exc:  # pragma: no cover - defensive
                raise self._connection_error(exc) from exc

            status_code = response.status_code
            retry_after = _parse_retry_after(response)
            if 200 <= status_code < 300:
                return SyncTransport._success_payload(response)
            payload = _decode_json(response)
            body_text = _text(response)
            if (
                attempt < self.max_retries
                and self._retryable_method(method)
                and self._should_retry_status(status_code)
            ):
                await response.aclose()
                await asyncio.sleep(_backoff(attempt, retry_after, self.max_retries))
                attempt += 1
                continue
            self._raise_for_response(
                status_code,
                payload,
                request_id=SyncTransport._request_id(response),
                retry_after=retry_after,
                raw_text=body_text,
            )

    @staticmethod
    def events_from(response: Any) -> AsyncIterator[Any]:
        """Yield typed events from an already-open streaming response."""
        return async_events(response)

    async def open_stream(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        headers: Optional[dict] = None,
    ) -> Any:
        """Awaitable twin of :meth:`SyncTransport.open_stream`."""
        import asyncio

        url = self._url(path)
        method = method.upper()
        self.config.require_api_key()
        request_headers = self._headers(headers)
        if json_body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        clean_params = SyncTransport._clean_params(params)
        attempt = 0
        while True:
            try:
                http_request = self._client.build_request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    headers=request_headers,
                    timeout=self.stream_timeout,
                )
                response = await self._client.send(http_request, stream=True)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(_backoff(attempt, None, self.max_retries))
                    attempt += 1
                    continue
                raise self._connection_error(exc) from exc
            break

        if response.status_code < 200 or response.status_code >= 300:
            try:
                payload = _decode_json(response)
                body_text = _text(response)
                self._raise_for_response(
                    response.status_code,
                    payload,
                    request_id=SyncTransport._request_id(response),
                    retry_after=_parse_retry_after(response),
                    raw_text=body_text,
                )
            finally:
                await response.aclose()
        return response

    async def stream(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        headers: Optional[dict] = None,
    ) -> AsyncIterator[Any]:
        """Async twin of :meth:`SyncTransport.stream`.

        Returns an async iterator; the request is sent before it is returned,
        so HTTP errors surface from the ``await``. The response is aclosed on
        exhaustion, error or when the caller breaks out of the loop.
        """
        response = await self.open_stream(
            method, path, params=params, json_body=json_body, headers=headers
        )
        return self.events_from(response)
