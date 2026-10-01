"""SSE streaming: incremental parsing, typed events, early-exit cleanup (§6)."""

from __future__ import annotations

import httpx
import pytest

from tai_sdk import errors
from tai_sdk._streaming import SSEDecoder
from tai_sdk.types import MessageDelta, MessageDone, MessageStart, StreamEvent

from conftest import BASE_URL, make_async_client, make_sync_client


def frame(event: str, data: str) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, data)).encode("utf-8")


START = frame("message.start", '{"id":"msg_1","model":"tfmf","thread_id":"thrd_1"}')
DELTA_A = frame("message.delta", '{"delta":"Hel"}')
DELTA_B = frame("message.delta", '{"delta":"lo"}')
DONE = frame(
    "message.done",
    '{"id":"msg_1","content":"Hello","usage":{"input_tokens":12,"output_tokens":40,'
    '"cost_cny":0.000126,"estimated":true}}',
)
ERROR = frame("error", '{"code":"backend_unavailable","message":"the model backend failed"}')

CHAT_PATH = "/api/v1/chat"
CHAT_BODY = {"model": "tfmf", "messages": [{"role": "user", "content": "hi"}]}


class RecordingStreamTransport(httpx.BaseTransport):
    """Returns a real streaming response and records when it gets closed."""

    def __init__(self, chunks, status_code: int = 200) -> None:
        self.chunks = list(chunks)
        self.status_code = status_code
        self.requests = []
        self.closed = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        class TrackedStream(httpx.SyncByteStream):
            def __init__(self, chunks, on_close):
                self._chunks = chunks
                self._on_close = on_close

            def __iter__(self):
                for chunk in self._chunks:
                    yield chunk

            def close(self):
                self._on_close()

        outer = self

        def on_close():
            outer.closed += 1

        return httpx.Response(
            self.status_code,
            headers={"content-type": "text/event-stream"},
            stream=TrackedStream(self.chunks, on_close),
        )


class RecordingAsyncStreamTransport(httpx.AsyncBaseTransport):
    """Async twin of :class:`RecordingStreamTransport`."""

    def __init__(self, chunks, status_code: int = 200) -> None:
        self.chunks = list(chunks)
        self.status_code = status_code
        self.requests = []
        self.closed = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outer = self

        class TrackedStream(httpx.AsyncByteStream):
            def __init__(self, chunks, on_close):
                self._chunks = chunks
                self._on_close = on_close

            async def __aiter__(self):
                for chunk in self._chunks:
                    yield chunk

            async def aclose(self):
                self._on_close()

        def on_close():
            outer.closed += 1

        return httpx.Response(
            self.status_code,
            headers={"content-type": "text/event-stream"},
            stream=TrackedStream(self.chunks, on_close),
        )


# --------------------------------------------------------------------------- #
# Decoder unit tests
# --------------------------------------------------------------------------- #
def test_decoder_reassembles_a_split_frame():
    decoder = SSEDecoder()
    assert decoder.feed(b"event: message.delta\ndata: {\"del") == []
    assert decoder.feed(b"ta\":\"Hi\"}\n") == []
    frames = decoder.feed(b"\n")
    assert len(frames) == 1
    assert frames[0].event == "message.delta"
    assert frames[0].data == '{"delta":"Hi"}'


def test_decoder_ignores_comments_and_handles_crlf():
    decoder = SSEDecoder()
    frames = decoder.feed(b": keep-alive\r\nevent: message.delta\r\ndata: {\"delta\":\"x\"}\r\n\r\n")
    assert [f.event for f in frames] == ["message.delta"]
    assert frames[0].data == '{"delta":"x"}'


def test_decoder_flushes_an_unterminated_final_frame():
    decoder = SSEDecoder()
    assert decoder.feed(b"event: message.done\ndata: {\"content\":\"bye\"}") == []
    frames = decoder.close()
    assert [f.event for f in frames] == ["message.done"]


