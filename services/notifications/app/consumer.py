"""T072: the consumer on notifications.orders (FR-015 to FR-017, research R8, R9).

order.paid sends the buyer a confirmation and the seller a sale email; order.refunded sends the
buyer a refund notice. Delivery is at least once, so each (event_id, template) is sent at most
once: a redelivered message finds its row `sent` and sends nothing more.

What `handle` answers decides the message:
  ack     every email for it is sent (or it is an event Notifications does not act on)
  requeue a send failed, fewer than five times: RabbitMQ delivers it again
  reject  it breaks the envelope, or a send failed for the fifth time (the row becomes
          dead_lettered): no requeue, so RabbitMQ moves it to notifications.orders.dead

The queue's own x-delivery-limit of 5 is the backstop: a message whose handling never finishes
(the database down, the pod killed) is dead-lettered on its sixth delivery.
"""

import asyncio
import json
import logging
import uuid
from typing import Annotated, Literal, Protocol

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from app.store import MAX_ATTEMPTS, MemoryStore, Store

log = logging.getLogger(__name__)

_PROPAGATOR = TraceContextTextMapPropagator()

# Which emails each event sends: (template, whose address).
PLANS: dict[str, tuple[tuple[str, str], ...]] = {
    "order.paid": (("order_confirmation_buyer", "buyer_sub"), ("item_sold_seller", "seller_sub")),
    "order.refunded": (("order_refund_buyer", "buyer_sub"),),
}


