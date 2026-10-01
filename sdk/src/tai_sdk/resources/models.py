"""``client.models`` — the live model catalogue (contract §7)."""

from __future__ import annotations

from ..errors import ModelNotFoundError
from ..types import ModelInfo, ModelsPage
from ._base import Resource, awaited

__all__ = ["Models"]

_PATH = "/api/v1/models"


class Models(Resource):
    """Read-only list of live models, with pricing and context window."""

    def list(self) -> ModelsPage:
        """``GET /api/v1/models``."""
        payload = self._transport.request("GET", _PATH)
        return awaited(payload, lambda value: ModelsPage.parse(value or {}))

    def retrieve(self, model_id: str) -> ModelInfo:
        """Return one model by id.

        v1 exposes no single-model endpoint, so this filters :meth:`list` and
        raises :class:`~tai_sdk.errors.ModelNotFoundError` for an unknown id —
        the same "return the object or raise" behaviour as every other
        ``retrieve`` in the SDK.
        """
        def pick(page: ModelsPage) -> ModelInfo:
            for item in page.data:
                if item.id == model_id:
                    return item
            raise ModelNotFoundError(
                f"No such model: {model_id}", param="model"
            )

        return awaited(self.list(), pick)

