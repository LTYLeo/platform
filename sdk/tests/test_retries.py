"""Retries: backoff + jitter, Retry-After, and what may not be retried (§9)."""

from __future__ import annotations

import httpx
import pytest

from tai_sdk import errors

from conftest import make_async_client, make_sync_client


@pytest.fixture
def sleeps(monkeypatch):
    """Record (and neuter) every backoff sleep, sync and async."""
    recorded = []

    def fake_sleep(seconds):
        recorded.append(seconds)

    async def fake_async_sleep(seconds):
        recorded.append(seconds)

    import time

    monkeypatch.setattr(time, "sleep", fake_sleep)
    import asyncio

    monkeypatch.setattr(asyncio, "sleep", fake_async_sleep)
    return recorded


def make_flaky(api, method, path, responses, success):
    """Serve ``responses`` in order, then ``success``; return a call counter."""
    state = {"calls": 0}

    def handler(body, request):
        index = state["calls"]
        state["calls"] += 1
        if index < len(responses):
            status, payload, headers = responses[index]
            return httpx.Response(status, json=payload, headers=headers or {})
        return httpx.Response(200, json=success)

    api.route(method, path, handler)
    return state


def test_retry_then_succeed_on_429(api, home, sleeps):
    state = make_flaky(
        api,
        "GET",
        "/api/v1/models",
        [(429, {"code": "rate_limited", "message": "slow down"}, None)],
        {"object": "list", "data": []},
    )
    client = make_sync_client(api)
    try:
        page = client.models.list()
    finally:
        client.close()

    assert state["calls"] == 2
    assert len(api.requests) == 2
    assert page.data == []
    assert len(sleeps) == 1
    assert 0 <= sleeps[0] <= 0.5  # exponential base 0.5 with full jitter


def test_retry_then_succeed_on_500(api, home, sleeps):
    state = make_flaky(
        api,
        "GET",
        "/api/v1/assistants/asst_1",
        [
            (500, {"code": "internal", "message": "oops"}, None),
            (502, {"code": "backend_unavailable", "message": "oops"}, None),
        ],
        {"id": "asst_1", "object": "assistant", "model": "tfmf"},
    )
    client = make_sync_client(api)
    try:
        assistant = client.assistants.get("asst_1")
    finally:
        client.close()

    assert state["calls"] == 3
    assert assistant.id == "asst_1"
    assert len(sleeps) == 2


def test_429_is_raised_after_max_retries(api, home, sleeps):
    state = make_flaky(
        api,
        "GET",
        "/api/v1/models",
        [(429, {"code": "rate_limited", "message": "nope"}, None)] * 5,
        {"object": "list", "data": []},
    )
    client = make_sync_client(api, max_retries=2)
    try:
        with pytest.raises(errors.RateLimitError):
            client.models.list()
    finally:
        client.close()
    assert state["calls"] == 3  # initial + 2 retries
    assert len(sleeps) == 2


def test_max_retries_zero_disables_retrying(api, home, sleeps):
    state = make_flaky(
        api,
        "GET",
        "/api/v1/models",
        [(503, {"code": "internal", "message": "later"}, None)],
        {"object": "list", "data": []},
    )
    client = make_sync_client(api, max_retries=0)
    try:
        with pytest.raises(errors.InternalServerError):
            client.models.list()
    finally:
        client.close()
    assert state["calls"] == 1
    assert sleeps == []


def test_retry_after_header_is_honoured(api, home, sleeps):
    state = make_flaky(
        api,
        "GET",
        "/api/v1/models",
        [(429, {"code": "rate_limited", "message": "wait"}, {"retry-after": "7"})],
        {"object": "list", "data": []},
    )
    client = make_sync_client(api)
    try:
        client.models.list()
    finally:
        client.close()
    assert state["calls"] == 2
    assert sleeps == [7.0]


def test_backoff_is_exponential(api, home, monkeypatch):
    """Sampled jitter must sit inside [0, base] for each successive attempt."""
    samples = []

    def fake_sleep(seconds):
        samples.append(seconds)

    import time

    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr("tai_sdk._http.random.uniform", lambda a, b: b)

    state = make_flaky(
        api,
        "GET",
        "/api/v1/models",
        [(500, {"code": "internal", "message": "x"}, None)] * 2,
        {"object": "list", "data": []},
    )
    client = make_sync_client(api, max_retries=2)
    try:
        client.models.list()
    finally:
        client.close()
    assert state["calls"] == 3
    assert samples == [0.5, 1.0]