class Order(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: uuid.UUID
    listing_id: uuid.UUID
    listing_title: StrictStr | None = None
    buyer_sub: uuid.UUID
    seller_sub: uuid.UUID
    amount_cents: StrictInt
    status: Literal["paid", "failed", "refunded"]


class Envelope(BaseModel):
    """contracts/orders-events.asyncapi.yaml, Envelope."""

    model_config = ConfigDict(extra="allow")
    event_id: uuid.UUID
    event_type: Annotated[str, Field(min_length=1)]
    occurred_at: str
    env: Literal["dev", "prod"]
    order: Order


class Mailer(Protocol):
    async def send(self, to: str, template: str, context: dict) -> None: ...


class Users(Protocol):
    async def email_of(self, sub: str) -> str: ...


class Titles(Protocol):
    async def title_of(self, listing_id: str) -> str | None: ...


class Consumer:
    def __init__(
        self,
        *,
        mailer: Mailer,
        users: Users,
        store: Store | None = None,
        titles: Titles | None = None,
        on_send=None,
    ):
        self.mailer = mailer
        self.users = users
        self.store = store if store is not None else MemoryStore()
        self.titles = titles
        # Called with (template, result) after each send attempt; the metrics counter.
        self.on_send = on_send or (lambda template, result: None)

    async def handle(self, *, body: bytes, routing_key: str, headers: dict | None = None) -> str:
        try:
            message = Envelope.model_validate(json.loads(body))
        except (ValueError, ValidationError) as exc:
            # json.JSONDecodeError and UnicodeDecodeError are ValueErrors.
            log.warning(
                "message refused: it breaks the envelope",
                extra={"routing_key": routing_key, "reason": type(exc).__name__},
            )
            return "reject"
        if message.event_type != routing_key:
            log.warning(
                "message refused: event_type does not match its routing key",
                extra={"routing_key": routing_key, "event_id": str(message.event_id)},
            )
            return "reject"

        plan = PLANS.get(message.event_type)
        if plan is None:
            log.info("event not acted on", extra={"event_type": message.event_type})
            return "ack"

        order = message.order
        context = {
            "event_id": str(message.event_id),
            "order_id": str(order.id),
            "order": order.model_dump(mode="json"),
            "listing_title": order.listing_title or await self._title(order.listing_id),
        }
        delivery = _delivery_count(headers)
        worst = 0
        failed: list[str] = []
        for template, who in plan:
            sub = getattr(order, who)
            row = await self.store.claim(message.event_id, template, sub)
            if row.state == "sent":
                continue
            try:
                to = await self.users.email_of(str(sub))
                await self.mailer.send(to, template, context)
            except Exception as exc:  # noqa: BLE001 - any failure is counted and retried
                attempts = await self.store.record_failure(
                    message.event_id, template, f"{type(exc).__name__}: {exc}"
                )
                worst = max(worst, attempts)
                failed.append(template)
                self.on_send(template, "failed")
                log.warning(
                    "email not sent",
                    extra={
                        "event_id": str(message.event_id),
                        "order_id": str(order.id),
                        "template": template,
                        "attempts": attempts,
                        "reason": type(exc).__name__,
                    },
                )
                continue
            await self.store.mark_sent(message.event_id, template)
            self.on_send(template, "sent")
            log.info(
                "email sent",
                extra={
                    "event_id": str(message.event_id),
                    "order_id": str(order.id),
                    "template": template,
                },
            )

        if not failed:
            return "ack"
        # The fifth failure of a row, or the fifth redelivery of the message, ends it.
        if worst >= MAX_ATTEMPTS or delivery >= MAX_ATTEMPTS:
            for template in failed:
                await self.store.mark_dead(message.event_id, template)
            log.error(
                "message dead-lettered after repeated failures",
                extra={
                    "event_id": str(message.event_id),
                    "order_id": str(order.id),
                    "templates": failed,
                },
            )
            return "reject"
        return "requeue"

    async def _title(self, listing_id: uuid.UUID) -> str | None:
        if self.titles is None:
            return None
        return await self.titles.title_of(str(listing_id))


def _delivery_count(headers: dict | None) -> int:
    """x-delivery-count from a quorum queue: absent on the first delivery, then 1, 2, ..."""
    try:
        return int((headers or {}).get("x-delivery-count") or 0)
    except (TypeError, ValueError):
        return 0


def backoff_seconds(headers: dict | None) -> float:
    """How long to hold a message before requeueing it: 2, 4, 8, 16 s, so the five attempts
    span about half a minute instead of a few milliseconds."""
    return float(min(2 ** (_delivery_count(headers) + 1), 30))


async def process(consumer: Consumer, message, tracer: trace.Tracer | None = None) -> str:
    """One aio-pika message: handled under the trace context it carries, then settled."""
    tracer = tracer or trace.get_tracer(__name__)
    headers = {
        k: (v.decode() if isinstance(v, bytes) else v) for k, v in (message.headers or {}).items()
    }
    parent = _PROPAGATOR.extract({k: v for k, v in headers.items() if isinstance(v, str)})
    with tracer.start_as_current_span(
        f"{message.routing_key} process",
        context=parent,
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "rabbitmq",
            "messaging.operation.type": "process",
            "messaging.destination.name": "notifications.orders",
            "messaging.rabbitmq.destination.routing_key": message.routing_key or "",
            "messaging.message.id": message.message_id or "",
        },
    ) as span:
        try:
            outcome = await consumer.handle(
                body=message.body, routing_key=message.routing_key or "", headers=headers
            )
        except Exception:
            log.exception("message handling failed; it will be delivered again")
            outcome = "requeue"
        span.set_attribute("notifications.outcome", outcome)
    if outcome == "ack":
        await message.ack()
    elif outcome == "reject":
        await message.reject(requeue=False)
    else:
        await asyncio.sleep(backoff_seconds(headers))
        await message.nack(requeue=True)
    return outcome


async def run_forever(
    consumer: Consumer,
    amqp_url: str,
    queue_name: str,
    prefetch: int,
    tracer: trace.Tracer | None = None,
    retry_seconds: float = 5.0,
) -> None:
    """Consumes until cancelled. The queue is declared by the Messaging Topology Operator (T070);
    this only checks it is there. A lost connection is retried."""
    import aio_pika

    running: set[asyncio.Task] = set()
    while True:
        connection = None
        try:
            connection = await aio_pika.connect_robust(amqp_url)
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=prefetch)
            queue = await channel.get_queue(queue_name, ensure=True)
            log.info("consuming", extra={"queue": queue_name})
            async with queue.iterator() as messages:
                async for message in messages:
                    # Up to `prefetch` messages at once; a held reference keeps each task alive.
                    task = asyncio.create_task(process(consumer, message, tracer))
                    running.add(task)
                    task.add_done_callback(running.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("consumer connection failed; retrying")
        finally:
            if connection is not None and not connection.is_closed:
                await connection.close()
        await asyncio.sleep(retry_seconds)
