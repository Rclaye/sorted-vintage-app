"""Where each (event_id, template) stands: queued, sent or dead_lettered (T071, FR-016, FR-017).

One row per event and template, so a redelivered message finds its email already sent and sends
nothing. `attempts` counts failed sends; the fifth makes the row dead_lettered and the consumer
rejects the message, which RabbitMQ then moves to notifications.orders.dead.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db import notification

MAX_ATTEMPTS = 5


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Row:
    state: str
    attempts: int


class Store(Protocol):
    async def claim(self, event_id: uuid.UUID, template: str, recipient_sub: uuid.UUID) -> Row:
        """The row for this event and template, created `queued` if it is new."""

    async def mark_sent(self, event_id: uuid.UUID, template: str) -> None: ...

    async def record_failure(self, event_id: uuid.UUID, template: str, error: str) -> int:
        """Counts one failed send and returns the attempts so far."""

    async def mark_dead(self, event_id: uuid.UUID, template: str) -> None: ...


class MemoryStore:
    """The same rules in memory: for tests, and the contract tests' default."""

    def __init__(self):
        self.rows: dict[tuple[uuid.UUID, str], dict] = {}

    async def claim(self, event_id, template, recipient_sub) -> Row:
        row = self.rows.setdefault(
            (event_id, template),
            {"recipient_sub": recipient_sub, "state": "queued", "attempts": 0, "last_error": None},
        )
        return Row(row["state"], row["attempts"])

    async def mark_sent(self, event_id, template) -> None:
        row = self.rows[(event_id, template)]
        row["state"], row["last_error"] = "sent", None

    async def record_failure(self, event_id, template, error) -> int:
        row = self.rows[(event_id, template)]
        row["attempts"] = min(row["attempts"] + 1, MAX_ATTEMPTS)
        row["last_error"] = error
        return row["attempts"]

    async def mark_dead(self, event_id, template) -> None:
        self.rows[(event_id, template)]["state"] = "dead_lettered"


class PgStore:
    """The Notification table. Blocking database calls run off the event loop."""

    def __init__(self, engine: Engine):
        self.engine = engine

    async def claim(self, event_id, template, recipient_sub) -> Row:
        return await asyncio.to_thread(self._claim, event_id, template, recipient_sub)

    def _claim(self, event_id, template, recipient_sub) -> Row:
        with self.engine.begin() as conn:
            conn.execute(
                insert(notification)
                .values(
                    id=uuid.uuid4(),
                    event_id=event_id,
                    template=template,
                    recipient_sub=recipient_sub,
                    attempts=0,
                    state="queued",
                    created_at=now(),
                )
                .on_conflict_do_nothing(index_elements=["event_id", "template"])
            )
            row = conn.execute(
                select(notification.c.state, notification.c.attempts).where(
                    notification.c.event_id == event_id, notification.c.template == template
                )
            ).one()
        return Row(row.state, row.attempts)

    async def mark_sent(self, event_id, template) -> None:
        await asyncio.to_thread(
            self._update, event_id, template, state="sent", sent_at=now(), last_error=None
        )

    async def mark_dead(self, event_id, template) -> None:
        await asyncio.to_thread(self._update, event_id, template, state="dead_lettered")

    def _update(self, event_id, template, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(notification)
                .where(notification.c.event_id == event_id, notification.c.template == template)
                .values(**values)
            )

    async def record_failure(self, event_id, template, error) -> int:
        return await asyncio.to_thread(self._record_failure, event_id, template, error)

    def _record_failure(self, event_id, template, error) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                update(notification)
                .where(notification.c.event_id == event_id, notification.c.template == template)
                .values(
                    attempts=notification.c.attempts
                    + (notification.c.attempts < MAX_ATTEMPTS).cast(notification.c.attempts.type),
                    last_error=error[:500],
                )
                .returning(notification.c.attempts)
            ).scalar_one()
