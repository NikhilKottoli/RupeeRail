"""Starting points for new agents, and the avatars the extension bundles.

Every avatar id has a file in extension/static/avatars/, and every colour id a class in extension/static/panel.css.
A test keeps them in step.
"""

from __future__ import annotations

AVATARS = ("chef", "shopper", "traveller", "gardener", "student", "doctor", "artist", "musician", "athlete", "mechanic", "scientist", "robot")
COLOURS = ("saffron", "coral", "rose", "violet", "indigo", "teal", "leaf", "slate")

TEMPLATES = (
    {
        "id": "chef",
        "name": "Chef",
        "description": "Knows what you eat, and orders food for you",
        "avatar": "chef",
        "colour": "saffron",
        "instructions": (
            "You order food for delivery for this person from food delivery sites. Only use delivery pages, never Dineout or "
            "table booking pages. Choose one meal that fits their request and "
            "everything you know about them. Their diet and allergies come first, then their tastes. Prefer well-rated "
            "places that can deliver now, and order a sensible amount for the number of people. Deliver to the saved "
            "address that matches their profile. Don't add drinks, sides, tips, donations or add-ons unless they asked. "
            "If nothing suitable fits the limit, use need_help. Never compromise on diet or allergies."
        ),
        "profile": "",
        "profile_hint": "Diet: \nAllergies: \nDelivery address: \nLikes: \nDislikes: ",
        "sites": ["swiggy.com", "zomato.com"],
        "start_url": "https://www.swiggy.com/",
        "per_order_max_inr": 300,
        "budget_usdc": "5",
    },
    {
        "id": "blank",
        "name": "",
        "description": "",
        "avatar": "robot",
        "colour": "indigo",
        "instructions": "",
        "profile": "",
        "profile_hint": "Anything this agent should always know about you",
        "sites": [],
        "start_url": "",
        "per_order_max_inr": 100,
        "budget_usdc": "2",
    },
)
