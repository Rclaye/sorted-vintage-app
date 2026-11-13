"""T057: the message contract between Orders and Notifications (orders-events.asyncapi.yaml).

What is fixed here is behaviour: which messages are valid, which emails each one sends, that a
redelivered event_id sends nothing more, and that a message breaking the envelope sends nothing
and is rejected without requeue (so it dead-letters, FR-017). How the consumer is wired (its
constructor, the Keycloak lookup, SMTP) is T072's to settle; it may adjust the fakes below when it
removes the markers, but not the assertions.

Contract first (Ticket 3.2): the consumer arrives with T071 and T072 (Ticket 3.6). Until then each
test is a strict xfail; when the consumer makes one pass, CI fails until its marker is removed.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest

PENDING = pytest.mark.xfail(reason="T071, T072 (Ticket 3.6): consumer not built yet", strict=True)

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


class Mailer:
    """Records what would have gone to Mailpit."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to: str, template: str, context: dict) -> None:
        self.sent.append({"to": to, "template": template, "context": context})


class Users:
    """Keycloak as Notifications sees it with its view-users token: sub -> email."""

    def __init__(self, emails: dict[str, str]):
        self.emails = emails

    async def email_of(self, sub: str) -> str:
        return self.emails[sub]


def envelope(event_type: str, status: str, event_id: str | None = None, **order_overrides) -> dict:
    order = {
        "id": str(uuid.uuid4()),
        "listing_id": str(uuid.uuid4()),
        "listing_title": "Levi's Western shirt #01",
        "buyer_sub": str(uuid.uuid4()),
        "seller_sub": str(uuid.uuid4()),
        "amount_cents": 2500,
        "status": status,
    }
    order.update(order_overrides)
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "env": "dev",
        "order": order,
    }


@pytest.fixture
def world():
    """A consumer with fake mail and users; built inside each test so a missing module xfails."""

    def build(message: dict):
        from app.consumer import Consumer

        order = message["order"]
        users = Users(
            {order["buyer_sub"]: "buyer@sortedvtg.lan", order["seller_sub"]: "seller@sortedvtg.lan"}
        )
        mailer = Mailer()
        return Consumer(mailer=mailer, users=users, store=None), mailer

    return build


async def deliver(consumer, message: dict | bytes, routing_key: str, headers=None) -> str:
    body = message if isinstance(message, bytes) else json.dumps(message).encode()
    return await consumer.handle(
        body=body,
        routing_key=routing_key,
        headers={"traceparent": TRACEPARENT} if headers is None else headers,
    )


@PENDING
async def test_order_paid_sends_the_buyer_and_the_seller_one_email_each(world):
    message = envelope("order.paid", "paid")
    consumer, mailer = world(message)

    assert await deliver(consumer, message, "order.paid") == "ack"

    assert sorted((m["to"], m["template"]) for m in mailer.sent) == [
        ("buyer@sortedvtg.lan", "order_confirmation_buyer"),
        ("seller@sortedvtg.lan", "item_sold_seller"),
    ]
    assert all(m["context"]["order"]["id"] == message["order"]["id"] for m in mailer.sent)


@PENDING
async def test_order_refunded_sends_the_buyer_a_refund_notice(world):
    message = envelope("order.refunded", "refunded")
    consumer, mailer = world(message)

    assert await deliver(consumer, message, "order.refunded") == "ack"
    assert [(m["to"], m["template"]) for m in mailer.sent] == [
        ("buyer@sortedvtg.lan", "order_refund_buyer")
    ]


@PENDING
async def test_a_redelivered_event_sends_nothing_more(world):
    message = envelope("order.paid", "paid")
    consumer, mailer = world(message)

    for _ in range(3):
        assert await deliver(consumer, message, "order.paid") == "ack"
    assert len(mailer.sent) == 2


@PENDING
@pytest.mark.parametrize(
    "broken",
    [
        b"not json",
        json.dumps({"event_type": "order.paid"}).encode(),  # no event_id, env, order
        json.dumps(envelope("order.paid", "paid", event_id="not-a-uuid")).encode(),
        json.dumps(envelope("order.paid", "paid", amount_cents="2500")).encode(),  # not an integer
        json.dumps({**envelope("order.paid", "paid"), "env": "staging"}).encode(),
    ],
)
async def test_a_message_that_breaks_the_envelope_is_rejected_and_sends_nothing(world, broken):
    consumer, mailer = world(envelope("order.paid", "paid"))
    assert await deliver(consumer, broken, "order.paid") == "reject"
    assert mailer.sent == []


@PENDING
async def test_a_message_without_trace_context_is_still_handled(world):
    # traceparent is required on the wire (R9); a missing one must not lose the customer's email.
    message = envelope("order.paid", "paid")
    consumer, mailer = world(message)
    assert await deliver(consumer, message, "order.paid", headers={}) == "ack"
    assert len(mailer.sent) == 2
