"""Order, idempotency key, outbox message and the payment-result record (T061, data-model.md).

Revision ID: 0001_orders
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001_orders"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = "('pending', 'paid', 'failed', 'refunded')"


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("listing_id", UUID(as_uuid=True), nullable=False),
        sa.Column("buyer_sub", UUID(as_uuid=True), nullable=False),
        sa.Column("seller_sub", UUID(as_uuid=True), nullable=False),
        # The listing price and the fee setting when checkout started (FR-013).
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("fee_pct", sa.Numeric(5, 2), nullable=False, server_default="10.00"),
        # The Listings hold this order settles (consume or release).
        sa.Column("reservation_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("reservation_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_intent_id", sa.Text, unique=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("failure_reason", sa.Text),
        # T064: when expiry released the hold and asked Payments to cancel; the order stays
        # pending until Payments reports canceled (failed) or a late success (refunded).
        sa.Column("expired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"status IN {STATUSES}", name="status_ok"),
        sa.CheckConstraint("amount_cents > 0", name="amount_positive"),
    )
    op.create_index("orders_buyer_idx", "orders", ["buyer_sub", "created_at"])
    op.create_index("orders_seller_idx", "orders", ["seller_sub", "created_at"])
    op.create_index(
        "orders_expiring_idx",
        "orders",
        ["reservation_expires_at"],
        postgresql_where=sa.text("status = 'pending' AND expired_at IS NULL"),
    )
    op.create_table(
        "idempotency_key",
        sa.Column("buyer_sub", UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("order_id", UUID(as_uuid=True), nullable=False, unique=True),
        # Returned again on a replay so the Payment Element can still load; never logged.
        sa.Column("client_secret", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "outbox_message",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("traceparent", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "outbox_unpublished_idx",
        "outbox_message",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "payment_result",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.Text, nullable=False),
        sa.Column("payment_intent_id", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("status_before", sa.Text, nullable=False),
        sa.Column("status_after", sa.Text, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("payment_result_order_idx", "payment_result", ["order_id"])


def downgrade() -> None:
    op.drop_table("payment_result")
    op.drop_index("outbox_unpublished_idx", table_name="outbox_message")
    op.drop_table("outbox_message")
    op.drop_table("idempotency_key")
    op.drop_index("orders_expiring_idx", table_name="orders")
    op.drop_index("orders_seller_idx", table_name="orders")
    op.drop_index("orders_buyer_idx", table_name="orders")
    op.drop_table("orders")
