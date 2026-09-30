"""Model catalogue and pricing.

Prices are CNY per **1,000,000 tokens** and this module is the single source of
truth for cost. The front-end has its own copy for the public pricing table and
the token calculator, but anything that touches money is computed here.

Keep in sync with pricing.html when the catalogue changes.
"""

from __future__ import annotations

#: model id -> (display name, input price, output price, live?)
MODELS: dict[str, dict] = {
    "gtc-2.5-mini": {
        "name": "GTC-2.5 mini",
        "input": 0.5,
        "output": 1.0,
        "live": True,
    },
    "tfmf": {
        "name": "TFMF",
        "input": 0.5,
        "output": 3.0,
        "live": True,
    },
    "giggle": {
        "name": "Giggle",
        "input": 1.0,
        "output": 5.0,
        "live": False,
    },
    "bro": {
        "name": "Bro",
        "input": 1.0,
        "output": 5.0,
        "live": False,
    },
    "sweetie": {
        "name": "Sweetie",
        "input": 1.0,
        "output": 5.0,
        "live": False,
    },
}

#: Tokens every new account gets for free before anything is billable.
FREE_TOKENS = 5_000


def is_live(model: str) -> bool:
    entry = MODELS.get(model)
    return bool(entry and entry["live"])


def cost_cny(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost in CNY for one call. Unknown models cost nothing rather than crashing."""
    entry = MODELS.get(model)
    if not entry:
        return 0.0
    return (
        input_tokens / 1_000_000 * entry["input"]
        + output_tokens / 1_000_000 * entry["output"]
    )
