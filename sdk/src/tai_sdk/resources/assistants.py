"""``client.assistants`` — crate / list / get / update / delete (contract §2)."""

from __future__ import annotations

from typing import Any, Optional

from ..types import Assistant, AssistantsPage
from ._base import Resource, awaited

__all__ = ["Assistants"]

_PATH = "/api/v1/assistants"

_UNSET = object()


class Assistants(Resource):
    """A reusable configuration: model + system instructions + a name."""

    def create(
        self,
        *,
        model: str,
        name: Optional[str] = None,
        instructions: Optional[str] = None,
        metadata: Optional[dict] = None,
        **extra: Any,
    ) -> Assistant:
        """``POST /api/v1/assistants``."""
        if not model:
            raise ValueError("model is required to create an assistant")
        body = self._clean(
            {
                "model": model,
                "name": name,
                "instructions": instructions,
                "metadata": self._validate_metadata(metadata),
            }
        )
        body.update(self._clean(extra))
        payload = self._transport.request("POST", _PATH, json_body=body)
        return awaited(payload, lambda value: Assistant.parse(value or {}))

    def list(self, *, limit: Optional[int] = None, after: Optional[str] = None) -> AssistantsPage:
        """``GET /api/v1/assistants?limit=&after=`` — newest first."""
        payload = self._transport.request(
            "GET", _PATH, params={"limit": limit, "after": after}
        )
        return awaited(payload, lambda value: AssistantsPage.parse(value or {}))

    def get(self, assistant_id: str) -> Assistant:
        """``GET /api/v1/assistants/{assistant_id}``."""
        if not assistant_id:
            raise ValueError("assistant_id is required")
        payload = self._transport.request("GET", "%s/%s" % (_PATH, assistant_id))
        return awaited(payload, lambda value: Assistant.parse(value or {}))

    def update(
        self,
        assistant_id: str,
        *,
        name: Any = _UNSET,
        model: Any = _UNSET,
        instructions: Any = _UNSET,
        metadata: Any = _UNSET,
        **extra: Any,
    ) -> Assistant:
        """``PATCH /api/v1/assistants/{assistant_id}`` with any subset of fields.

        Only the fields you pass are sent. Passing ``None`` explicitly sends a
        JSON ``null``; omitting a field leaves it untouched.
        """
        if not assistant_id:
            raise ValueError("assistant_id is required")
        body = self._patch_body(name=name, model=model, instructions=instructions, metadata=metadata)
        body.update(extra)
        if not body:
            raise ValueError("update() needs at least one of name, model, instructions, metadata")
        payload = self._transport.request(
            "PATCH", "%s/%s" % (_PATH, assistant_id), json_body=body
        )
        return awaited(payload, lambda value: Assistant.parse(value or {}))

    def delete(self, assistant_id: str) -> None:
        """``DELETE /api/v1/assistants/{assistant_id}`` -> 204."""
        if not assistant_id:
            raise ValueError("assistant_id is required")
        return awaited(self._transport.request("DELETE", "%s/%s" % (_PATH, assistant_id)))

    # -- helpers ------------------------------------------------------------ #
    def _patch_body(self, **fields: Any) -> dict:
        body = {}
        for key, value in fields.items():
            if value is _UNSET:
                continue
            if key == "metadata":
                value = self._validate_metadata(value)
            body[key] = value
        return body
