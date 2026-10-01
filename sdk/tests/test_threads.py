"""client.threads and client.threads.messages (contract §3 and §4)."""

from __future__ import annotations

import httpx
import pytest

from tai_sdk.types import Message, MessageExchange, MessagesPage, Thread, ThreadsPage

from conftest import make_sync_client

THREAD = {
    "id": "thrd_xyz789",
    "object": "thread",
    "title": "Factorial help",
    "assistant_id": "asst_abc123",
    "metadata": {},
    "message_count": 4,
    "created_at": "2026-09-30T15:04:05+00:00",
    "updated_at": "2026-09-30T15:05:00+00:00",
}

USER_MESSAGE = {
    "id": "msg_user1",
    "object": "message",
    "thread_id": "thrd_xyz789",
    "role": "user",
    "content": "How do I reverse a list?",
    "model": None,
    "usage": None,
    "created_at": "2026-09-30T15:04:10+00:00",
}

ASSISTANT_MESSAGE = {
    "id": "msg_asst1",
    "object": "message",
    "thread_id": "thrd_xyz789",
    "role": "assistant",
    "content": "Use slicing: items[::-1].",
    "model": "tfmf",
    "usage": {"input_tokens": 12, "output_tokens": 40, "cost_cny": 0.000126, "estimated": True},
    "created_at": "2026-09-30T15:04:11+00:00",
}

EXCHANGE = {
    "object": "message.exchange",
    "thread_id": "thrd_xyz789",
    "user_message": USER_MESSAGE,
    "assistant_message": ASSISTANT_MESSAGE,
    "usage": {
        "model": "tfmf",
        "input_tokens": 128,
        "output_tokens": 42,
        "cost_cny": 0.00019,
        "estimated": True,
    },
}


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #
def test_create_thread(api, home):
    api.json("POST", "/api/v1/threads", THREAD)
    client = make_sync_client(api)
    try:
        thread = client.threads.create(
            assistant_id="asst_abc123", title="Factorial help", metadata={"tag": "math"}
        )
    finally:
        client.close()

    assert api.body == {
        "assistant_id": "asst_abc123",
        "title": "Factorial help",
        "metadata": {"tag": "math"},
    }
    assert isinstance(thread, Thread)
    assert thread.id == "thrd_xyz789"
    assert thread.object == "thread"
    assert thread.assistant_id == "asst_abc123"
    assert thread.message_count == 4
    assert thread.updated_at == "2026-09-30T15:05:00+00:00"
    assert thread.raw == THREAD


def test_create_thread_with_model_only(api, home):
    api.json("POST", "/api/v1/threads", THREAD)
    client = make_sync_client(api)
    try:
        client.threads.create(model="tfmf")
    finally:
        client.close()
    assert api.body == {"model": "tfmf"}


def test_list_threads_honours_pagination(api, home):
    api.json(
        "GET",
        "/api/v1/threads",
        {
            "object": "list",
            "data": [THREAD],
            "has_more": True,
            "first_id": "thrd_xyz789",
            "last_id": "thrd_xyz789",
        },
    )
    client = make_sync_client(api)
    try:
        page = client.threads.list(limit=1, after="thrd_xyz789")
    finally:
        client.close()
    assert api.query == {"limit": "1", "after": "thrd_xyz789"}
    assert isinstance(page, ThreadsPage)
    assert page.has_more is True
    assert page.first_id == "thrd_xyz789"
    assert [t.id for t in page.data] == ["thrd_xyz789"]


def test_get_thread(api, home):
    api.json("GET", "/api/v1/threads/thrd_xyz789", THREAD)
    client = make_sync_client(api)
    try:
        thread = client.threads.get("thrd_xyz789")
    finally:
        client.close()
    assert str(api.request.url) == "https://api.test.local/api/v1/threads/thrd_xyz789"
    assert thread.title == "Factorial help"


def test_update_thread_replaces_assistant(api, home):
    api.json("PATCH", "/api/v1/threads/thrd_xyz789", dict(THREAD, assistant_id="asst_new"))
    client = make_sync_client(api)
    try:
        thread = client.threads.update("thrd_xyz789", title="Renamed", assistant_id="asst_new")
    finally:
        client.close()
    assert api.request.method == "PATCH"
    assert api.body == {"title": "Renamed", "assistant_id": "asst_new"}
    assert thread.assistant_id == "asst_new"


