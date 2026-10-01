"""Regression tests for two defects found while integrating the SDK against a
live server. Both were real, both were silent, and neither was covered before.

1. Abandoning a stream (``break`` out of the loop) left the generator to be
   closed by the garbage collector. The teardown then ran ``httpx``'s client
   close while the interpreter was finalising, which **segfaulted the whole
   process** (CPython 3.13: ``Response.close`` -> ``Client.close`` inside GC).
   The close now goes through ``_streaming._close``, which is a no-op once
   ``sys.is_finalizing()`` is true.

2. ``client.models.retrieve(id)`` returned a one-element ``ModelsPage`` instead
   of the model, so ``.name`` blew up. It now returns a ``ModelInfo`` or raises
   ``ModelNotFoundError``, matching every other ``retrieve`` in the SDK.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tai_sdk import TAI, errors
from tai_sdk.types import MessageDelta, MessageDone, ModelInfo


# --------------------------------------------------------------------------- #
# 1. stream teardown must never touch httpx during interpreter finalisation
# --------------------------------------------------------------------------- #

def test_close_is_a_noop_while_finalizing(monkeypatch):
    """``_close`` must not call into httpx once the interpreter is shutting down."""
    from tai_sdk import _streaming

    calls = []

    class FakeResponse:
        def close(self):
            calls.append("closed")

    monkeypatch.setattr(_streaming.sys, "is_finalizing", lambda: True)
    _streaming._close(FakeResponse())
    assert calls == [], "closing during finalisation is what crashed the process"

    # ...but it must still close normally at any other time.
    monkeypatch.setattr(_streaming.sys, "is_finalizing", lambda: False)
    _streaming._close(FakeResponse())
    assert calls == ["closed"]


def test_sync_events_closes_through_the_guard(monkeypatch):
    """The abandoned-generator path must use ``_close``, not ``response.close()``."""
    from tai_sdk import _streaming

    guarded = []

    class FakeResponse:
        def close(self):                       # pragma: no cover - must not run
            raise AssertionError("sync_events closed the response directly")

        def iter_bytes(self):
            yield b'event: message.delta\ndata: {"delta": "a"}\n\n'

    monkeypatch.setattr(_streaming, "_close", lambda r: guarded.append(r))

    gen = _streaming.sync_events(FakeResponse())
    next(gen)
    gen.close()                                # simulates the GC closing it

    # Both layers (sync_events and iter_stream) close defensively; what matters is
    # that every close went through the guard and none called the response directly.
    assert len(guarded) >= 1, "teardown did not route through the guarded helper"


@pytest.mark.parametrize("abandon_after", [0, 1, 2])
def test_abandoning_a_stream_exits_cleanly(abandon_after, tmp_path):
    """End-to-end: breaking out of a stream must not crash on interpreter exit.

    Runs in a subprocess because the original failure only appeared while the
    interpreter was being torn down, which cannot be observed in-process.
    """
    script = textwrap.dedent(
        f"""
        import httpx
        from tai_sdk import TAI
        from tai_sdk._streaming import sync_events

        # A real httpx response object driven by MockTransport, so the teardown
        # path is exercised exactly as it is in production.
        body = b'event: message.start\\ndata: {{"id": "msg_1", "model": "tfmf"}}\\n\\n'
        body += b'event: message.delta\\ndata: {{"delta": "hi"}}\\n\\n'
        body += b'event: message.delta\\ndata: {{"delta": " there"}}\\n\\n'

        def handler(request):
            return httpx.Response(200, headers={{"content-type": "text/event-stream"}}, content=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        response = client.send(client.build_request("GET", "http://x/stream"), stream=True)

        n = 0
        for _ in sync_events(response):
            n += 1
            if n > {abandon_after}:
                break
        # deliberately leave the client and response unreferenced here
        """
    )
    path = tmp_path / "abandon.py"
    path.write_text(script, encoding="utf-8")

    # The suite runs from the checkout via a sys.path shim, so the child needs
    # the same path rather than an installed distribution.
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src) + os.pathsep + os.environ.get("PYTHONPATH", "")}

    result = subprocess.run(
        [sys.executable, str(path)], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, (
        f"exited with {result.returncode} (negative means a signal, e.g. -11 = SIGSEGV)\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


# --------------------------------------------------------------------------- #
# 2. models.retrieve returns the model, or raises
# --------------------------------------------------------------------------- #

def _models_payload():
    return {
        "object": "list",
        "data": [
            {
                "id": "gtc-2.5-mini",
                "object": "model",
                "name": "GTC-2.5 mini",
                "live": True,
                "context_window": 8192,
                "input_cny_per_1m": 0.5,
                "output_cny_per_1m": 1.0,
            },
            {
                "id": "tfmf",
                "object": "model",
                "name": "TFMF",
                "live": True,
                "context_window": 8192,
                "input_cny_per_1m": 0.5,
                "output_cny_per_1m": 3.0,
            },
        ],
        "has_more": False,
        "first_id": "gtc-2.5-mini",
        "last_id": "tfmf",
    }


@pytest.fixture()
def client():
    import httpx

    def handler(request):
        assert request.url.path == "/api/v1/models"
        return httpx.Response(200, json=_models_payload())

    return TAI(
        api_key="sk-tai-test",
        base_url="http://test",
        discover=False,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_retrieve_returns_a_single_model(client):
    model = client.models.retrieve("tfmf")
    assert isinstance(model, ModelInfo)
    assert model.id == "tfmf"
    assert model.name == "TFMF"                      # the bug: this raised AttributeError
    assert model.input_cny_per_1m == 0.5
    assert model.output_cny_per_1m == 3.0


def test_retrieve_unknown_model_raises(client):
    with pytest.raises(errors.ModelNotFoundError) as excinfo:
        client.models.retrieve("gpt-9")
    assert excinfo.value.code == "model_not_found"
    assert excinfo.value.param == "model"


def test_list_still_returns_a_page(client):
    page = client.models.list()
    assert [m.id for m in page.data] == ["gtc-2.5-mini", "tfmf"]
