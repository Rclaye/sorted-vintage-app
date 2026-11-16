"""Order rows, idempotency keys and the outbox (data-model.md, research R8).

Every state change is a conditional update from `pending` plus its outbox row, in one
transaction: an order's state and its event are never seen apart (FR-011). A change that finds
the order already final changes nothing and writes no event.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy import Connection, Engine, delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db import idempotency_key, orders, outbox_message, payment_result

FINAL = frozenset({"paid", "failed", "refunded"})
EVENT_FOR = {"paid": "order.paid", "failed": "order.failed", "refunded": "order.refunded"}

_PROPAGATOR = TraceContextTextMapPropagator()


def now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def as_json(row) -> dict:
    """The Order schema in contracts/orders.openapi.yaml."""
    return {
        "id": str(row.id),
        "listing_id": str(row.listing_id),
        "buyer_sub": str(row.buyer_sub),
        "seller_sub": str(row.seller_sub),
        "amount_cents": row.amount_cents,
        "status": row.status,
        "failure_reason": row.failure_reason,
        "reservation_expires_at": _iso(row.reservation_expires_at),
        "created_at": _iso(row.created_at),
        "paid_at": _iso(row.paid_at),
    }


def current_traceparent() -> str | None:
    """The W3C trace context of the request writing the outbox row (research R9)."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


# ---------------------------------------------------------------- reads


def find(engine: Engine, order_id: uuid.UUID):
    with engine.connect() as conn:
        return conn.execute(select(orders).where(orders.c.id == order_id)).first()


def for_buyer(engine: Engine, buyer_sub: uuid.UUID) -> list:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(orders)
                .where(orders.c.buyer_sub == buyer_sub)
                .order_by(orders.c.created_at.desc(), orders.c.id)
            )
        )


# ---------------------------------------------------------------- idempotency keys


def claim_key(engine: Engine, buyer_sub: uuid.UUID, key: str, order_id: uuid.UUID):
    """Stores the key for a new order. Returns None when this call owns it, else the earlier
    row (its order may not exist yet if that checkout is still running)."""
    with engine.begin() as conn:
        claimed = conn.execute(
            insert(idempotency_key)
            .values(buyer_sub=buyer_sub, key=key, order_id=order_id, created_at=now())
            .on_conflict_do_nothing(index_elements=["buyer_sub", "key"])
            .returning(idempotency_key.c.order_id)
        ).first()
        if claimed:
            return None
        return conn.execute(
            select(idempotency_key).where(
                idempotency_key.c.buyer_sub == buyer_sub, idempotency_key.c.key == key
            )
        ).one()


def forget_key(engine: Engine, buyer_sub: uuid.UUID, key: str) -> None:
    """After a refused checkout, so the same key may try again."""
    with engine.begin() as conn:
        conn.execute(
            delete(idempotency_key).where(
                idempotency_key.c.buyer_sub == buyer_sub, idempotency_key.c.key == key
            )
        )


# ---------------------------------------------------------------- writes


def insert_pending(
    engine: Engine,
    *,
    order_id: uuid.UUID,
    listing_id: uuid.UUID,
    buyer_sub: uuid.UUID,
    seller_sub: uuid.UUID,
    amount_cents: int,
    fee_pct: Decimal,
    reservation_id: uuid.UUID,
    reservation_expires_at: datetime,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            orders.insert().values(
                id=order_id,
                listing_id=listing_id,
                buyer_sub=buyer_sub,
                seller_sub=seller_sub,
                amount_cents=amount_cents,
                fee_pct=fee_pct,
                reservation_id=reservation_id,
                reservation_expires_at=reservation_expires_at,
                status="pending",
                created_at=now(),
            )
        )


def attach_intent(
    engine: Engine,
    order_id: uuid.UUID,
    buyer_sub: uuid.UUID,
    key: str,
    payment_intent_id: str,
    client_secret: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(orders)
            .where(orders.c.id == order_id)
            .values(payment_intent_id=payment_intent_id)
        )
        conn.execute(
            update(idempotency_key)
            .where(idempotency_key.c.buyer_sub == buyer_sub, idempotency_key.c.key == key)
            .values(client_secret=client_secret)
        )


def client_secret_for(engine: Engine, order_id: uuid.UUID) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            select(idempotency_key.c.client_secret).where(idempotency_key.c.order_id == order_id)
        ).scalar_one_or_none()


def mark_expired(engine: Engine, order_id: uuid.UUID) -> None:
    """T064: the hold is released and the PaymentIntent cancelled; the outcome comes later."""
    with engine.begin() as conn:
        conn.execute(
            update(orders)
            .where(orders.c.id == order_id, orders.c.status == "pending")
            .values(expired_at=now())
        )


def _event(conn: Connection, row, env: str) -> None:
    event_id = uuid.uuid4()
    event_type = EVENT_FOR[row.status]
    conn.execute(
        outbox_message.insert().values(
            id=event_id,
            event_type=event_type,
            payload={
                "event_id": str(event_id),
                "event_type": event_type,
                "occurred_at": now().isoformat(),
                "env": env,
                "order": {
                    "id": str(row.id),
                    "listing_id": str(row.listing_id),
                    "buyer_sub": str(row.buyer_sub),
                    "seller_sub": str(row.seller_sub),
                    "amount_cents": row.amount_cents,
                    "status": row.status,
                },
            },
            traceparent=current_traceparent(),
            created_at=now(),
        )
    )


def close(
    engine: Engine, order_id: uuid.UUID, status: str, env: str, failure_reason: str | None = None
):
    """pending → paid, failed or refunded, with its outbox row. Returns the order as it now
    is; if it was already final, nothing changes and no event is written."""
    if status not in FINAL:
        raise ValueError(status)
    at = now()
    values = {"status": status, "closed_at": at, "failure_reason": failure_reason}
    if status == "paid":
        values["paid_at"] = at
    with engine.begin() as conn:
        changed = conn.execute(
            update(orders)
            .where(orders.c.id == order_id, orders.c.status == "pending")
            .values(**values)
            .returning(*orders.c)
        ).first()
        if changed is None:
            return conn.execute(select(orders).where(orders.c.id == order_id)).first()
        _event(conn, changed, env)
        return changed


def record_result(
    engine: Engine,
    order_id: uuid.UUID,
    event_key: str,
    payment_intent_id: str,
    outcome: str,
    before: str,
    after: str,
) -> None:
    """Every payment result is kept, including the ignored ones (FR-011)."""
    with engine.begin() as conn:
        conn.execute(
            payment_result.insert().values(
                order_id=order_id,
                event_key=event_key,
                payment_intent_id=payment_intent_id,
                outcome=outcome,
                status_before=before,
                status_after=after,
                received_at=now(),
            )
        )
