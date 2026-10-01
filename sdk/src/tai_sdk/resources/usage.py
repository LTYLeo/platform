"""``client.usage`` — account usage summary (contract §7)."""

from __future__ import annotations

from ..types import UsageSummary
from ._base import Resource, awaited

__all__ = ["Usage"]

_PATH = "/api/v1/usage"


class Usage(Resource):
    """The same payload the cookie-authenticated dashboard ``/api/usage`` returns."""

    def retrieve(self) -> UsageSummary:
        """``GET /api/v1/usage``."""
        payload = self._transport.request("GET", _PATH)
        return awaited(payload, lambda value: UsageSummary.parse(value or {}))
