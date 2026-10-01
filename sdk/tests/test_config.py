"""Base URL / API key resolution and endpoint-discovery caching (contract §9-§10)."""

from __future__ import annotations

import json
import os
import time

import pytest

from tai_sdk import TAI, errors
from tai_sdk._config import (
    DEFAULT_BASE_URL,
    DEFAULT_DISCOVERY_URL,
    ENDPOINT_CACHE_TTL,
    read_endpoint_cache,
    resolve_api_key,
    resolve_base_url,
    write_endpoint_cache,
)

from conftest import BASE_URL, FakeDiscoveryClient, make_sync_client

DISCOVERY_URL = "https://discovery.test/api-endpoint.json"
DISCOVERED = "https://tunnel.ngrok-free.app"


def write_config(home: str, base_url: str) -> None:
    directory = os.path.join(home, ".tai")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"base_url": base_url}, handle)


# --------------------------------------------------------------------------- #
# Resolution order
# --------------------------------------------------------------------------- #
def test_argument_wins_over_everything(home, monkeypatch):
    monkeypatch.setenv("TAI_BASE_URL", "https://from-env.test")
    write_config(home, "https://from-config.test")
    resolved, source = resolve_base_url("https://from-argument.test", discover=False)
    assert (resolved, source) == ("https://from-argument.test", "argument")


def test_env_wins_over_config(home, monkeypatch):
    monkeypatch.setenv("TAI_BASE_URL", "https://from-env.test")
    write_config(home, "https://from-config.test")
    assert resolve_base_url(discover=False) == ("https://from-env.test", "env")


def test_config_file_used_when_no_argument_or_env(home):
    write_config(home, "https://from-config.test/")
    assert resolve_base_url(discover=False) == ("https://from-config.test", "config")


def test_default_when_everything_missing(home):
    assert resolve_base_url(discover=False) == (DEFAULT_BASE_URL, "default")


def test_invalid_values_are_skipped(home, monkeypatch):
    # Not a URL, so it must be ignored rather than become the base URL.
    monkeypatch.setenv("TAI_BASE_URL", "not-a-url")
    write_config(home, "also-not-a-url")
    assert resolve_base_url(discover=False) == (DEFAULT_BASE_URL, "default")


def test_discover_false_skips_the_document(home, monkeypatch):
    fake = FakeDiscoveryClient({"base_url": DISCOVERED})
    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)
    assert resolve_base_url(discover=False, http_client=fake) == (DEFAULT_BASE_URL, "default")
    assert fake.calls == []


def test_discovery_env_var_switch(home, monkeypatch):
    fake = FakeDiscoveryClient({"base_url": DISCOVERED})
    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)
    monkeypatch.setenv("TAI_DISCOVERY", "0")
    assert resolve_base_url(discover=True, http_client=fake) == (DEFAULT_BASE_URL, "default")
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Discovery + caching
# --------------------------------------------------------------------------- #
def test_discovery_is_used_and_cached(home, monkeypatch):
    fake = FakeDiscoveryClient(
        {"object": "endpoint", "base_url": DISCOVERED, "updated_at": "2026-09-30T15:04:05+00:00"}
    )
    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)

    assert resolve_base_url(http_client=fake) == (DISCOVERED, "discovery")
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == DISCOVERY_URL

    cache_file = os.path.join(home, ".tai", "endpoint-cache.json")
    assert os.path.exists(cache_file)
    with open(cache_file, encoding="utf-8") as handle:
        cached = json.load(handle)
    assert cached["base_url"] == DISCOVERED
    assert cached["source"] == DISCOVERY_URL
    assert cached["updated_at"] == "2026-09-30T15:04:05+00:00"
    assert cached["cached_at"] == pytest.approx(time.time(), abs=30)

    # A second client construction must reuse the cache and never touch Pages.
    assert resolve_base_url(http_client=fake) == (DISCOVERED, "discovery")
    assert len(fake.calls) == 1


def test_cached_discovery_is_used_without_any_http(home, monkeypatch):
    write_endpoint_cache(DISCOVERED, source=DISCOVERY_URL)

    class Exploding:
        def get(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("discovery document should not be fetched")

        def close(self):  # pragma: no cover - defensive
            pass

    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)
    assert resolve_base_url(http_client=Exploding()) == (DISCOVERED, "discovery")


