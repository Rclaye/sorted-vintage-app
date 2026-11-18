"""T072: retries, dead-lettering and the parts of the consumer the contract does not fix."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.consumer import Consumer, backoff_seconds, process
from app.store import MemoryStore


class Mailer:
    def __init__(self, fail_for: set[str] | None = None):
        self.sent: list[tuple[str, str, dict]] = []
        self.fail_for = fail_for or set()

    async def send(self, to, template, context):
        if template in self.fail_for:
            raise ConnectionRefusedError("mailpit down")
        self.sent.append((to, template, context))


class Users:
    async def email_of(self, sub):
        return f"{sub}@sortedvtg.lan"


class Titles:
    def __init__(self):
        self.asked: list[str] = []

    async def title_of(self, listing_id):
        self.asked.append(listing_id)
        return "Carhartt jacket"


def message(event_type="order.paid", status="paid", **order):
    body = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "env": "dev",
        "order": {
            "id": str(uuid.uuid4()),
            "listing_id": str(uuid.uuid4()),
            "buyer_sub": str(uuid.uuid4()),
            "seller_sub": str(uuid.uuid4()),
            "amount_cents": 4200,
            "status": status,
            **order,
        },
    }
    return body, json.dumps(body).encode()


async def deliver(consumer, body, routing_key="order.paid", count=None):
    headers = {} if count is None else {"x-delivery-count": count}
    return await consumer.handle(body=body, routing_key=routing_key, headers=headers)


async def test_a_failing_send_is_requeued_four_times_then_rejected_and_dead_lettered():
    store, mailer = MemoryStore(), Mailer(fail_for={"item_sold_seller"})
    consumer = Consumer(mailer=mailer, users=Users(), store=store)
    data, body = message()
    event = uuid.UUID(data["event_id"])

    outcomes = [await deliver(consumer, body, count=n or None) for n in range(5)]

    assert outcomes == ["requeue"] * 4 + ["reject"]
    assert store.rows[(event, "item_sold_seller")]["state"] == "dead_lettered"
    assert store.rows[(event, "item_sold_seller")]["attempts"] == 5
    assert "ConnectionRefusedError" in store.rows[(event, "item_sold_seller")]["last_error"]
    # The buyer's email went on the first delivery and only then.
    assert store.rows[(event, "order_confirmation_buyer")]["state"] == "sent"
    assert [t for _, t, _ in mailer.sent] == ["order_confirmation_buyer"]


async def test_a_send_that_recovers_sends_only_what_is_missing():
    store, mailer = MemoryStore(), Mailer(fail_for={"item_sold_seller"})
    consumer = Consumer(mailer=mailer, users=Users(), store=store)
    _, body = message()

    assert await deliver(consumer, body) == "requeue"
    mailer.fail_for.clear()
    assert await deliver(consumer, body, count=1) == "ack"
    assert sorted(t for _, t, _ in mailer.sent) == ["item_sold_seller", "order_confirmation_buyer"]
    assert await deliver(consumer, body, count=2) == "ack"
    assert len(mailer.sent) == 2


async def test_the_fifth_redelivery_ends_a_message_even_if_its_row_counted_fewer():
    # The database was down for earlier deliveries, so the row saw only one failure.
    store, mailer = MemoryStore(), Mailer(fail_for={"order_refund_buyer"})
    consumer = Consumer(mailer=mailer, users=Users(), store=store)
    data, body = message("order.refunded", "refunded")

    assert await deliver(consumer, body, "order.refunded", count=5) == "reject"
    assert store.rows[(uuid.UUID(data["event_id"]), "order_refund_buyer")]["state"] == (
        "dead_lettered"
    )


async def test_a_title_missing_from_the_event_is_read_from_listings():
    titles, mailer = Titles(), Mailer()
    consumer = Consumer(mailer=mailer, users=Users(), titles=titles)
    data, body = message()

    assert await deliver(consumer, body) == "ack"
    assert titles.asked == [data["order"]["listing_id"]]
    assert {c["listing_title"] for _, _, c in mailer.sent} == {"Carhartt jacket"}


async def test_a_title_in_the_event_is_used_as_it_is():
    titles, mailer = Titles(), Mailer()
    consumer = Consumer(mailer=mailer, users=Users(), titles=titles)
    _, body = message(listing_title="Barbour coat")

    assert await deliver(consumer, body) == "ack"
    assert titles.asked == []
    assert {c["listing_title"] for _, _, c in mailer.sent} == {"Barbour coat"}


async def test_an_event_type_that_does_not_match_its_routing_key_is_rejected():
    mailer = Mailer()
    consumer = Consumer(mailer=mailer, users=Users())
    _, body = message("order.paid")
    assert await deliver(consumer, body, "order.refunded") == "reject"
    assert mailer.sent == []


async def test_an_event_notifications_does_not_act_on_is_acknowledged():
    mailer = Mailer()
    consumer = Consumer(mailer=mailer, users=Users())
    _, body = message("order.failed", "failed")
    assert await deliver(consumer, body, "order.failed") == "ack"
    assert mailer.sent == []


async def test_each_send_is_counted():
    seen = []
    consumer = Consumer(
        mailer=Mailer(fail_for={"item_sold_seller"}),
        users=Users(),
        on_send=lambda template, result: seen.append((template, result)),
    )
    _, body = message()
    await deliver(consumer, body)
    assert sorted(seen) == [("item_sold_seller", "failed"), ("order_confirmation_buyer", "sent")]


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({}, 2),
        ({"x-delivery-count": 1}, 4),
        ({"x-delivery-count": 3}, 16),
        (None, 2),
        ({"x-delivery-count": 9}, 30),
        ({"x-delivery-count": "x"}, 2),
    ],
)
def test_backoff_grows_and_is_capped(headers, seconds):
    assert backoff_seconds(headers) == seconds


class FakeMessage:
    def __init__(self, body, routing_key, headers):
        self.body, self.routing_key, self.headers = body, routing_key, headers
        self.message_id = "m-1"
        self.settled = None

    async def ack(self):
        self.settled = "ack"

    async def reject(self, requeue):
        self.settled = f"reject requeue={requeue}"

    async def nack(self, requeue):
        self.settled = f"nack requeue={requeue}"


async def test_process_settles_each_outcome(monkeypatch):
    monkeypatch.setattr("app.consumer.backoff_seconds", lambda headers: 0)
    consumer = Consumer(mailer=Mailer(fail_for={"item_sold_seller"}), users=Users())
    _, body = message()

    m = FakeMessage(body, "order.paid", {"x-delivery-count": 1})
    assert await process(consumer, m) == "requeue"
    assert m.settled == "nack requeue=True"

    m = FakeMessage(b"{", "order.paid", {})
    assert await process(consumer, m) == "reject"
    assert m.settled == "reject requeue=False"

    _, ok = message("order.failed", "failed")
    m = FakeMessage(ok, "order.failed", {b"traceparent": b"not-a-traceparent"})
    assert await process(consumer, m) == "ack"
    assert m.settled == "ack"


async def test_process_requeues_when_handling_itself_fails(monkeypatch):
    monkeypatch.setattr("app.consumer.backoff_seconds", lambda headers: 0)

    class BrokenStore(MemoryStore):
        async def claim(self, *args):
            raise OSError("database down")

    consumer = Consumer(mailer=Mailer(), users=Users(), store=BrokenStore())
    _, body = message()
    m = FakeMessage(body, "order.paid", {})
    assert await process(consumer, m) == "requeue"
    assert m.settled == "nack requeue=True"


async def test_two_copies_of_one_event_at_the_same_moment_send_once():
    class SlowMailer(Mailer):
        async def send(self, to, template, context):
            await asyncio.sleep(0.05)  # the other copy runs meanwhile
            await super().send(to, template, context)

    mailer = SlowMailer()
    consumer = Consumer(mailer=mailer, users=Users(), store=MemoryStore())
    _, body = message()

    outcomes = await asyncio.gather(deliver(consumer, body), deliver(consumer, body))

    assert outcomes == ["ack", "ack"]
    assert sorted(t for _, t, _ in mailer.sent) == ["item_sold_seller", "order_confirmation_buyer"]
    assert consumer._locks == {}  # nothing kept once both are done


async def test_different_events_do_not_wait_for_each_other():
    gate = asyncio.Event()

    class GatedMailer(Mailer):
        async def send(self, to, template, context):
            if context["order"]["amount_cents"] == 1:
                await gate.wait()
            await super().send(to, template, context)

    mailer = GatedMailer()
    consumer = Consumer(mailer=mailer, users=Users(), store=MemoryStore())
    _, held = message(amount_cents=1)
    _, free = message()

    first = asyncio.create_task(deliver(consumer, held))
    await asyncio.sleep(0)
    assert await asyncio.wait_for(deliver(consumer, free), 2) == "ack"
    gate.set()
    assert await first == "ack"


async def test_each_message_span_carries_its_order_and_the_next_starts_clean():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.telemetry import current_tags

    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    consumer = Consumer(mailer=Mailer(), users=Users())
    data, body = message()

    assert await process(consumer, FakeMessage(body, "order.paid", {}), provider.get_tracer("t"))
    assert await process(consumer, FakeMessage(b"{", "order.paid", {}), provider.get_tracer("t"))

    first, second = spans.get_finished_spans()
    assert first.attributes["order_id"] == data["order"]["id"]
    assert "order_id" not in second.attributes
    assert current_tags() == {}
