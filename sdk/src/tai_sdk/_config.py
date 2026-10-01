"""Base URL + API key resolution, endpoint discovery and caching.

Resolution order for the base URL (contract §9):

1. the ``base_url=`` argument to ``TAI(...)`` / ``AsyncTAI(...)``
2. the ``TAI_BASE_URL`` environment variable
3. ``~/.tai/config.json`` — ``{"base_url": "..."}``
4. the discovery document (contract §10), cached 12 h in
   ``~/.tai/endpoint-cache.json``
5. ``https://api.tai-research.dev``

Step 4 is skipped entirely with ``discover=False``. The document URL defaults to
``https://ltyleo.github.io/platform/api-endpoint.json`` and can be overridden
with ``TAI_DISCOVERY_URL``.

API key resolution: the ``api_key=`` argument, then ``TAI_API_KEY``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from .errors import TAIError

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_DISCOVERY_URL",
    "DISCOVERY_TIMEOUT",
    "ENDPOINT_CACHE_TTL",
    "ClientConfig",
    "resolve_api_key",
    "resolve_base_url",
    "fetch_discovery_document",
    "config_dir",
    "config_path",
    "endpoint_cache_path",
    "read_config_file",
    "read_endpoint_cache",
    "write_endpoint_cache",
    "discover_base_url",
    "discovery_enabled",
]

#: Last-resort base URL when nothing else resolves (contract §9.5).
DEFAULT_BASE_URL = "https://api.tai-research.dev"

#: GitHub Pages discovery document (contract §10).
DEFAULT_DISCOVERY_URL = "https://ltyleo.github.io/platform/api-endpoint.json"

#: Discovery is best-effort: a 3 s timeout, failures are non-fatal (contract §10).
DISCOVERY_TIMEOUT = 3.0

#: Cache lifetime for a discovered endpoint (contract §10).
ENDPOINT_CACHE_TTL = 12 * 60 * 60


def config_dir() -> Path:
    """``~/.tai`` (honours ``$HOME``, so tests can monkeypatch it)."""
    return Path(os.path.expanduser("~")) / ".tai"


def config_path() -> Path:
    """``~/.tai/config.json``."""
    return config_dir() / "config.json"


def endpoint_cache_path() -> Path:
    """``~/.tai/endpoint-cache.json``."""
    return config_dir() / "endpoint-cache.json"


def discovery_enabled() -> bool:
    """False when ``TAI_DISCOVERY`` is set to a falsy literal."""
    value = os.environ.get("TAI_DISCOVERY")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _load_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _ensure_config_dir() -> bool:
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _write_json(path: Path, payload: Any) -> bool:
    if not _ensure_config_dir():
        return False
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _valid_base_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip("/")
    if not candidate:
        return None
    if "://" not in candidate:
        return None
    return candidate


def _env_base_url() -> Optional[str]:
    return _valid_base_url(os.environ.get("TAI_BASE_URL"))


def read_config_file(path: Optional[Path] = None) -> Optional[str]:
    """Return ``base_url`` from ``~/.tai/config.json`` if present."""
    payload = _load_json(path or config_path())
    if isinstance(payload, dict):
        return _valid_base_url(payload.get("base_url"))
    return None


def read_endpoint_cache(
    path: Optional[Path] = None,
    *,
    ttl: float = ENDPOINT_CACHE_TTL,
    now: Optional[float] = None,
) -> Optional[str]:
    """Return the cached discovered base URL, or None when missing/expired."""
    payload = _load_json(path or endpoint_cache_path())
    if not isinstance(payload, dict):
        return None
    base_url = _valid_base_url(payload.get("base_url"))
    if not base_url:
        return None
    cached_at = payload.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        # A hand-written cache file without a timestamp is treated as expired.
        return None
    current = time.time() if now is None else now
    if current - float(cached_at) > ttl:
        return None
    return base_url


def write_endpoint_cache(
    base_url: str,
    *,
    path: Optional[Path] = None,
    source: Optional[str] = None,
    now: Optional[float] = None,
    extra: Optional[dict] = None,
) -> bool:
    """Persist a discovered base URL for :data:`ENDPOINT_CACHE_TTL` seconds."""
    payload = {
        "base_url": base_url,
        "cached_at": time.time() if now is None else now,
    }
    if source:
        payload["source"] = source
    if extra:
        payload.update(extra)
    return _write_json(path or endpoint_cache_path(), payload)


def resolve_api_key(api_key: Optional[str] = None) -> Optional[str]:
    """``api_key=`` argument, then ``TAI_API_KEY`` (contract §9)."""
    if api_key:
        return api_key
    value = os.environ.get("TAI_API_KEY")
    if value:
        return value
    return None


def fetch_discovery_document(
    url: str,
    *,
    timeout: float = DISCOVERY_TIMEOUT,
    http_client: Any = None,
) -> Optional[dict]:
    """GET the discovery document. Any failure returns ``None`` (non-fatal)."""
    owns_client = http_client is None
    if owns_client:
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx is a hard dependency
            return None
        http_client = httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = http_client.get(url)
    except Exception:
        return None
    finally:
        if owns_client:
            try:
                http_client.close()
            except Exception:  # pragma: no cover - defensive
                pass
    try:
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def discover_base_url(
    discovery_url: Optional[str] = None,
    *,
    timeout: float = DISCOVERY_TIMEOUT,
    http_client: Any = None,
    cache_path: Optional[Path] = None,
    ttl: float = ENDPOINT_CACHE_TTL,
    use_cache: bool = True,
    now: Optional[float] = None,
) -> Optional[str]:
    """Resolve the base URL from the discovery document, using its cache.

    Returns ``None`` when discovery is unavailable — the caller then falls
    through to :data:`DEFAULT_BASE_URL`.
    """
    if use_cache:
        cached = read_endpoint_cache(cache_path, ttl=ttl, now=now)
        if cached:
            return cached

    url = discovery_url or os.environ.get("TAI_DISCOVERY_URL") or DEFAULT_DISCOVERY_URL
    document = fetch_discovery_document(url, timeout=timeout, http_client=http_client)
    if not document:
        return None
    base_url = _valid_base_url(document.get("base_url"))
    if not base_url:
        return None
    if use_cache:
        write_endpoint_cache(
            base_url,
            path=cache_path,
            source=url,
            now=now,
            extra={"updated_at": document.get("updated_at")},
        )
    return base_url


def resolve_base_url(
    base_url: Optional[str] = None,
    *,
    discover: bool = True,
    discovery_url: Optional[str] = None,
    discovery_timeout: float = DISCOVERY_TIMEOUT,
    http_client: Any = None,
    cache_path: Optional[Path] = None,
    cache_ttl: float = ENDPOINT_CACHE_TTL,
    use_cache: bool = True,
    now: Optional[float] = None,
) -> Tuple[str, str]:
    """Resolve the base URL.

    Returns ``(base_url, source)`` where source is one of ``"argument"``,
    ``"env"``, ``"config"``, ``"discovery"``, ``"default"``.
    """
    from_argument = _valid_base_url(base_url)
    if from_argument:
        return from_argument, "argument"

    from_env = _env_base_url()
    if from_env:
        return from_env, "env"

    from_config = read_config_file()
    if from_config:
        return from_config, "config"

    if discover and discovery_enabled():
        discovered = discover_base_url(
            discovery_url,
            timeout=discovery_timeout,
            http_client=http_client,
            cache_path=cache_path,
            ttl=cache_ttl,
            use_cache=use_cache,
            now=now,
        )
        if discovered:
            return discovered, "discovery"

    return DEFAULT_BASE_URL, "default"


@dataclass
class ClientConfig:
    """Resolved client settings: base URL, its source, API key and transport knobs."""

    base_url: str
    base_url_source: str = "default"
    api_key: Optional[str] = None
    timeout: Any = 30.0
    max_retries: int = 2
    default_headers: Optional[dict] = None
    discover: bool = True

    def require_api_key(self) -> str:
        """Return the API key or raise a clear error before hitting the network."""
        if not self.api_key:
            raise TAIError(
                "No API key was provided. Pass api_key=\"sk-tai-...\" to TAI(), "
                "or set the TAI_API_KEY environment variable."
            )
        return self.api_key