def test_expired_cache_triggers_a_refetch(home, monkeypatch):
    stale = time.time() - ENDPOINT_CACHE_TTL - 60
    write_endpoint_cache("https://old-tunnel.ngrok-free.app", now=stale)
    fake = FakeDiscoveryClient({"base_url": DISCOVERED})
    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)

    assert read_endpoint_cache() is None
    assert resolve_base_url(http_client=fake) == (DISCOVERED, "discovery")
    assert len(fake.calls) == 1
    assert read_endpoint_cache() == DISCOVERED


def test_discovery_failure_falls_through_to_default(home, monkeypatch):
    failing = FakeDiscoveryClient({"code": "not_found"}, status_code=404)
    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)
    assert resolve_base_url(http_client=failing) == (DEFAULT_BASE_URL, "default")
    assert not os.path.exists(os.path.join(home, ".tai", "endpoint-cache.json"))


def test_corrupt_cache_is_ignored(home, monkeypatch):
    directory = os.path.join(home, ".tai")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "endpoint-cache.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    fake = FakeDiscoveryClient({"base_url": DISCOVERED})
    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)
    assert resolve_base_url(http_client=fake) == (DISCOVERED, "discovery")


def test_unwritable_cache_is_not_fatal(home, monkeypatch):
    # Point the cache at a path whose parent cannot be created (a file, not a dir).
    blocker = os.path.join(home, ".tai")
    with open(blocker, "w", encoding="utf-8") as handle:
        handle.write("not a directory")
    fake = FakeDiscoveryClient({"base_url": DISCOVERED})
    monkeypatch.setenv("TAI_DISCOVERY_URL", DISCOVERY_URL)
    assert resolve_base_url(http_client=fake) == (DISCOVERED, "discovery")


def test_default_discovery_url_constant():
    assert DEFAULT_DISCOVERY_URL == "https://ltyleo.github.io/platform/api-endpoint.json"


# --------------------------------------------------------------------------- #
# API key resolution
# --------------------------------------------------------------------------- #
def test_api_key_from_argument_then_env(home, monkeypatch):
    monkeypatch.setenv("TAI_API_KEY", "sk-tai-env")
    assert resolve_api_key("sk-tai-arg") == "sk-tai-arg"
    assert resolve_api_key() == "sk-tai-env"
    monkeypatch.delenv("TAI_API_KEY")
    assert resolve_api_key() is None


def test_client_exposes_resolution_source(home, monkeypatch):
    monkeypatch.setenv("TAI_BASE_URL", "https://from-env.test")
    monkeypatch.setenv("TAI_API_KEY", "sk-tai-env")
    client = TAI(discover=False)
    try:
        assert client.base_url == "https://from-env.test"
        assert client.base_url_source == "env"
        assert client.api_key == "sk-tai-env"
        assert client.max_retries == 2
    finally:
        client.close()


def test_missing_api_key_is_a_clear_client_side_error(api, home):
    client = TAI(base_url=BASE_URL, discover=False, transport=api.transport)
    try:
        with pytest.raises(errors.TAIError) as excinfo:
            client.models.list()
        assert "TAI_API_KEY" in str(excinfo.value)
        assert api.requests == []
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# End-to-end construction against a mock transport
# --------------------------------------------------------------------------- #
def test_client_uses_cached_discovered_base_url(api, home):
    """A warm discovery cache makes TAI() resolve to the tunnel with no page fetch."""
    write_endpoint_cache(DISCOVERED, source=DISCOVERY_URL)
    api.json("GET", "/api/v1/models", {"object": "list", "data": []})

    client = TAI(api_key="sk-tai-test", transport=api.transport)
    try:
        assert client.base_url == DISCOVERED
        assert client.base_url_source == "discovery"
        client.models.list()
        assert str(api.request.url) == DISCOVERED + "/api/v1/models"
    finally:
        client.close()


def test_base_url_has_no_trailing_slash(api, home):
    client = make_sync_client(api, base_url=BASE_URL + "/")
    try:
        assert client.base_url == BASE_URL
    finally:
        client.close()


def test_ngrok_header_on_every_request(api, home):
    api.json("GET", "/api/v1/models", {"object": "list", "data": []})
    client = make_sync_client(api)
    try:
        client.models.list()
        client.models.list()
    finally:
        client.close()
    assert len(api.requests) == 2
    for request in api.requests:
        assert request.headers["ngrok-skip-browser-warning"] == "true"


def test_closed_client_refuses_work(api, home):
    client = make_sync_client(api)
    client.close()
    client.close()  # idempotent
    assert client.is_closed
    with pytest.raises(RuntimeError):
        client.models.list()
