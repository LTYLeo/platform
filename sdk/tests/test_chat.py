"""client.chat and client.models / client.usage happy paths (contract §5, §7)."""

from __future__ import annotations

import pytest

from tai_sdk.types import ChatCompletion, ChatMessage, ModelInfo, ModelsPage, Usage, UsageSummary

from conftest import make_sync_client

COMPLETION = {
    "id": "chat_abc123",
    "object": "chat.completion",
    "model": "tfmf",
    "output": {"role": "assistant", "content": "A factorial is a product of 1..n."},
    "usage": {"input_tokens": 12, "output_tokens": 40, "cost_cny": 0.000126, "estimated": True},
    "created_at": "2026-09-30T15:04:05+00:00",
}


# --------------------------------------------------------------------------- #
# chat.create
# --------------------------------------------------------------------------- #
def test_create_sends_messages_and_parses_output(api, home):
    api.json("POST", "/api/v1/chat", COMPLETION)
    client = make_sync_client(api)
    try:
        completion = client.chat.create(
            model="tfmf",
            messages=[
                {"role": "user", "content": "What is a factorial?"},
                {"role": "assistant", "content": "A product."},
                ChatMessage(role="user", content="Thanks"),
            ],
        )
    finally:
        client.close()

    assert str(api.request.url) == "https://api.test.local/api/v1/chat"
    assert api.body == {
        "model": "tfmf",
        "messages": [
            {"role": "user", "content": "What is a factorial?"},
            {"role": "assistant", "content": "A product."},
            {"role": "user", "content": "Thanks"},
        ],
    }

    assert isinstance(completion, ChatCompletion)
    assert completion.id == "chat_abc123"
    assert completion.object == "chat.completion"
    assert completion.model == "tfmf"
    assert completion.output == {"role": "assistant", "content": "A factorial is a product of 1..n."}
    assert completion.content == "A factorial is a product of 1..n."
    assert completion.created_at == "2026-09-30T15:04:05+00:00"
    assert isinstance(completion.usage, Usage)
    assert completion.usage.input_tokens == 12
    assert completion.usage.output_tokens == 40
    assert completion.usage.cost_cny == pytest.approx(0.000126)
    assert completion.usage.estimated is True
    assert completion.raw == COMPLETION
    assert "ChatCompletion" in repr(completion)


def test_create_accepts_tuples_and_defaults_the_role(api, home):
    api.json("POST", "/api/v1/chat", COMPLETION)
    client = make_sync_client(api)
    try:
        client.chat.create(model="tfmf", messages=[("user", "hi"), {"content": "again"}])
    finally:
        client.close()
    assert api.body["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "again"},
    ]


def test_create_with_assistant_id_only(api, home):
    api.json("POST", "/api/v1/chat", COMPLETION)
    client = make_sync_client(api)
    try:
        client.chat.create(
            assistant_id="asst_abc123",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.3,
            max_output_tokens=256,
        )
    finally:
        client.close()
    assert api.body == {
        "assistant_id": "asst_abc123",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.3,
        "max_output_tokens": 256,
    }


def test_create_requires_model_or_assistant(api, home):
    client = make_sync_client(api)
    try:
        with pytest.raises(ValueError):
            client.chat.create(messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(ValueError):
            client.chat.create(model="tfmf", messages=[])
    finally:
        client.close()
    assert api.requests == []


def test_rejects_unknown_message_shapes(api, home):
    client = make_sync_client(api)
    try:
        with pytest.raises(TypeError):
            client.chat.create(model="tfmf", messages=[object()])
        with pytest.raises(ValueError):
            client.chat.create(model="tfmf", messages=[{"role": "user"}])
    finally:
        client.close()
    assert api.requests == []


# --------------------------------------------------------------------------- #
# models / usage
# --------------------------------------------------------------------------- #
def test_models_list(api, home):
    api.json(
        "GET",
        "/api/v1/models",
        {
            "object": "list",
            "data": [
                {
                    "id": "tfmf",
                    "object": "model",
                    "name": "TFMF",
                    "live": True,
                    "context_window": 8192,
                    "input_cny_per_1m": 0.5,
                    "output_cny_per_1m": 3.0,
                }
            ],
        },
    )
    client = make_sync_client(api)
    try:
        page = client.models.list()
    finally:
        client.close()

    assert str(api.request.url) == "https://api.test.local/api/v1/models"
    assert isinstance(page, ModelsPage)
    assert isinstance(page.data[0], ModelInfo)
    assert page.data[0].id == "tfmf"
    assert page.data[0].name == "TFMF"
    assert page.data[0].live is True
    assert page.data[0].context_window == 8192
    assert page.data[0].input_cny_per_1m == pytest.approx(0.5)
    assert page.data[0].output_cny_per_1m == pytest.approx(3.0)


def test_usage_retrieve(api, home):
    api.json(
        "GET",
        "/api/v1/usage",
        {
            "object": "usage",
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "total_cost_cny": 0.002,
            "requests": 12,
        },
    )
    client = make_sync_client(api)
    try:
        summary = client.usage.retrieve()
    finally:
        client.close()

    assert str(api.request.url) == "https://api.test.local/api/v1/usage"
    assert isinstance(summary, UsageSummary)
    assert summary.object == "usage"
    assert summary.total_input_tokens == 1000
    assert summary.total_output_tokens == 500
    assert summary.total_cost_cny == pytest.approx(0.002)
    assert summary.requests == 12
    assert summary.raw["requests"] == 12
