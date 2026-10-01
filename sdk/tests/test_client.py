"""Cross-cutting details: timeouts, headers, context managers, async parity."""

from __future__ import annotations

import httpx
import pytest

from tai_sdk import AsyncTAI, TAI, errors

from conftest import BASE_URL, make_async_client, make_sync_client


def test_default_timeout_is_30_seconds(api, home):
    client = make_sync_client(api)
    try:
        assert client.timeout == 30.0
        assert client._transport.timeout_value == 30.0
        assert client._transport.stream_timeout[0] == 30.0
        assert client._transport.stream_timeout[1] is None  # read timeout disabled
    finally:
        client.close()


def test_custom_timeout_is_used_for_requests(api, home):
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"object": "list", "data": []})

    client = make_sync_client(api, transport=httpx.MockTransport(handler), timeout=5.0)
    try:
        client.models.list()
    finally:
        client.close()
    assert seen["timeout"]["connect"] == 5.0
    assert seen["timeout"]["read"] == 5.0


def test_streaming_disables_the_read_timeout(api, home):
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"event: message.done\ndata: {}\n\n"),
        )

    client = make_sync_client(api, transport=httpx.MockTransport(handler), timeout=30.0)
    try:
        list(client.chat.stream(model="tfmf", messages=[{"role": "user", "content": "hi"}]))
    finally:
        client.close()
    assert seen["timeout"]["read"] is None
    assert seen["timeout"]["connect"] == 30.0


def test_sync_client_is_a_context_manager(api, home):
    api.json("GET", "/api/v1/models", {"object": "list", "data": []})
    with TAI(
        api_key="sk-tai-test", base_url=BASE_URL, discover=False, transport=api.transport
    ) as client:
        assert not client.is_closed
        client.models.list()
        client.models.list()
    assert client.is_closed


async def test_async_client_is_a_context_manager(api, home):
    api.json("GET", "/api/v1/models", {"object": "list", "data": []})
    async with make_async_client(api) as client:
        assert not client.is_closed
        await client.models.list()
        page = await client.models.list()
        assert page.data == []
    assert client.is_closed


def test_api_key_and_headers_shape(api, home):
    api.json("GET", "/api/v1/models", {"object": "list", "data": []})
    client = make_sync_client(api, api_key="sk-tai-xyz", default_headers={"X-Trace": "abc"})
    try:
        client.models.list()
    finally:
        client.close()
    headers = api.request.headers
    assert headers["authorization"] == "Bearer sk-tai-xyz"
    assert headers["ngrok-skip-browser-warning"] == "true"
    assert headers["accept"] == "application/json"
    assert headers["x-trace"] == "abc"
    assert headers["user-agent"].startswith("tai-sdk-python")


def test_no_trailing_slash_double_up(api, home):
    api.json("GET", "/api/v1/models", {"object": "list", "data": []})
    client = make_sync_client(api, base_url=BASE_URL + "///")
    try:
        client.models.list()
    finally:
        client.close()
    assert str(api.request.url) == BASE_URL + "/api/v1/models"


async def test_async_stream_http_error_raises_on_iteration(api, home):
    """``async for`` is what drives the async request, so the error surfaces there."""
    api.error("POST", "/api/v1/chat", 422, "invalid_request", "bad", param="messages")
    client = make_async_client(api)
    try:
        with pytest.raises(errors.InvalidRequestError) as excinfo:
            async for _ in client.chat.stream(model="tfmf", messages=[{"role": "user", "content": "hi"}]):
                pass
    finally:
        await client.aclose()
    assert excinfo.value.param == "messages"


def test_async_client_creation_is_lazy_about_the_event_loop(api, home):
    # Constructing AsyncTAI outside a running loop must be fine (3.9+).
    client = AsyncTAI(
        api_key="sk-tai-test", base_url=BASE_URL, discover=False, transport=api.transport
    )
    assert client.base_url == BASE_URL
    import asyncio

    async def run():
        api.json("GET", "/api/v1/models", {"object": "list", "data": []})
        async with client:
            return await client.models.list()

    page = asyncio.run(run())
    assert page.data == []


def test_closed_async_client_refuses_work(api, home):
    import asyncio

    client = make_async_client(api)

    async def run():
        await client.aclose()
        await client.aclose()  # idempotent
        assert client.is_closed
        with pytest.raises(RuntimeError):
            await client.models.list()

    asyncio.run(run())


def test_low_level_escape_hatches_exist(api, home):
    api.json("GET", "/api/v1/models", {"object": "list", "data": []})
    client = make_sync_client(api)
    try:
        payload = client.request("GET", "/api/v1/models")
        assert payload["object"] == "list"
    finally:
        client.close()


def test_low_level_stream_escape_hatch(api, home):
    api.sse(
        "POST",
        "/api/v1/chat",
        [
            b'event: message.start\ndata: {"id":"msg_1","model":"tfmf","thread_id":null}\n\n',
            b'event: message.done\ndata: {"id":"msg_1","content":"hi","usage":null}\n\n',
        ],
    )
    client = make_sync_client(api)
    try:
        events = list(client.stream("POST", "/api/v1/chat", json_body={"model": "tfmf"}))
    finally:
        client.close()
    assert [e.event for e in events] == ["message.start", "message.done"]
    assert api.body == {"model": "tfmf"}


async def test_low_level_async_stream_escape_hatch(api, home):
    api.sse(
        "POST",
        "/api/v1/chat",
        [b'event: message.delta\ndata: {"delta":"hey"}\n\n'],
    )
    client = make_async_client(api)
    try:
        stream = await client.astream("POST", "/api/v1/chat", json_body={})
        events = [event async for event in stream]
    finally:
        await client.aclose()
    assert [e.event for e in events] == ["message.delta"]
    assert events[0].delta == "hey"
