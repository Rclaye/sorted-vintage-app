"""The demo listings: fixed, so a re-run asks for the same 50 and only creates what is missing.

Each title ends in its number ("... #07"), which is how the seed job recognises what it already
published for a seller.
"""

from dataclasses import dataclass

ITEMS = [
    ("tops", "Levi's", "Western shirt", "M"),
    ("outerwear", "Carhartt", "Chore coat", "L"),
    ("bottoms", "Wrangler", "Straight jeans", "32"),
    ("dresses", "Laura Ashley", "Floral midi dress", "10"),
    ("shoes", "Dr. Martens", "1460 boots", "9"),
    ("accessories", "Hermes", "Silk scarf", "OS"),
    ("tops", "Champion", "Reverse weave sweatshirt", "L"),
    ("outerwear", "Barbour", "Waxed jacket", "M"),
    ("bottoms", "Dickies", "874 work pants", "34"),
    ("tops", "Ralph Lauren", "Oxford shirt", "S"),
]
CONDITIONS = ["good", "like_new", "fair", "new_with_tags"]


@dataclass(frozen=True)
class Listing:
    number: int
    title: str
    brand: str
    category: str
    size: str
    condition: str
    price: str  # dollars, as the BFF form takes it
    description: str


def listing(number: int) -> Listing:
    category, brand, item, size = ITEMS[(number - 1) % len(ITEMS)]
    return Listing(
        number=number,
        title=f"{brand} {item} #{number:02d}",
        brand=brand,
        category=category,
        size=size,
        condition=CONDITIONS[number % len(CONDITIONS)],
        price=f"{12 + (number * 7) % 90}.00",
        description=f"Demo listing {number} from the seed job (dev only).",
    )


def numbers_for(seller_index: int, sellers: int, total: int) -> list[int]:
    """Deal 1..total round-robin over the sellers: seller 0 gets 1, 3, 5, ... for two sellers."""
    return [n for n in range(1, total + 1) if (n - 1) % sellers == seller_index]


def number_of(title: str) -> int | None:
    head, sep, tail = title.rpartition(" #")
    return int(tail) if sep and head and tail.isdigit() else None
