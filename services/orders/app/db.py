"""Postgres: the tables Orders owns (data-model.md) and migrations at startup."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    BigInteger,
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
# Any fixed number: only Orders takes this lock, so replicas starting together migrate in turn.
_MIGRATION_LOCK = 7_340_002

metadata = MetaData()

# Matches migrations/versions/0001_orders.py; the migration is the record, this is for queries.
orders = Table(
    "orders",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("listing_id", UUID(as_uuid=True), nullable=False),
    Column("buyer_sub", UUID(as_uuid=True), nullable=False),
    Column("seller_sub", UUID(as_uuid=True), nullable=False),
    Column("amount_cents", Integer, nullable=False),
    Column("fee_pct", Numeric(5, 2), nullable=False),
    Column("reservation_id", UUID(as_uuid=True), nullable=False),
    Column("reservation_expires_at", DateTime(timezone=True), nullable=False),
    Column("payment_intent_id", String, unique=True),
    Column("status", String, nullable=False),
    Column("failure_reason", String),
    Column("expired_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("paid_at", DateTime(timezone=True)),
    Column("closed_at", DateTime(timezone=True)),
)

idempotency_key = Table(
    "idempotency_key",
    metadata,
    Column("buyer_sub", UUID(as_uuid=True), primary_key=True),
    Column("key", String, primary_key=True),
    Column("order_id", UUID(as_uuid=True), nullable=False),
    Column("client_secret", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

outbox_message = Table(
    "outbox_message",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("event_type", String, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("traceparent", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True)),
)

payment_result = Table(
    "payment_result",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("order_id", UUID(as_uuid=True), nullable=False),
    Column("event_key", String, nullable=False),
    Column("payment_intent_id", String, nullable=False),
    Column("outcome", String, nullable=False),
    Column("status_before", String, nullable=False),
    Column("status_after", String, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
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
