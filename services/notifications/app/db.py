"""Postgres: the table Notifications owns (data-model.md) and migrations at startup."""

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
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

log = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
# Any fixed number: only Notifications takes this lock, so replicas starting together migrate in
# turn.
_MIGRATION_LOCK = 7_340_003

metadata = MetaData()

# Matches migrations/versions/0001_notifications.py; the migration is the record.
notification = Table(
    "notification",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("event_id", UUID(as_uuid=True), nullable=False),
    Column("template", String, nullable=False),
    Column("recipient_sub", UUID(as_uuid=True), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("last_error", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sent_at", DateTime(timezone=True)),
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
