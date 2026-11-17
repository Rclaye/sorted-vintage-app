"""The three emails (FR-015) and what goes to SMTP."""

import pytest

from app.mailer import TEMPLATES, SmtpMailer, compose, money

CONTEXT = {
    "event_id": "e-1",
    "order": {"id": "o-1", "amount_cents": 123456},
    "listing_title": "Stussy Shirt",
}


def test_money_is_dollars_and_cents():
    assert money(2500) == "$25.00"
    assert money(123456) == "$1,234.56"


@pytest.mark.parametrize(
    ("template", "subject"),
    [
        ("order_confirmation_buyer", "Your order is confirmed: Stussy Shirt"),
        ("item_sold_seller", "You sold Stussy Shirt"),
        ("order_refund_buyer", "Your refund for Stussy Shirt"),
    ],
)
def test_each_template_names_the_item_the_amount_and_the_order(template, subject):
    got_subject, body = compose(template, CONTEXT)
    assert got_subject == subject
    assert "$1,234.56" in body
    assert "o-1" in body


def test_without_a_title_the_email_still_reads_well():
    subject, _ = compose("item_sold_seller", {**CONTEXT, "listing_title": None})
    assert subject == "You sold your item"


def test_an_unknown_template_is_refused():
    with pytest.raises(ValueError, match="unknown template"):
        compose("welcome", CONTEXT)


def test_every_template_in_the_table_can_be_composed():
    for template in TEMPLATES:
        assert compose(template, CONTEXT)[0]


async def test_send_hands_one_message_to_smtp(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            seen["server"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, message):
            seen["message"] = message

    monkeypatch.setattr("app.mailer.smtplib.SMTP", FakeSMTP)
    mailer = SmtpMailer("mailpit", 1025, "Sorted Vintage <no-reply@sortedvtg.lan>")
    await mailer.send("buyer@sortedvtg.lan", "order_confirmation_buyer", CONTEXT)

    message = seen["message"]
    assert seen["server"] == ("mailpit", 1025, 10.0)
    assert message["To"] == "buyer@sortedvtg.lan"
    assert message["From"] == "Sorted Vintage <no-reply@sortedvtg.lan>"
    assert message["Subject"] == "Your order is confirmed: Stussy Shirt"
    assert message["X-SV-Event-Id"] == "e-1"
    assert message["X-SV-Template"] == "order_confirmation_buyer"
    assert "Stussy Shirt" in message.get_content()