def test_post_is_not_retried_on_5xx(api, home, sleeps):
    """A POST that reached the server may already have generated a completion."""
    state = make_flaky(
        api,
        "POST",
        "/api/v1/chat",
        [(502, {"code": "backend_unavailable", "message": "boom"}, None)],
        {"id": "chat_1", "object": "chat.completion", "output": {"content": "hi"}},
    )
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.BackendUnavailableError):
            client.chat.create(model="tfmf", messages=[{"role": "user", "content": "hi"}])
    finally:
        client.close()
    assert state["calls"] == 1
    assert sleeps == []


def test_post_is_not_retried_on_429(api, home, sleeps):
    state = make_flaky(
        api,
        "POST",
        "/api/v1/chat",
        [(429, {"code": "rate_limited", "message": "wait"}, None)],
        {"id": "chat_1", "object": "chat.completion", "output": {"content": "hi"}},
    )
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.RateLimitError):
            client.chat.create(model="tfmf", messages=[{"role": "user", "content": "hi"}])
    finally:
        client.close()
    assert state["calls"] == 1


def test_post_is_retried_when_it_never_left_the_client(api, home, sleeps):
    """A connect failure means the request provably never reached the server."""
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "object": "chat.completion",
                "output": {"content": "hi"},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = make_sync_client(api, transport=httpx.MockTransport(handler))
    try:
        completion = client.chat.create(model="tfmf", messages=[{"role": "user", "content": "hi"}])
    finally:
        client.close()
    assert state["calls"] == 2
    assert completion.content == "hi"
    assert len(sleeps) == 1


def test_get_retries_connection_errors(api, home, sleeps):
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["calls"] <= 2:
            raise httpx.ConnectError("still refusing", request=request)
        return httpx.Response(200, json={"object": "list", "data": []})

    client = make_sync_client(api, transport=httpx.MockTransport(handler))
    try:
        client.models.list()
    finally:
        client.close()
    assert state["calls"] == 3


def test_connection_errors_give_up_after_max_retries(api, home, sleeps):
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        raise httpx.ConnectError("always refusing", request=request)

    client = make_sync_client(api, transport=httpx.MockTransport(handler), max_retries=2)
    try:
        with pytest.raises(errors.APIConnectionError):
            client.models.list()
    finally:
        client.close()
    assert state["calls"] == 3


def test_timeouts_are_not_retried(api, home, sleeps):
    """A read timeout on a POST may have produced output server-side."""
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        raise httpx.ReadTimeout("too slow", request=request)

    client = make_sync_client(api, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(errors.APITimeoutError):
            client.chat.create(model="tfmf", messages=[{"role": "user", "content": "hi"}])
    finally:
        client.close()
    assert state["calls"] == 1
    assert sleeps == []


def test_delete_is_idempotent_and_retried(api, home, sleeps):
    """DELETE is safe to replay: 204 after one 503."""
    state = {"calls": 0}

    def handler(body, request):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(503, json={"code": "internal", "message": "later"})
        return httpx.Response(204)

    api.route("DELETE", "/api/v1/threads/thrd_1", handler)
    client = make_sync_client(api)
    try:
        assert client.threads.delete("thrd_1") is None
    finally:
        client.close()
    assert state["calls"] == 2
    assert len(api.requests) == 2
    assert len(sleeps) == 1


def test_client_default_is_two_retries(api, home, sleeps):
    state = make_flaky(
        api,
        "GET",
        "/api/v1/models",
        [
            (500, {"code": "internal", "message": "a"}, None),
            (500, {"code": "internal", "message": "b"}, None),
        ],
        {"object": "list", "data": []},
    )
    client = make_sync_client(api)
    try:
        assert client.max_retries == 2
        client.models.list()
    finally:
        client.close()
    assert state["calls"] == 3


async def test_async_retry_then_succeed(api, home, sleeps):
    state = make_flaky(
        api,
        "GET",
        "/api/v1/models",
        [(429, {"code": "rate_limited", "message": "slow"}, None)],
        {"object": "list", "data": []},
    )
    client = make_async_client(api)
    try:
        page = await client.models.list()
    finally:
        await client.aclose()
    assert state["calls"] == 2
    assert page.data == []
    assert len(sleeps) == 1


async def test_async_post_5xx_is_not_retried(api, home, sleeps):
    state = make_flaky(
        api,
        "POST",
        "/api/v1/chat",
        [(502, {"code": "backend_unavailable", "message": "boom"}, None)],
        {"id": "chat_1", "object": "chat.completion", "output": {"content": "hi"}},
    )
    client = make_async_client(api)
    try:
        with pytest.raises(errors.BackendUnavailableError):
            await client.chat.create(model="tfmf", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert state["calls"] == 1
