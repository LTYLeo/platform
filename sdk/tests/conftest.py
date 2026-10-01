"""Shared fixtures.

Every test runs with an isolated ``HOME`` so that ``~/.tai/config.json`` and
``~/.tai/endpoint-cache.json`` are freshly created temp files, and with network
discovery disabled unless the test opts in. No test needs a live server: all
HTTP goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

# src/ layout: import the package straight from the checkout, no install needed.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tai_sdk import TAI, AsyncTAI  # noqa: E402

BASE_URL = "https://api.test.local"

#: Every request made through a mock client, for header/URL assertions.
RecordedRequest = Tuple[str, str, Dict[str, str], Any]


class FakeDiscoveryClient:
    """Stand-in for ``httpx.Client`` used only by the discovery document fetch."""

    def __init__(self, document: Any, status_code: int = 200, body: Optional[str] = None) -> None:
        self.document = document
        self.status_code = status_code
        self.body = body
        self.calls: List[Tuple[str, dict]] = []
        self.closed = False

    def get(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        if self.body is not None:
            return httpx.Response(
                self.status_code,
                content=self.body.encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(self.status_code, json=self.document)

    def close(self) -> None:
        self.closed = True


class MockAPI:
    """Recording httpx.MockTransport handler with a routing table.

    Routes are matched on ``(METHOD, path)``; the handler receives the decoded
    JSON body (or None) plus the request itself.
    """

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Callable[..., httpx.Response]] = {}
        self.requests: List[httpx.Request] = []
        self.bodies: List[Any] = []

    def route(self, method: str, path: str, handler: Callable[..., httpx.Response]) -> None:
        self.routes[(method.upper(), path)] = handler

    def json(self, method: str, path: str, payload: Any, status_code: int = 200) -> None:
        self.route(
            method,
            path,
            lambda *args, **kwargs: httpx.Response(status_code, json=payload),
        )

    def error(
        self,
        method: str,
        path: str,
        status_code: int,
        code: str,
        message: str = "boom",
        param: Optional[str] = None,
    ) -> None:
        payload = {"code": code, "message": message}
        if param:
            payload["param"] = param
        self.json(method, path, payload, status_code=status_code)

    def sse(self, method: str, path: str, chunks: List[bytes], status_code: int = 200) -> None:
        def handler(*args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                status_code,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(b"".join(chunks)),
            )

        self.route(method, path, handler)

    # -- httpx integration -------------------------------------------------- #
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:  # pragma: no cover - defensive
            body = request.content
        self.bodies.append(body)
        key = (request.method.upper(), request.url.path)
        if key not in self.routes:
            return httpx.Response(
                404,
                json={"code": "not_found", "message": "no route for %s %s" % key},
            )
        return self.routes[key](body, request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def request(self) -> httpx.Request:
        assert self.requests, "no request was recorded"
        return self.requests[-1]

    @property
    def body(self) -> Any:
        assert self.bodies, "no request was recorded"
        return self.bodies[-1]

    @property
    def query(self) -> Dict[str, str]:
        return dict(self.request.url.params)


@pytest.fixture
def home(tmp_path, monkeypatch) -> str:
    """Isolated ``$HOME`` plus a clean TAI environment."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    for name in (
        "TAI_API_KEY",
        "TAI_BASE_URL",
        "TAI_DISCOVERY_URL",
        "TAI_DISCOVERY",
    ):
        monkeypatch.delenv(name, raising=False)
    return str(fake_home)


@pytest.fixture
def dot_tai(home) -> str:
    """The isolated ``~/.tai`` directory."""
    return os.path.join(home, ".tai")


@pytest.fixture
def api() -> MockAPI:
    return MockAPI()


def make_sync_client(api: MockAPI, **kwargs: Any) -> TAI:
    kwargs.setdefault("api_key", "sk-tai-test")
    kwargs.setdefault("base_url", BASE_URL)
    kwargs.setdefault("discover", False)
    kwargs.setdefault("transport", api.transport)
    return TAI(**kwargs)


def make_async_client(api: MockAPI, **kwargs: Any) -> AsyncTAI:
    kwargs.setdefault("api_key", "sk-tai-test")
    kwargs.setdefault("base_url", BASE_URL)
    kwargs.setdefault("discover", False)
    kwargs.setdefault("transport", api.transport)
    return AsyncTAI(**kwargs)


@pytest.fixture
def client(api: MockAPI, home) -> TAI:
    return make_sync_client(api)


@pytest.fixture
async def async_client(api: MockAPI, home):
    client = make_async_client(api)
    try:
        yield client
    finally:
        await client.aclose()
