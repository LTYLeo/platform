"""Exception hierarchy for the TAI Assistant API SDK.

Every failure raised by the SDK is a :class:`TAIError`. Errors that came from a
server response additionally carry ``code``, ``message``, ``param``,
``status_code`` and (when the server sent one) ``request_id``.

Stable API error codes (contract §1.1):

======================  ====  ===============================
code                    HTTP  exception
======================  ====  ===============================
``missing_api_key``     401   :class:`AuthenticationError`
``invalid_api_key``     401   :class:`AuthenticationError`
``account_disabled``    403   :class:`PermissionDeniedError`
``insufficient_balance``402   :class:`InsufficientBalanceError`
``not_found``           404   :class:`NotFoundError`
``model_not_found``     404   :class:`ModelNotFoundError`
``invalid_request``     422   :class:`InvalidRequestError`
``model_not_available`` 409   :class:`ModelNotAvailableError`
``rate_limited``        429   :class:`RateLimitError`
``backend_unavailable`` 502   :class:`BackendUnavailableError`
``stream_aborted``      499   :class:`StreamAbortedError`
======================  ====  ===============================
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
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
    "error_from_response",
    "error_from_body",
    "error_from_status",
]

#: Human-readable text for the API codes documented in the contract.
_CODE_MESSAGES = {
    "missing_api_key": "No API key was provided in the Authorization header.",
    "invalid_api_key": "The API key is unknown or has been revoked.",
    "account_disabled": "The account owning this API key is disabled.",
    "insufficient_balance": "The account balance is exhausted.",
    "not_found": "The requested object does not exist.",
    "invalid_request": "The request failed validation.",
    "model_not_available": "The model exists but is not live.",
    "model_not_found": "Unknown model id.",
    "rate_limited": "Too many requests.",
    "backend_unavailable": "The model backend failed.",
    "stream_aborted": "The client disconnected mid-stream.",
}


class TAIError(Exception):
    """Base class for every error raised by this SDK."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: str = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# --------------------------------------------------------------------------- #
# Transport-level errors
# --------------------------------------------------------------------------- #
class TransportError(TAIError):
    """Base class for failures that happened below the HTTP status layer."""


class APIConnectionError(TransportError):
    """The SDK could not reach the API.

    The message always includes the resolved base URL so users can see exactly
    which endpoint (often a rotating ngrok tunnel) was attempted.
    """

    def __init__(self, base_url: str, message: str, *, cause: Optional[BaseException] = None) -> None:
        text = "Could not connect to the TAI API at %s: %s" % (base_url, message)
        super().__init__(text)
        self.base_url = base_url
        self.cause = cause


class APITimeoutError(TransportError):
    """The request timed out.

    Like :class:`APIConnectionError` the message includes the resolved base URL.
    """

    def __init__(self, base_url: str, message: str, *, cause: Optional[BaseException] = None) -> None:
        text = "Request to the TAI API at %s timed out: %s" % (base_url, message)
        super().__init__(text)
        self.base_url = base_url
        self.cause = cause


# --------------------------------------------------------------------------- #
# API errors
# --------------------------------------------------------------------------- #
class APIError(TAIError):
    """A non-2xx response, or an ``error`` event inside an SSE stream."""

    code: str = "api_error"
    status_code: Optional[int] = None

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        param: Optional[str] = None,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        body: Any = None,
    ) -> None:
        if code:
            self.code = code
        resolved_status = status_code if status_code is not None else self.status_code
        # ``self.message`` is the bare server message; ``str(err)`` adds context.
        text = message or _CODE_MESSAGES.get(self.code) or self.code.replace("_", " ")
        super().__init__(text)
        self.param = param
        self.status_code = resolved_status
        self.request_id = request_id
        self.body = body

    def __str__(self) -> str:
        details = []
        if self.status_code is not None:
            details.append("status_code=%s" % self.status_code)
        details.append("code=%s" % self.code)
        if self.param is not None:
            details.append("param=%s" % self.param)
        if self.request_id is not None:
            details.append("request_id=%s" % self.request_id)
        return "%s (%s)" % (self.message, ", ".join(details))

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "%s(code=%r, message=%r, status_code=%r, param=%r, request_id=%r)" % (
            type(self).__name__,
            self.code,
            self.message,
            self.status_code,
            self.param,
            self.request_id,
        )


class APIStatusError(APIError):
    """Base class for non-2xx HTTP responses."""


class BadRequestError(APIStatusError):
    """HTTP 400."""

    code = "bad_request"
    status_code = 400


class AuthenticationError(APIStatusError):
    """HTTP 401 — missing or invalid API key."""

    code = "invalid_api_key"
    status_code = 401


class PermissionDeniedError(APIStatusError):
    """HTTP 403 — the account is disabled or lacks access."""

    code = "account_disabled"
    status_code = 403


class InsufficientBalanceError(APIStatusError):
    """HTTP 402 — balance exhausted (reserved code, not enforced yet)."""

    code = "insufficient_balance"
    status_code = 402


class NotFoundError(APIStatusError):
    """HTTP 404 — no such object, or it belongs to another account."""

    code = "not_found"
    status_code = 404


class ModelNotFoundError(NotFoundError):
    """HTTP 404 with ``model_not_found`` — unknown model id."""

    code = "model_not_found"


