"""Notification (T071, data-model.md): one row per event and template.

Revision ID: 0001_notifications
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0001_notifications"
down_revision = None
branch_labels = None
depends_on = None

# order_refund_buyer is the refund notice on order.refunded (the AsyncAPI contract, T057).
TEMPLATES = "('order_confirmation_buyer', 'item_sold_seller', 'order_refund_buyer')"
STATES = "('queued', 'sent', 'dead_lettered')"


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # The message's event_id (the Orders outbox row): with template, sent at most once.
        sa.Column("event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("template", sa.Text, nullable=False),
        # The address is looked up from Keycloak at send time, never stored.
        sa.Column("recipient_sub", UUID(as_uuid=True), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("event_id", "template", name="notification_event_template_key"),
        sa.CheckConstraint(f"template IN {TEMPLATES}", name="template_ok"),
        sa.CheckConstraint(f"state IN {STATES}", name="state_ok"),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5", name="attempts_at_most_5"),
    )
    op.create_index(
        "notification_dead_idx",
        "notification",
        ["created_at"],
        postgresql_where=sa.text("state = 'dead_lettered'"),
    )


def downgrade() -> None:
    op.drop_table("notification")