def test_update_thread_can_clear_assistant_with_null(api, home):
    api.json("PATCH", "/api/v1/threads/thrd_xyz789", dict(THREAD, assistant_id=None))
    client = make_sync_client(api)
    try:
        client.threads.update("thrd_xyz789", assistant_id=None)
    finally:
        client.close()
    assert api.body == {"assistant_id": None}


def test_delete_thread(api, home):
    api.route("DELETE", "/api/v1/threads/thrd_xyz789", lambda *a, **k: httpx.Response(204))
    client = make_sync_client(api)
    try:
        assert client.threads.delete("thrd_xyz789") is None
    finally:
        client.close()
    assert api.request.method == "DELETE"


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def test_threads_expose_a_messages_namespace(api, home):
    client = make_sync_client(api)
    try:
        assert client.threads.messages is client.messages
    finally:
        client.close()


def test_create_message_returns_both_messages(api, home):
    api.json("POST", "/api/v1/threads/thrd_xyz789/messages", EXCHANGE)
    client = make_sync_client(api)
    try:
        exchange = client.threads.messages.create(
            "thrd_xyz789", content="How do I reverse a list?"
        )
    finally:
        client.close()

    assert str(api.request.url) == "https://api.test.local/api/v1/threads/thrd_xyz789/messages"
    assert api.body == {"content": "How do I reverse a list?"}
    assert isinstance(exchange, MessageExchange)
    assert exchange.object == "message.exchange"
    assert exchange.thread_id == "thrd_xyz789"
    assert exchange.user_message.id == "msg_user1"
    assert exchange.user_message.role == "user"
    assert exchange.user_message.usage is None
    assert exchange.assistant_message.model == "tfmf"
    assert exchange.assistant_message.usage.output_tokens == 40
    assert exchange.assistant_message.usage.estimated is True
    assert exchange.usage.cost_cny == pytest.approx(0.00019)
    assert exchange.content == "Use slicing: items[::-1]."
    assert exchange.raw == EXCHANGE


def test_create_message_sends_generation_options(api, home):
    api.json("POST", "/api/v1/threads/thrd_xyz789/messages", EXCHANGE)
    client = make_sync_client(api)
    try:
        client.threads.messages.create(
            "thrd_xyz789",
            content="hi",
            temperature=0.2,
            max_output_tokens=1024,
            role="user",
        )
    finally:
        client.close()
    assert api.body == {
        "content": "hi",
        "temperature": 0.2,
        "max_output_tokens": 1024,
        "role": "user",
    }


def test_list_messages_default_order_is_not_sent(api, home):
    api.json(
        "GET",
        "/api/v1/threads/thrd_xyz789/messages",
        {
            "object": "list",
            "data": [USER_MESSAGE, ASSISTANT_MESSAGE],
            "has_more": False,
            "first_id": "msg_user1",
            "last_id": "msg_asst1",
        },
    )
    client = make_sync_client(api)
    try:
        page = client.threads.messages.list("thrd_xyz789", limit=10)
    finally:
        client.close()
    assert api.query == {"limit": "10"}
    assert isinstance(page, MessagesPage)
    assert [m.id for m in page.data] == ["msg_user1", "msg_asst1"]
    assert isinstance(page.data[0], Message)


def test_list_messages_with_order(api, home):
    api.json(
        "GET",
        "/api/v1/threads/thrd_xyz789/messages",
        {"object": "list", "data": [], "has_more": False},
    )
    client = make_sync_client(api)
    try:
        client.threads.messages.list("thrd_xyz789", order="asc", after="msg_user1")
    finally:
        client.close()
    assert api.query == {"order": "asc", "after": "msg_user1"}


def test_list_messages_rejects_a_bad_order(api, home):
    client = make_sync_client(api)
    try:
        with pytest.raises(ValueError):
            client.threads.messages.list("thrd_xyz789", order="sideways")
    finally:
        client.close()
    assert api.requests == []
