"""T065: the outbox relay (research R8, R9).

Unpublished outbox rows go to the topic exchange `orders` with the event type as routing key,
on a channel with publisher confirms. `published_at` is set only after RabbitMQ confirms, so a
crash in between publishes the row again: delivery is at least once and consumers deduplicate
on event_id. Each publish runs under the trace context saved with its row, not the relay's own,
so the checkout's trace continues into Notifications.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy import Engine, select, update

from app.db import outbox_message
from app.store import now

log = logging.getLogger(__name__)
EXCHANGE = "orders"
BATCH = 100

_PROPAGATOR = TraceContextTextMapPropagator()

# Publishes one message and returns once the broker has confirmed it (raises otherwise).
Publish = Callable[[aio_pika.Message, str], Awaitable[Any]]


def unpublished(engine: Engine, limit: int = BATCH) -> list:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(outbox_message)
                .where(outbox_message.c.published_at.is_(None))
                .order_by(outbox_message.c.created_at, outbox_message.c.id)
                .limit(limit)
            )
        )


def mark_published(engine: Engine, row_id) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(outbox_message)
            .where(outbox_message.c.id == row_id, outbox_message.c.published_at.is_(None))
            .values(published_at=now())
        )


def message_for(row) -> aio_pika.Message:
    """The message, with the trace headers of a producer span under the row's context."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return aio_pika.Message(
        body=json.dumps(row.payload, separators=(",", ":")).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        message_id=str(row.id),
        type=row.event_type,
        headers=dict(carrier),
    )


async def relay_once(engine: Engine, publish: Publish, tracer: trace.Tracer | None = None) -> int:
    """Publishes what is waiting, oldest first; stops at the first failure so order holds."""
    tracer = tracer or trace.get_tracer(__name__)
    rows = await asyncio.to_thread(unpublished, engine)
    sent = 0
    for row in rows:
        parent = _PROPAGATOR.extract({"traceparent": row.traceparent} if row.traceparent else {})
        with tracer.start_as_current_span(
            f"{EXCHANGE} publish",
            context=parent,
            kind=SpanKind.PRODUCER,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "publish",
                "messaging.destination.name": EXCHANGE,
                "messaging.rabbitmq.destination.routing_key": row.event_type,
                "messaging.message.id": str(row.id),
            },
        ):
            await publish(message_for(row), row.event_type)
        await asyncio.to_thread(mark_published, engine, row.id)
        sent += 1
    return sent


async def connect(amqp_url: str):
    """A robust connection (it reconnects on its own), a confirming channel and the exchange.
    The exchange is declared by the Messaging Topology Operator (T070); this only checks it."""
    connection = await aio_pika.connect_robust(amqp_url)
    try:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.get_exchange(EXCHANGE, ensure=True)
    except Exception:
        await connection.close()
        raise

    async def publish(message: aio_pika.Message, routing_key: str) -> None:
        # mandatory off: order.failed has no queue bound in v1, and that is fine.
        await exchange.publish(message, routing_key=routing_key, mandatory=False)

    return connection, publish


async def run_forever(
    engine: Engine, amqp_url: str, interval: float, tracer: trace.Tracer | None = None
) -> None:
    """A failed pass is retried after `interval`; nothing is marked published without a confirm."""
    connection = publish = None
    try:
        while True:
            try:
                if publish is None:
                    connection, publish = await connect(amqp_url)
                sent = await relay_once(engine, publish, tracer)
                if sent:
                    log.info("outbox published", extra={"count": sent})
            except Exception:
                log.exception("outbox relay pass failed")
                if connection is not None and connection.is_closed:
                    connection = publish = None
            await asyncio.sleep(interval)
    finally:
        if connection is not None and not connection.is_closed:
            await connection.close()
