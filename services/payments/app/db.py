"""Postgres: the tables Payments owns (data-model.md) and migrations at startup."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

log = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
# Any fixed number: only Payments takes this lock, so replicas starting together migrate in turn.
_MIGRATION_LOCK = 7_340_001

metadata = MetaData()

# Matches migrations/versions/0001_connect.py; the migration is the record, this is for queries.
connected_account = Table(
    "connected_account",
    metadata,
    Column("account_sub", UUID(as_uuid=True), primary_key=True),
    Column("stripe_account_id", String, nullable=False, unique=True),
    Column("card_payments_status", String, nullable=False),
    Column("stripe_transfers_status", String, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

payment_event = Table(
    "payment_event",
    metadata,
    Column("stripe_event_id", String, primary_key=True),
    Column("type", String, nullable=False),
    Column("source", String, nullable=False),
    Column("payment_intent_id", String),
    Column("payload", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("applied_at", DateTime(timezone=True)),
    # 0002_charges: the inbox worker's backoff.
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True)),
)

# Matches migrations/versions/0002_charges.py.
payment_intent = Table(
    "payment_intent",
    metadata,
    Column("order_id", UUID(as_uuid=True), primary_key=True),
    Column("payment_intent_id", String, nullable=False, unique=True),
    Column("seller_sub", UUID(as_uuid=True), nullable=False),
    Column("amount_cents", Integer, nullable=False),
    Column("fee_pct", Numeric(5, 2), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # What Orders last answered for this order once it was final: paid, failed or refunded.
    Column("order_status", String),
    Column("settled_at", DateTime(timezone=True)),
)

ledger_entry = Table(
    "ledger_entry",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("order_id", UUID(as_uuid=True), nullable=False),
    Column("party", String, nullable=False),
    Column("account_sub", UUID(as_uuid=True)),
    Column("amount_cents", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def sqlalchemy_url(database_url: str) -> str:
    """postgresql://... from the Deployment, with the psycopg 3 driver named."""
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg://" + database_url[len(prefix) :]
    return database_url


def make_engine(database_url: str) -> Engine:
    return create_engine(sqlalchemy_url(database_url), pool_pre_ping=True, pool_size=5)


def migrate(engine: Engine) -> None:
    """alembic upgrade head, one replica at a time."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as conn:
        conn.execute(text("select pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK})
        try:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        finally:
            conn.execute(text("select pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK})
    log.info("database migrated to head")
