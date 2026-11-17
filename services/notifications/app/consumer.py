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
import contextlib
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

# Seconds allowed to reach RabbitMQ, and to let go of it at shutdown, before giving up: a broker
# that accepts a socket but never answers must not hold the service (or a test) forever.
CONNECT_TIMEOUT = 15.0
CLOSE_TIMEOUT = 5.0

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
        # Two copies of one event handled at the same moment (prefetch > 1) take turns, so the
        # second finds the rows the first sent instead of sending them again.
        self._locks: dict[uuid.UUID, list] = {}

    @contextlib.asynccontextmanager
    async def _one_at_a_time(self, event_id: uuid.UUID):
        entry = self._locks.setdefault(event_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                del self._locks[event_id]

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
        async with self._one_at_a_time(message.event_id):
            return await self._send_all(message, plan, context, _delivery_count(headers))

    async def _send_all(self, message: Envelope, plan, context: dict, delivery: int) -> str:
        order = message.order
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
    this only checks it is there. A failure to connect or to find the queue, and a lost
    connection, are retried here after `retry_seconds`.

    A plain connection, not aio-pika's robust one: the robust connection keeps its own reconnect
    tasks, and when closing it ran out of time those tasks outlived the consumer and held the
    event loop open at shutdown (app main builds, 2026-11-17). Here every task is ours."""
    import aio_pika

    while True:
        connection = None
        running: set[asyncio.Task] = set()
        on_message = _dispatcher(consumer, tracer, running)
        try:
            connection = await asyncio.wait_for(
                aio_pika.connect(amqp_url, timeout=CONNECT_TIMEOUT), CONNECT_TIMEOUT
            )
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=prefetch)
            queue = await channel.get_queue(queue_name, ensure=True)
            await queue.consume(on_message)
            log.info("consuming", extra={"queue": queue_name})
            await connection.closed()  # until the broker goes away, or cancelled
            log.warning("connection to RabbitMQ lost; reconnecting", extra={"queue": queue_name})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("consumer could not start; retrying")
        finally:
            await _let_go(connection, running)
        await asyncio.sleep(retry_seconds)


def _dispatcher(consumer: Consumer, tracer, running: set[asyncio.Task]):
    """The queue callback for one connection: each message in its own task, up to `prefetch`
    at once, held in `running` so it stays alive and can be stopped at shutdown."""

    async def on_message(message) -> None:
        task = asyncio.create_task(process(consumer, message, tracer))
        running.add(task)
        task.add_done_callback(running.discard)

    return on_message


async def _let_go(connection, running: set[asyncio.Task]) -> None:
    """Stops in-flight messages (unsettled ones are delivered again) and closes the connection,
    each within CLOSE_TIMEOUT."""
    for task in list(running):
        task.cancel()
    if running:
        await asyncio.wait(running, timeout=CLOSE_TIMEOUT)
    if connection is not None and not connection.is_closed:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(connection.close(), CLOSE_TIMEOUT)