# --------------------------------------------------------------------------- #
# chat.stream (sync)
# --------------------------------------------------------------------------- #
def test_stream_yields_typed_events(api, home):
    api.sse("POST", CHAT_PATH, [START, DELTA_A, DELTA_B, DONE])
    client = make_sync_client(api)
    try:
        events = list(client.chat.stream(**CHAT_BODY))
    finally:
        client.close()

    assert api.body == {"model": "tfmf", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    assert api.request.headers["accept"] == "application/json"
    assert [e.event for e in events] == [
        "message.start",
        "message.delta",
        "message.delta",
        "message.done",
    ]

    start, delta_a, delta_b, done = events
    assert isinstance(start, MessageStart)
    assert start.id == "msg_1"
    assert start.model == "tfmf"
    assert start.thread_id == "thrd_1"

    assert isinstance(delta_a, MessageDelta)
    assert delta_a.delta == "Hel"
    assert delta_b.delta == "lo"

    assert isinstance(done, MessageDone)
    assert done.content == "Hello"
    assert done.id == "msg_1"
    assert done.usage.input_tokens == 12
    assert done.usage.output_tokens == 40
    assert done.usage.estimated is True


def test_stream_handles_a_split_chunk_boundary(api, home):
    payload = START + DELTA_A + DELTA_B + DONE
    cut = len(START) + 9  # inside DELTA_A's JSON
    api.sse("POST", CHAT_PATH, [payload[:cut], payload[cut:]])
    client = make_sync_client(api)
    try:
        deltas = [e.delta for e in client.chat.stream(**CHAT_BODY) if isinstance(e, MessageDelta)]
    finally:
        client.close()
    assert deltas == ["Hel", "lo"]


def test_stream_handles_a_split_utf8_sequence(api, home):
    text = "café ☕".encode("utf-8")
    payload = START + frame("message.delta", '{"delta":"caf\u00e9 \u2615"}') + DONE
    # Split in the middle of a multi-byte character.
    marker = payload.index(text)
    chunks = [payload[: marker + 1], payload[marker + 1 :]]
    api.sse("POST", CHAT_PATH, chunks)
    client = make_sync_client(api)
    try:
        deltas = "".join(
            e.delta for e in client.chat.stream(**CHAT_BODY) if isinstance(e, MessageDelta)
        )
    finally:
        client.close()
    assert deltas == "café ☕"


def test_stream_raises_on_an_error_event(api, home):
    api.sse("POST", CHAT_PATH, [START, DELTA_A, ERROR])
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.BackendUnavailableError) as excinfo:
            list(client.chat.stream(**CHAT_BODY))
    finally:
        client.close()
    error = excinfo.value
    assert error.code == "backend_unavailable"
    assert error.message == "the model backend failed"
    assert error.status_code == 502
    assert isinstance(error, errors.APIError)


def test_stream_error_event_with_an_unmapped_code(api, home):
    api.sse("POST", CHAT_PATH, [frame("error", '{"code":"weird_code","message":"huh"}')])
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.UnknownAPIError):
            list(client.chat.stream(**CHAT_BODY))
    finally:
        client.close()


def test_stream_stops_at_the_done_sentinel(api, home):
    api.sse("POST", CHAT_PATH, [START, DONE, b"data: [DONE]\n\n", DELTA_A])
    client = make_sync_client(api)
    try:
        events = list(client.chat.stream(**CHAT_BODY))
    finally:
        client.close()
    assert [e.event for e in events] == ["message.start", "message.done"]


def test_stream_response_is_closed_when_the_consumer_breaks(api, home):
    transport = RecordingStreamTransport([START, DELTA_A, DELTA_B, DONE])
    client = make_sync_client(api, transport=transport)
    try:
        stream = client.chat.stream(**CHAT_BODY)
        seen = []
        for event in stream:
            seen.append(event.event)
            if isinstance(event, MessageDelta):
                break
        assert seen == ["message.start", "message.delta"]
        assert transport.closed == 0  # nothing closes it while the iterator lives
        stream.close()  # what the ``with`` statement does for you
        assert transport.closed == 1
    finally:
        client.close()


def test_stream_response_is_closed_when_an_exception_propagates(api, home):
    transport = RecordingStreamTransport([START, DELTA_A, ERROR])
    client = make_sync_client(api, transport=transport)
    try:
        with pytest.raises(errors.BackendUnavailableError):
            for _ in client.chat.stream(**CHAT_BODY):
                pass
    finally:
        client.close()
    assert transport.closed == 1


def test_stream_response_is_closed_when_used_as_a_context_manager(api, home):
    transport = RecordingStreamTransport([START, DELTA_A, DELTA_B, DONE])
    client = make_sync_client(api, transport=transport)
    try:
        with client.chat.stream(**CHAT_BODY) as stream:
            for _ in stream:
                break
            assert transport.closed == 0
        assert transport.closed == 1
    finally:
        client.close()


def test_stream_response_is_closed_when_the_generator_is_dropped(api, home):
    import gc

    transport = RecordingStreamTransport([START, DELTA_A, DELTA_B, DONE])
    client = make_sync_client(api, transport=transport)
    try:
        stream = client.chat.stream(**CHAT_BODY)
        for _ in stream:
            break
        del stream
        gc.collect()
        assert transport.closed == 1
    finally:
        client.close()


