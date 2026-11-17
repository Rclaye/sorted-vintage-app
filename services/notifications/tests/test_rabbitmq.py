"""T072 against RabbitMQ 4.3.4 (the lab's version) with the T070 topology, in a container."""

import asyncio
import contextlib
import functools
import json
import uuid
from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import consumer as consumer_module
from app.consumer import Consumer
from app.store import MemoryStore

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE_ID}-b7ad6b7169203331-01"


def bounded(test):
    """A broker that stops answering fails the test within a minute instead of hanging CI (seen
    once on app main, 2026-11-17: the job sat in this module for 12 minutes)."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(60):
            return await test(*args, **kwargs)

    return run


@pytest.fixture(scope="module")
def amqp_url():
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    try:
        container = (
            DockerContainer("rabbitmq:4.3.4-alpine")
            .with_exposed_ports(5672)
            .waiting_for(LogMessageWaitStrategy("Server startup complete"))
        )
        container.start()
    except Exception as exc:
        pytest.skip(f"Docker is not available: {exc}")
    try:
        host, port = container.get_container_host_ip(), container.get_exposed_port(5672)
        yield f"amqp://guest:guest@{host}:{port}/"
    finally:
        container.stop()


@pytest.fixture
async def broker(amqp_url):
    """The T070 topology (services/orders/base/messaging.yaml in gitops), fresh for each test."""
    import aio_pika

    connection = await asyncio.wait_for(aio_pika.connect(amqp_url, timeout=15), 15)
    channel = await connection.channel()
    suffix = uuid.uuid4().hex[:8]
    orders = await channel.declare_exchange(
        f"orders.{suffix}", aio_pika.ExchangeType.TOPIC, durable=True
    )
    dlx = await channel.declare_exchange(f"orders.dlx.{suffix}", aio_pika.ExchangeType.FANOUT)
    queue = await channel.declare_queue(
        f"notifications.orders.{suffix}",
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-delivery-limit": 5,
            "x-dead-letter-exchange": dlx.name,
        },
    )
    dead = await channel.declare_queue(
        f"notifications.orders.dead.{suffix}", durable=True, arguments={"x-queue-type": "quorum"}
    )
    for key in ("order.paid", "order.refunded"):
        await queue.bind(orders, routing_key=key)
    await dead.bind(dlx)
    yield {"exchange": orders, "queue": queue.name, "dead": dead}
    with contextlib.suppress(Exception):
        await asyncio.wait_for(connection.close(), 5)


class Mailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, to, template, context):
        if self.fail:
            raise ConnectionRefusedError("mailpit down")
        self.sent.append((to, template))


class Users:
    async def email_of(self, sub):
        return f"{sub}@sortedvtg.lan"


def envelope(event_type, status, event_id=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "env": "dev",
        "order": {
            "id": str(uuid.uuid4()),
            "listing_id": str(uuid.uuid4()),
            "buyer_sub": str(uuid.uuid4()),
            "seller_sub": str(uuid.uuid4()),
            "amount_cents": 2500,
            "status": status,
        },
    }


async def publish(broker, body: dict):
    import aio_pika

    await broker["exchange"].publish(
        aio_pika.Message(
            json.dumps(body).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=body["event_id"],
            headers={"traceparent": TRACEPARENT},
        ),
        routing_key=body["event_type"],
    )


@contextlib.asynccontextmanager
async def consuming(consumer, amqp_url, queue, tracer=None):
    task = asyncio.create_task(
        consumer_module.run_forever(consumer, amqp_url, queue, 10, tracer, retry_seconds=0.2)
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)


async def until(condition, seconds=20.0):
    for _ in range(int(seconds / 0.1)):
        if await condition():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


@bounded
async def test_order_paid_sends_two_emails_once_under_the_publishers_trace(amqp_url, broker):
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    mailer = Mailer()
    consumer = Consumer(mailer=mailer, users=Users(), store=MemoryStore())
    body = envelope("order.paid", "paid")

    async with consuming(consumer, amqp_url, broker["queue"], provider.get_tracer("t")):
        await publish(broker, body)
        await publish(broker, body)  # at least once: the same event again

        async def two_sent_and_settled():
            return len(mailer.sent) == 2 and len(spans.get_finished_spans()) == 2

        await until(two_sent_and_settled)
        await asyncio.sleep(0.5)

    assert len(mailer.sent) == 2
    span = spans.get_finished_spans()[0]
    assert span.name == "order.paid process"
    assert format(span.context.trace_id, "032x") == TRACE_ID
    assert span.attributes["notifications.outcome"] == "ack"


@bounded
async def test_a_message_that_fails_five_times_is_dead_lettered(amqp_url, broker, monkeypatch):
    monkeypatch.setattr(consumer_module, "backoff_seconds", lambda headers: 0)
    store = MemoryStore()
    consumer = Consumer(mailer=Mailer(fail=True), users=Users(), store=store)
    body = envelope("order.refunded", "refunded")
    key = (uuid.UUID(body["event_id"]), "order_refund_buyer")

    async with consuming(consumer, amqp_url, broker["queue"]):
        await publish(broker, body)

        async def dead_lettered():
            return key in store.rows and store.rows[key]["state"] == "dead_lettered"

        await until(dead_lettered)

    message = await broker["dead"].get(timeout=5)
    await message.ack()
    assert json.loads(message.body)["event_id"] == body["event_id"]
    assert store.rows[key]["attempts"] == 5
    # Rejected by the consumer on its fifth failure, before the queue's own limit.
    assert message.headers["x-first-death-reason"] == "rejected"


@bounded
async def test_a_broken_message_goes_straight_to_the_dead_queue(amqp_url, broker):
    import aio_pika

    mailer = Mailer()
    consumer = Consumer(mailer=mailer, users=Users(), store=MemoryStore())
    async with consuming(consumer, amqp_url, broker["queue"]):
        await broker["exchange"].publish(aio_pika.Message(b"not json"), routing_key="order.paid")
        message = None
        for _ in range(100):
            message = await broker["dead"].get(fail=False)
            if message:
                break
            await asyncio.sleep(0.1)
    assert message is not None
    await message.ack()
    assert message.body == b"not json"
    assert mailer.sent == []


@bounded
async def test_a_missing_queue_is_retried_not_fatal(amqp_url):
    consumer = Consumer(mailer=Mailer(), users=Users(), store=MemoryStore())
    task = asyncio.create_task(
        consumer_module.run_forever(consumer, amqp_url, "no.such.queue", 1, retry_seconds=0.1)
    )
    await asyncio.sleep(1.0)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)


@bounded
async def test_an_unreachable_broker_is_retried_and_shutdown_is_prompt():
    consumer = Consumer(mailer=Mailer(), users=Users(), store=MemoryStore())
    task = asyncio.create_task(
        consumer_module.run_forever(
            consumer, "amqp://guest:guest@127.0.0.1:1/", "q", 1, retry_seconds=0.05
        )
    )
    await asyncio.sleep(0.5)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
