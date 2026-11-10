"""Connected account and Payment event (T042, data-model.md).

Revision ID: 0001_connect
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001_connect"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = "('active', 'pending', 'restricted', 'unsupported')"


def upgrade() -> None:
    op.create_table(
        "connected_account",
        sa.Column("account_sub", UUID(as_uuid=True), primary_key=True),
        sa.Column("stripe_account_id", sa.Text, nullable=False, unique=True),
        # Accounts v2 capability statuses (research R17), as Stripe last reported them.
        sa.Column("card_payments_status", sa.Text, nullable=False),
        sa.Column("stripe_transfers_status", sa.Text, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"card_payments_status IN {STATUSES}", name="card_payments_status_ok"),
        sa.CheckConstraint(
            f"stripe_transfers_status IN {STATUSES}", name="stripe_transfers_status_ok"
        ),
    )
    op.create_table(
        "payment_event",
        sa.Column("stripe_event_id", sa.Text, primary_key=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("payment_intent_id", sa.Text),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "source IN ('webhook', 'connect_webhook', 'reconciliation')", name="source_ok"
        ),
    )
    op.create_index(
        "payment_event_unapplied_idx",
        "payment_event",
        ["received_at"],
        postgresql_where=sa.text("applied_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("payment_event_unapplied_idx", table_name="payment_event")
    op.drop_table("payment_event")
    op.drop_table("connected_account")