def test_streaming_http_error_is_raised_before_iteration(api, home):
    api.error("POST", CHAT_PATH, 422, "invalid_request", "bad temperature", param="temperature")
    client = make_sync_client(api)
    try:
        with pytest.raises(errors.InvalidRequestError) as excinfo:
            client.chat.stream(**CHAT_BODY)
    finally:
        client.close()
    assert excinfo.value.param == "temperature"


def test_thread_message_stream_reports_the_thread(api, home):
    path = "/api/v1/threads/thrd_1/messages"
    api.sse(
        "POST",
        path,
        [
            frame("message.start", '{"id":"msg_2","model":"tfmf","thread_id":"thrd_1"}'),
            DELTA_A,
            DONE,
        ],
    )
    client = make_sync_client(api)
    try:
        events = list(client.threads.messages.create("thrd_1", content="hi", stream=True))
    finally:
        client.close()
    assert api.body == {"content": "hi", "stream": True}
    assert events[0].thread_id == "thrd_1"
    assert isinstance(events[-1], MessageDone)


def test_unknown_event_names_are_preserved(api, home):
    api.sse("POST", CHAT_PATH, [START, frame("message.heartbeat", '{"seq":1}'), DONE])
    client = make_sync_client(api)
    try:
        events = list(client.chat.stream(**CHAT_BODY))
    finally:
        client.close()
    assert [e.event for e in events] == ["message.start", "message.heartbeat", "message.done"]
    assert isinstance(events[1], StreamEvent)
    assert events[1].raw == {"seq": 1}


# --------------------------------------------------------------------------- #
# chat.stream (async)
# --------------------------------------------------------------------------- #
async def test_async_stream_yields_typed_events(api, home):
    api.sse("POST", CHAT_PATH, [START, DELTA_A, DELTA_B, DONE])
    client = make_async_client(api)
    try:
        events = [event async for event in client.chat.stream(**CHAT_BODY)]
    finally:
        await client.aclose()

    assert [e.event for e in events] == [
        "message.start",
        "message.delta",
        "message.delta",
        "message.done",
    ]
    assert events[1].delta == "Hel"
    assert events[-1].usage.output_tokens == 40


async def test_async_stream_handles_a_split_boundary(api, home):
    payload = START + DELTA_A + DELTA_B + DONE
    api.sse("POST", CHAT_PATH, [payload[:20], payload[20:]])
    client = make_async_client(api)
    try:
        deltas = [
            e.delta
            async for e in client.chat.stream(**CHAT_BODY)
            if isinstance(e, MessageDelta)
        ]
    finally:
        await client.aclose()
    assert deltas == ["Hel", "lo"]


async def test_async_stream_raises_on_error_event(api, home):
    api.sse("POST", CHAT_PATH, [START, ERROR])
    client = make_async_client(api)
    try:
        with pytest.raises(errors.BackendUnavailableError):
            async for _ in client.chat.stream(**CHAT_BODY):
                pass
    finally:
        await client.aclose()


async def test_async_stream_closes_the_response_on_early_break(api, home):
    transport = RecordingAsyncStreamTransport([START, DELTA_A, DELTA_B, DONE])
    client = make_async_client(api, transport=transport)
    try:
        stream = client.chat.stream(**CHAT_BODY)
        async for event in stream:
            if isinstance(event, MessageDelta):
                break
        await stream.aclose()  # dropping the iterator does this for you
        assert transport.closed == 1
    finally:
        await client.aclose()


async def test_async_stream_closes_the_response_on_error(api, home):
    transport = RecordingAsyncStreamTransport([START, DELTA_A, ERROR])
    client = make_async_client(api, transport=transport)
    try:
        with pytest.raises(errors.BackendUnavailableError):
            async for _ in client.chat.stream(**CHAT_BODY):
                pass
    finally:
        await client.aclose()
    assert transport.closed == 1


async def test_async_errors_are_mapped(api, home):
    api.error("POST", CHAT_PATH, 401, "invalid_api_key", "nope")
    client = make_async_client(api)
    try:
        with pytest.raises(errors.AuthenticationError):
            await client.chat.create(**CHAT_BODY)
    finally:
        await client.aclose()


def test_stream_retries_a_connection_error_before_any_response(api, home, monkeypatch):
    """A connect failure means the stream request never reached the server."""
    monkeypatch.setattr("tai_sdk._http.random.uniform", lambda a, b: 0.0)
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(START + DONE),
        )

    client = make_sync_client(api, transport=httpx.MockTransport(handler))
    try:
        events = list(client.chat.stream(**CHAT_BODY))
    finally:
        client.close()
    assert state["calls"] == 2
    assert [e.event for e in events] == ["message.start", "message.done"]
