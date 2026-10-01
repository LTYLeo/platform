"""Error mapping: every non-2xx becomes the right exception (contract §1.1)."""

from __future__ import annotations

import httpx
import pytest

from tai_sdk import errors

from conftest import BASE_URL, make_sync_client


@pytest.mark.parametrize(
    "status, code, expected",
    [
        (401, "missing_api_key", errors.AuthenticationError),
        (401, "invalid_api_key", errors.AuthenticationError),
        (403, "account_disabled", errors.PermissionDeniedError),
        (402, "insufficient_balance", errors.InsufficientBalanceError),
        (404, "not_found", errors.NotFoundError),
        (404, "model_not_found", errors.ModelNotFoundError),
        (409, "model_not_available", errors.ModelNotAvailableError),
        (422, "invalid_request", errors.InvalidRequestError),
        (429, "rate_limited", errors.RateLimitError),
        (502, "backend_unavailable", errors.BackendUnavailableError),
        (500, "internal", errors.InternalServerError),
        (400, "bad_thing", errors.BadRequestError),
    ],
)
def test_status_and_code_mapping(api, home, status, code, expected):
    payload = {"code": code, "message": "Human readable sentence"}
    if status == 422:
        payload["param"] = "model"
    api.json("POST", "/api/v1/chat", payload, status_code=status)
    client = make_sync_client(api)
    try:
        with pytest.raises(expected) as excinfo:
            client.chat.create(model="tfmf", messages=[{"role": "user", "content": "hi"}])
    finally:
        client.close()

    error = excinfo.value
    assert isinstance(error, errors.TAIError)
    assert isinstance(error, errors.APIError)
    assert error.code == code
    assert error.message == "Human readable sentence"
    assert error.status_code == status
    assert error.param == ("model" if status == 422 else None)
    assert error.request_id is None
    assert error.body == payload
    assert "code=%s" % code in str(error)
    assert "status_code=%d" % status in str(error)


def test_model_not_found_is_also_a_not_found(api, home):
    api.error("GET", "/api/v1/assistants/asst_x", 404, "model_not_found")
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.ModelNotFoundError) as excinfo:
            client.assistants.get("asst_x")
    finally:
        client.close()
    assert isinstance(excinfo.value, errors.NotFoundError)


def test_request_id_is_captured(api, home):
    def handler(body, request):
        return httpx.Response(
            429,
            json={"code": "rate_limited", "message": "slow down"},
            headers={"x-request-id": "req_12345", "retry-after": "1"},
        )

    api.route("GET", "/api/v1/models", handler)
    client = make_sync_client(api, max_retries=0)
    try:
        with pytest.raises(errors.RateLimitError) as excinfo:
            client.models.list()
    finally:
        client.close()
    assert excinfo.value.request_id == "req_12345"
    assert "request_id=req_12345" in str(excinfo.value)
    assert excinfo.value.retry_after == 1.0


def test_unmapped_code_keeps_the_server_code(api, home):
    api.error("GET", "/api/v1/models", 418, "teapot")
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.UnknownAPIError) as excinfo:
            client.models.list()
    finally:
        client.close()
    assert excinfo.value.code == "teapot"
    assert excinfo.value.status_code == 418


def test_non_json_error_body_still_maps(api, home):
    api.route(
        "GET",
        "/api/v1/models",
        lambda *a, **k: httpx.Response(502, text="<html>Bad Gateway</html>"),
    )
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.BackendUnavailableError) as excinfo:
            client.models.list()
    finally:
        client.close()
    assert "Bad Gateway" in excinfo.value.message
    assert excinfo.value.status_code == 502


def test_bare_status_error(api, home):
    api.route("GET", "/api/v1/models", lambda *a, **k: httpx.Response(503))
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.InternalServerError) as excinfo:
            client.models.list()
    finally:
        client.close()
    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "internal_server_error"


def test_missing_message_falls_back_to_the_code(api, home):
    api.json("GET", "/api/v1/models", {"code": "not_found"}, status_code=404)
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.NotFoundError) as excinfo:
            client.models.list()
    finally:
        client.close()
    assert excinfo.value.message  # never empty


def test_connect_error_mentions_the_resolved_base_url(api, home):
    def explode(request):
        raise httpx.ConnectError("nodename nor servname provided", request=request)

    transport = httpx.MockTransport(explode)
    client = make_sync_client(api, transport=transport)
    try:
        with pytest.raises(errors.APIConnectionError) as excinfo:
            client.models.list()
    finally:
        client.close()
    assert BASE_URL in str(excinfo.value)
    assert excinfo.value.base_url == BASE_URL
    assert isinstance(excinfo.value, errors.TransportError)
    assert isinstance(excinfo.value, errors.TAIError)


def test_timeout_error_mentions_the_resolved_base_url(api, home):
    def explode(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    transport = httpx.MockTransport(explode)
    client = make_sync_client(api, transport=transport)
    try:
        with pytest.raises(errors.APITimeoutError) as excinfo:
            client.models.list()
    finally:
        client.close()
    assert BASE_URL in str(excinfo.value)
    assert "timed out" in str(excinfo.value).lower()


async def test_async_connect_error(api, home):
    from conftest import make_async_client

    def explode(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = make_async_client(api, transport=httpx.MockTransport(explode))
    try:
        with pytest.raises(errors.APIConnectionError) as excinfo:
            await client.models.list()
    finally:
        await client.aclose()
    assert BASE_URL in str(excinfo.value)


def test_error_hierarchy_is_importable_and_stable():
    for name in (
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
    ):
        assert hasattr(errors, name), name
        assert name in errors.__all__

    assert issubclass(errors.ModelNotAvailableError, errors.ConflictError)
    assert issubclass(errors.NotFoundError, errors.APIStatusError)
    assert issubclass(errors.APIStatusError, errors.APIError)
    assert issubclass(errors.APIError, errors.TAIError)
    assert issubclass(errors.APITimeoutError, errors.TransportError)