class ConflictError(APIStatusError):
    """HTTP 409."""

    code = "conflict"
    status_code = 409


class ModelNotAvailableError(ConflictError):
    """HTTP 409 with ``model_not_available`` — the model is not live yet."""

    code = "model_not_available"


class InvalidRequestError(APIStatusError):
    """HTTP 422 — validation failed. Check :attr:`param`."""

    code = "invalid_request"
    status_code = 422


class RateLimitError(APIStatusError):
    """HTTP 429 — too many requests. Respect ``Retry-After``."""

    code = "rate_limited"
    status_code = 429
    retry_after: Optional[float] = None

    def __init__(self, message: Optional[str] = None, *, retry_after: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        if retry_after is not None:
            self.retry_after = retry_after


class InternalServerError(APIStatusError):
    """HTTP 5xx from the API itself."""

    code = "internal_server_error"
    status_code = 500


class BackendUnavailableError(APIStatusError):
    """HTTP 502 with ``backend_unavailable`` — the model backend failed."""

    code = "backend_unavailable"
    status_code = 502


class StreamAbortedError(APIError):
    """HTTP 499 ``stream_aborted`` — client disconnected mid-stream."""

    code = "stream_aborted"
    status_code = 499


class StreamError(APIError):
    """An ``event: error`` frame inside an SSE stream."""

    code = "stream_error"


class UnknownAPIError(APIStatusError):
    """An unmapped code/status combination. :attr:`code` keeps the server code."""

    code = "unknown_error"


# --------------------------------------------------------------------------- #
# Code/status -> class mapping
# --------------------------------------------------------------------------- #
_CODE_TO_CLASS = {
    "missing_api_key": AuthenticationError,
    "invalid_api_key": AuthenticationError,
    "account_disabled": PermissionDeniedError,
    "insufficient_balance": InsufficientBalanceError,
    "not_found": NotFoundError,
    "model_not_found": ModelNotFoundError,
    "invalid_request": InvalidRequestError,
    "model_not_available": ModelNotAvailableError,
    "rate_limited": RateLimitError,
    "backend_unavailable": BackendUnavailableError,
    "stream_aborted": StreamAbortedError,
}

_STATUS_TO_CLASS = {
    400: BadRequestError,
    401: AuthenticationError,
    402: InsufficientBalanceError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: InvalidRequestError,
    429: RateLimitError,
    499: StreamAbortedError,
    500: InternalServerError,
    501: InternalServerError,
    502: BackendUnavailableError,
    503: InternalServerError,
    504: InternalServerError,
}


def _parse_retry_after(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def error_from_body(
    payload: Any,
    *,
    fallback_message: Optional[str] = None,
    default_status: Optional[int] = None,
    request_id: Optional[str] = None,
    retry_after: Optional[float] = None,
) -> APIError:
    """Build the right :class:`APIError` from a decoded error body.

    Used both for HTTP error responses and for ``event: error`` SSE frames.
    """

    code: Optional[str] = None
    message: Optional[str] = None
    param: Optional[str] = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        raw_message = payload.get("message")
        raw_param = payload.get("param")
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
        if isinstance(raw_param, str) and raw_param:
            param = raw_param

    if message is None:
        message = fallback_message

    if code is not None and code in _CODE_TO_CLASS:
        cls = _CODE_TO_CLASS[code]
    elif default_status is not None and default_status in _STATUS_TO_CLASS:
        cls = _STATUS_TO_CLASS[default_status]
    elif default_status is not None and 500 <= default_status <= 599:
        cls = InternalServerError
    elif code is not None:
        cls = UnknownAPIError
    else:
        cls = APIError

    if cls is RateLimitError:
        return RateLimitError(
            message,
            code=code,
            param=param,
            status_code=default_status,
            request_id=request_id,
            body=payload,
            retry_after=retry_after,
        )
    return cls(
        message,
        code=code,
        param=param,
        status_code=default_status,
        request_id=request_id,
        body=payload,
    )


def error_from_status(
    status_code: int,
    *,
    request_id: Optional[str] = None,
    retry_after: Optional[float] = None,
) -> APIStatusError:
    """Build the right error when the body was empty or not JSON."""

    cls = _STATUS_TO_CLASS.get(status_code)
    if cls is None:
        cls = InternalServerError if status_code >= 500 else APIStatusError
    if cls is RateLimitError:
        return RateLimitError(
            None,
            status_code=status_code,
            request_id=request_id,
            retry_after=retry_after,
        )
    return cls(None, status_code=status_code, request_id=request_id)


def error_from_response(
    status_code: int,
    payload: Any,
    *,
    request_id: Optional[str] = None,
    retry_after: Optional[float] = None,
    raw_text: Optional[str] = None,
) -> APIError:
    """Map a non-2xx HTTP response onto the correct exception class."""

    if isinstance(payload, dict):
        return error_from_body(
            payload,
            default_status=status_code,
            request_id=request_id,
            retry_after=retry_after,
        )
    fallback = None
    if raw_text:
        snippet = raw_text.strip()
        if snippet:
            fallback = snippet if len(snippet) <= 300 else snippet[:300] + "..."
    if fallback:
        return error_from_body(
            {"message": fallback},
            default_status=status_code,
            request_id=request_id,
            retry_after=retry_after,
        )
    return error_from_status(status_code, request_id=request_id, retry_after=retry_after)
