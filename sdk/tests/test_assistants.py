"""client.assistants — create / list / get / update / delete (contract §2)."""

from __future__ import annotations

import httpx
import pytest

from tai_sdk import TAI
from tai_sdk.types import Assistant, AssistantsPage

from conftest import make_sync_client

ASSISTANT = {
    "id": "asst_abc123",
    "object": "assistant",
    "name": "Study Buddy",
    "model": "tfmf",
    "instructions": "You are a patient tutor.",
    "metadata": {"course": "algorithms"},
    "created_at": "2026-09-30T15:04:05+00:00",
    "updated_at": "2026-09-30T15:04:05+00:00",
}


def test_create_sends_body_and_parses_response(api, home):
    api.json("POST", "/api/v1/assistants", ASSISTANT)
    client = make_sync_client(api)
    try:
        assistant = client.assistants.create(
            model="tfmf",
            name="Study Buddy",
            instructions="You are a patient tutor.",
            metadata={"course": "algorithms"},
        )
    finally:
        client.close()

    assert api.request.method == "POST"
    assert str(api.request.url) == "https://api.test.local/api/v1/assistants"
    assert api.body == {
        "model": "tfmf",
        "name": "Study Buddy",
        "instructions": "You are a patient tutor.",
        "metadata": {"course": "algorithms"},
    }

    assert isinstance(assistant, Assistant)
    assert assistant.id == "asst_abc123"
    assert assistant.object == "assistant"
    assert assistant.name == "Study Buddy"
    assert assistant.model == "tfmf"
    assert assistant.metadata == {"course": "algorithms"}
    assert assistant.created_at == "2026-09-30T15:04:05+00:00"
    assert assistant.raw["id"] == "asst_abc123"
    assert assistant.raw == ASSISTANT
    assert "assistant" in repr(assistant).lower() or "Assistant" in repr(assistant)


def test_create_omits_unset_optional_fields(api, home):
    api.json("POST", "/api/v1/assistants", ASSISTANT)
    client = make_sync_client(api)
    try:
        client.assistants.create(model="tfmf")
    finally:
        client.close()
    assert api.body == {"model": "tfmf"}


def test_create_requires_a_model(api, home):
    client = make_sync_client(api)
    try:
        with pytest.raises(ValueError):
            client.assistants.create(model="")
    finally:
        client.close()
    assert api.requests == []


def test_create_rejects_metadata_over_16_keys(api, home):
    client = make_sync_client(api)
    try:
        with pytest.raises(ValueError):
            client.assistants.create(model="tfmf", metadata={str(i): "v" for i in range(17)})
        with pytest.raises(TypeError):
            client.assistants.create(model="tfmf", metadata={"nested": {"a": 1}})
    finally:
        client.close()
    assert api.requests == []


def test_list_parses_the_pagination_envelope(api, home):
    api.json(
        "GET",
        "/api/v1/assistants",
        {
            "object": "list",
            "data": [ASSISTANT],
            "has_more": False,
            "first_id": "asst_abc123",
            "last_id": "asst_abc123",
        },
    )
    client = make_sync_client(api)
    try:
        page = client.assistants.list(limit=5, after="asst_abc123")
    finally:
        client.close()

    assert api.request.method == "GET"
    assert api.query == {"limit": "5", "after": "asst_abc123"}
    assert isinstance(page, AssistantsPage)
    assert page.object == "list"
    assert page.has_more is False
    assert page.first_id == "asst_abc123"
    assert page.last_id == "asst_abc123"
    assert len(page) == 1
    assert [a.id for a in page] == ["asst_abc123"]
    assert page[0].model == "tfmf"


def test_list_without_pagination_sends_no_query(api, home):
    api.json("GET", "/api/v1/assistants", {"object": "list", "data": [], "has_more": False})
    client = make_sync_client(api)
    try:
        page = client.assistants.list()
    finally:
        client.close()
    assert api.query == {}
    assert page.data == []
    assert len(page) == 0


def test_get(api, home):
    api.json("GET", "/api/v1/assistants/asst_abc123", ASSISTANT)
    client = make_sync_client(api)
    try:
        assistant = client.assistants.get("asst_abc123")
    finally:
        client.close()
    assert api.request.method == "GET"
    assert str(api.request.url) == "https://api.test.local/api/v1/assistants/asst_abc123"
    assert assistant.id == "asst_abc123"


def test_update_sends_only_provided_fields(api, home):
    api.json("PATCH", "/api/v1/assistants/asst_abc123", dict(ASSISTANT, name="Renamed"))
    client = make_sync_client(api)
    try:
        assistant = client.assistants.update("asst_abc123", name="Renamed")
    finally:
        client.close()

    assert api.request.method == "PATCH"
    assert api.body == {"name": "Renamed"}
    assert assistant.name == "Renamed"


def test_update_with_no_fields_is_a_client_error(api, home):
    client = make_sync_client(api)
    try:
        with pytest.raises(ValueError):
            client.assistants.update("asst_abc123")
    finally:
        client.close()
    assert api.requests == []


def test_update_can_send_explicit_null(api, home):
    api.json("PATCH", "/api/v1/assistants/asst_abc123", ASSISTANT)
    client = make_sync_client(api)
    try:
        client.assistants.update("asst_abc123", instructions=None)
    finally:
        client.close()
    assert api.body == {"instructions": None}


def test_delete_accepts_204(api, home):
    api.route("DELETE", "/api/v1/assistants/asst_abc123", lambda *a, **k: httpx.Response(204))
    client = make_sync_client(api)
    try:
        assert client.assistants.delete("asst_abc123") is None
    finally:
        client.close()
    assert api.request.method == "DELETE"
    assert str(api.request.url) == "https://api.test.local/api/v1/assistants/asst_abc123"


def test_authorization_header_is_sent(api, home):
    api.json("GET", "/api/v1/assistants/asst_abc123", ASSISTANT)
    client = make_sync_client(api, api_key="sk-tai-abc")
    try:
        client.assistants.get("asst_abc123")
    finally:
        client.close()
    assert api.request.headers["authorization"] == "Bearer sk-tai-abc"
