"""T068: money arithmetic and the ledger (research R7, FR-012).

All amounts are integer cents. The platform's fee is round_half_up(amount x fee_pct / 100) and
the seller's share is the remainder, so the two always add up to what the buyer paid. A sale is
one positive pair; a refund is the exact negative pair, so a refunded order sums to zero. The
unique (order_id, party, kind) makes writing either pair twice a no-op.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Context, Decimal

from fastapi import APIRouter, Request
from sqlalchemy import Connection, Engine, select
from sqlalchemy.dialects.postgresql import insert

from app.db import ledger_entry, payment_intent
from app.errors import ApiError

router = APIRouter()

Number = int | float | str | Decimal


@dataclass(frozen=True)
class Entry:
    party: str
    amount_cents: int


# Exact for any amount an order can have: 28 digits, and halves always round up (R7).
_MONEY = Context(prec=28, rounding=ROUND_HALF_UP)


def platform_fee(amount_cents: int, fee_pct: Number) -> int:
    product = _MONEY.multiply(Decimal(amount_cents), Decimal(str(fee_pct)))
    return int(_MONEY.quantize(_MONEY.divide(product, Decimal(100)), Decimal(1)))


def split(amount_cents: int, fee_pct: Number) -> tuple[int, int]:
    """(platform, seller): the seller gets the remainder, so there is never a rounding gap."""
    platform = platform_fee(amount_cents, fee_pct)
    return platform, amount_cents - platform


def sale_entries(amount_cents: int, fee_pct: Number) -> list[Entry]:
    platform, seller = split(amount_cents, fee_pct)
    return [Entry("platform", platform), Entry("seller", seller)]


def refund_entries(amount_cents: int, fee_pct: Number) -> list[Entry]:
    return [Entry(e.party, -e.amount_cents) for e in sale_entries(amount_cents, fee_pct)]


def write(
    conn: Connection, order_id: uuid.UUID, seller_sub: uuid.UUID, kind: str, entries: list[Entry]
) -> None:
    for entry in entries:
        conn.execute(
            insert(ledger_entry)
            .values(
                id=uuid.uuid4(),
                order_id=order_id,
                party=entry.party,
                account_sub=seller_sub if entry.party == "seller" else None,
                amount_cents=entry.amount_cents,
                kind=kind,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="ledger_entry_once")
        )


def record_sale(
    engine: Engine, *, order_id: uuid.UUID, seller_sub: uuid.UUID, amount_cents: int, fee_pct
) -> None:
    with engine.begin() as conn:
        write(conn, order_id, seller_sub, "sale", sale_entries(amount_cents, fee_pct))


def record_refund(
    engine: Engine, *, order_id: uuid.UUID, seller_sub: uuid.UUID, amount_cents: int, fee_pct
) -> None:
    """The sale and its reversal together: the money came in and went back."""
    with engine.begin() as conn:
        write(conn, order_id, seller_sub, "sale", sale_entries(amount_cents, fee_pct))
        write(conn, order_id, seller_sub, "refund", refund_entries(amount_cents, fee_pct))


def entries_for(engine: Engine, order_id: uuid.UUID) -> list:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(ledger_entry)
                .where(ledger_entry.c.order_id == order_id)
                .order_by(
                    ledger_entry.c.created_at, ledger_entry.c.kind.desc(), ledger_entry.c.party
                )
            )
        )


@router.get("/orders/{order_id}/ledger")
def get_ledger(order_id: uuid.UUID, request: Request) -> list[dict]:
    """For Orders (which checks its own buyer and seller before asking) and for the seller."""
    engine, settings = request.app.state.engine, request.app.state.settings
    claims = request.state.claims
    rows = entries_for(engine, order_id)
    if claims.get("azp") != settings.orders_azp:
        with engine.connect() as conn:
            seller = conn.execute(
                select(payment_intent.c.seller_sub).where(payment_intent.c.order_id == order_id)
            ).scalar_one_or_none()
        sellers = {str(seller)} if seller else set()
        sellers |= {str(r.account_sub) for r in rows if r.account_sub}
        if str(claims.get("sub")) not in sellers:
            raise ApiError(404, "not_found", "No such order.")
    return [
        {
            "party": r.party,
            "account_sub": str(r.account_sub) if r.account_sub else None,
            "amount_cents": r.amount_cents,
            "kind": r.kind,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
