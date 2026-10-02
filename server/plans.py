"""Plan definitions for the Sigma platform.

One place that says what each tier is worth. Sigma asks the platform for a
user's entitlements and enforces what it is told, rather than keeping a second
copy of the rules that can drift out of step with this one.

The shape follows the tiers people already know from OpenAI - Free, Plus, Pro -
with an internal Admin above them.

    Free    metered, the two cheapest models, no reasoning
    Plus    ¥5/month, a real working allowance, all but the largest model
    Pro     ¥25/month, no daily cap, every model, priority
    Admin   staff; never metered, never charged

``daily_limit`` of ``None`` means unlimited. ``models`` of ``None`` means every
model the host offers, so adding a model to the backend does not silently exclude
a paying tier.
"""

from __future__ import annotations

#: Cheapest first. Used to describe a tier without repeating the list.
MODEL_CHOICES = ("gtc25", "gtc25_400m", "gtc25v", "gtc25o", "tfmf")

#: On Sigma the same weights are sold under a product name; the developer
#: platform's API keeps the research name. One model, two audiences.
SIGMA_MODEL_NAMES = {"tfmf": "GTC-2.5 Turbo"}

#: Models that are still research rather than product. Reachable on Pro within
#: limits, and not below it. Kept separate from MODEL_CHOICES because "all models"
#: for Pro has to mean all of them, previews included.
FRONTIER_MODELS = ()

PLANS: dict[str, dict] = {
    "free": {
        "name": "Free",
        "tagline": "Try Sigma",
        "tagline_zh": "试试 Sigma",
        "price_cny": 0.0,
        "daily_limit": 20,
        # The two cheapest models only. This is the tier that costs us money on
        # every message, so it is the one that has to be bounded.
        "models": ["gtc25", "gtc25_400m"],
        "reasoning": False,
        "priority": False,
        # Conversation history is what makes a chat feel like a chat, and it is
        # also what makes each request cost more. Pro only.
        "context": False,
        # Written the way plan pages usually are: the heading sells the benefit,
        # the sentence says exactly what you get. Limits belong in the sentence,
        # not in the heading - nobody upgrades to "300 messages".
        "summary": "See how AI can help with everyday questions, drafts and small tasks.",
        "summary_zh": "看看 AI 如何帮你处理日常问题、草稿和小任务。",
    },
    "plus": {
        "name": "Plus",
        "tagline": "Your everyday assistant",
        "tagline_zh": "你的日常助手",
        "price_cny": 10.0,
        "daily_limit": 300,
        "models": [m for m in MODEL_CHOICES if m != "large"],
        # Reasoning is what separates the two paid tiers, so Plus does not get
        # it. It stays a Pro capability, and stays off unless asked for.
        "reasoning": False,
        "priority": False,
        "context": False,
        "summary": "For work you come back to daily. 300 messages a day, GTC-2.5 Turbo, and every model except the frontier previews.",
        "summary_zh": "适合每天都要用的人。每天 300 条消息，GTC-2.5 Turbo，以及除前沿预览外的全部模型。",
    },
    "pro": {
        "name": "Pro",
        "tagline": "Maximum power",
        "tagline_zh": "最强性能",
        "price_cny": 100.0,
        "daily_limit": None,          # unlimited
        "models": None,               # everything, including models added later
        "reasoning": True,
        "priority": True,
        "frontier": True,
        "context": True,
        "summary": "For people who rely on our most powerful models all day. No daily cap, conversation context, and limited access to frontier research previews.",
        "summary_zh": "适合全天依赖我们最强模型的人。每日不限量、支持多轮上下文，并可有限访问前沿研究预览。",
    },
    "admin": {
        "name": "Admin",
        "price_cny": 0.0,
        "daily_limit": None,
        "models": None,
        "reasoning": True,
        "priority": True,
        "frontier": True,
        "context": True,
        "summary": "Staff. Never metered and never charged.",
    },
}

PLAN_ORDER = ("free", "plus", "pro", "admin")

#: Billing purposes. The developer platform's API credit is separate from these,
#: because a Sigma subscription and API usage are two different things to sell.
SIGMA_PURPOSES = {"sigma_plus": "plus", "sigma_pro": "pro"}

#: What a plan costs per month, as a purchase purpose, for the order flow.
PURPOSE_BY_PLAN = {v: k for k, v in SIGMA_PURPOSES.items()}


def get(plan: str | None) -> dict:
    """The definition for a plan, falling back to free.

    Unknown values fall back rather than raising: a plan string arriving from an
    older client should not take the endpoint down, and settling on the most
    restrictive tier is the safe direction to be wrong in.
    """
    return PLANS.get((plan or "free").strip().lower(), PLANS["free"])


def is_paid(plan: str | None) -> bool:
    return get(plan)["price_cny"] > 0


def monthly_price(purpose: str) -> float:
    """The price for a purchase purpose, read from the tier it grants."""
    plan = SIGMA_PURPOSES.get(purpose)
    return float(PLANS[plan]["price_cny"]) if plan else 0.0


def public_catalog() -> list[dict]:
    """What the pricing page shows. Admin is internal and is left out."""
    catalog = []
    for name in PLAN_ORDER:
        if name == "admin":
            continue
        entry = dict(PLANS[name])
        entry["id"] = name
        entry["models"] = entry["models"] or list(MODEL_CHOICES)
        catalog.append(entry)
    return catalog


def enforce(plan: str | None) -> dict:
    """The limits a host should apply, in the shape its enforcement wants."""
    entry = get(plan)
    return {
        "plan": (plan or "free").lower(),
        "daily_limit": entry["daily_limit"],
        "models": list(entry["models"]) if entry["models"] else None,
        "reasoning": entry["reasoning"],
        "priority": entry["priority"],
        "context": entry.get("context", False),
        "frontier": entry.get("frontier", False),
    }
