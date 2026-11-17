"""PaymentIntents, the ledger and the inbox worker's backoff (T066-T068, data-model.md).

Revision ID: 0002_charges
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_charges"
down_revision = "0001_connect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One PaymentIntent per order: what the inbox worker needs to find the order of an event,
    # what the ledger needs to split the amount, and what reconciliation walks (T069).
    op.create_table(
        "payment_intent",
        sa.Column("order_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("payment_intent_id", sa.Text, nullable=False, unique=True),
        sa.Column("seller_sub", UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("fee_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("order_status", sa.Text),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount_cents > 0", name="amount_positive"),
        sa.CheckConstraint(
            "order_status IS NULL OR order_status IN ('paid', 'failed', 'refunded')",
            name="order_status_ok",
        ),
    )
    op.create_index(
        "payment_intent_unsettled_idx",
        "payment_intent",
        ["created_at"],
        postgresql_where=sa.text("settled_at IS NULL"),
    )
    op.create_table(
        "ledger_entry",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("party", sa.Text, nullable=False),
        sa.Column("account_sub", UUID(as_uuid=True)),
        # Positive for a sale, negative for its refund; a refunded order sums to zero.
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("party IN ('platform', 'seller')", name="party_ok"),
        sa.CheckConstraint("kind IN ('sale', 'refund')", name="kind_ok"),
        sa.CheckConstraint(
            "(party = 'platform') = (account_sub IS NULL)", name="account_only_for_seller"
        ),
        sa.UniqueConstraint("order_id", "party", "kind", name="ledger_entry_once"),
    )
    op.add_column(
        "payment_event", sa.Column("attempts", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column("payment_event", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("payment_event", "next_attempt_at")
    op.drop_column("payment_event", "attempts")
    op.drop_table("ledger_entry")
    op.drop_index("payment_intent_unsettled_idx", table_name="payment_intent")
    op.drop_table("payment_intent")
