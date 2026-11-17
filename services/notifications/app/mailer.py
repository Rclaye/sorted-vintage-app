"""The three emails (FR-015) and SMTP to this environment's Mailpit.

Plain text, one recipient each. smtplib blocks, so a send runs off the event loop. Each email
carries its event and template in headers, which is how a duplicate would be found in Mailpit.
"""

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

TEMPLATES = ("order_confirmation_buyer", "item_sold_seller", "order_refund_buyer")


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def compose(template: str, context: dict) -> tuple[str, str]:
    """Subject and body for a template."""
    order = context["order"]
    item = context.get("listing_title") or "your item"
    amount = money(int(order["amount_cents"]))
    ref = f"Order reference: {order['id']}"
    if template == "order_confirmation_buyer":
        return (
            f"Your order is confirmed: {item}",
            f"Thank you for your purchase.\n\nItem: {item}\nPaid: {amount}\n{ref}\n\n"
            "The seller has been told and will send it to you.\n",
        )
    if template == "item_sold_seller":
        return (
            f"You sold {item}",
            f"Good news: {item} has sold for {amount}.\n{ref}\n\n"
            "Your payout, less the platform fee, goes to your Stripe account.\n",
        )
    if template == "order_refund_buyer":
        return (
            f"Your refund for {item}",
            f"We could not complete your order, so we have refunded {amount} in full.\n{ref}\n\n"
            "The refund can take a few days to show on your statement.\n",
        )
    raise ValueError(f"unknown template {template}")


class SmtpMailer:
    def __init__(self, host: str, port: int, sender: str, timeout: float = 10.0):
        self.host, self.port, self.sender, self.timeout = host, port, sender, timeout

    async def send(self, to: str, template: str, context: dict) -> None:
        subject, body = compose(template, context)
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain="sortedvtg.lan")
        message["X-SV-Event-Id"] = str(context["event_id"])
        message["X-SV-Template"] = template
        message.set_content(body)
        await asyncio.to_thread(self._deliver, message)

    def _deliver(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            smtp.send_message(message)
