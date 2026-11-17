"""T071: the Notification table on Postgres 17 (testcontainers), the same rules as MemoryStore."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.db import make_engine, migrate, notification
from app.store import PgStore


@pytest.fixture(scope="module")
def engine():
    try:
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("postgres:17-alpine", driver=None)
        container.start()
    except Exception as exc:
        pytest.skip(f"Docker is not available: {exc}")
    try:
        engine = make_engine(container.get_connection_url())
        migrate(engine)
        migrate(engine)  # a second start finds nothing to do
        yield engine
        engine.dispose()
    finally:
        container.stop()


@pytest.fixture
def store(engine):
    with engine.begin() as conn:
        conn.execute(text("truncate notification"))
    return PgStore(engine)


def row(engine, event_id, template):
    with engine.connect() as conn:
        return conn.execute(
            select(notification).where(
                notification.c.event_id == event_id, notification.c.template == template
            )
        ).one()


async def test_claim_creates_one_queued_row_and_finds_it_again(store, engine):
    event, sub = uuid.uuid4(), uuid.uuid4()
    first = await store.claim(event, "order_confirmation_buyer", sub)
    again = await store.claim(event, "order_confirmation_buyer", sub)
    assert (first.state, first.attempts) == ("queued", 0)
    assert (again.state, again.attempts) == ("queued", 0)
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from notification")).scalar_one() == 1
    assert row(engine, event, "order_confirmation_buyer").recipient_sub == sub


async def test_sent_is_kept_with_its_time(store, engine):
    event = uuid.uuid4()
    await store.claim(event, "item_sold_seller", uuid.uuid4())
    await store.record_failure(event, "item_sold_seller", "SMTPException: busy")
    await store.mark_sent(event, "item_sold_seller")

    r = row(engine, event, "item_sold_seller")
    assert (r.state, r.attempts, r.last_error) == ("sent", 1, None)
    assert r.sent_at is not None
    assert (await store.claim(event, "item_sold_seller", uuid.uuid4())).state == "sent"


async def test_failures_count_up_to_five_and_stop_there(store, engine):
    event = uuid.uuid4()
    await store.claim(event, "order_refund_buyer", uuid.uuid4())
    counts = [await store.record_failure(event, "order_refund_buyer", "x" * 900) for _ in range(6)]
    await store.mark_dead(event, "order_refund_buyer")

    assert counts == [1, 2, 3, 4, 5, 5]
    r = row(engine, event, "order_refund_buyer")
    assert (r.state, r.attempts, len(r.last_error)) == ("dead_lettered", 5, 500)


async def test_two_templates_of_one_event_are_separate_rows(store):
    event = uuid.uuid4()
    await store.claim(event, "order_confirmation_buyer", uuid.uuid4())
    await store.claim(event, "item_sold_seller", uuid.uuid4())
    await store.mark_sent(event, "order_confirmation_buyer")
    assert (await store.claim(event, "item_sold_seller", uuid.uuid4())).state == "queued"


@pytest.mark.parametrize(
    "values",
    [
        {"template": "welcome"},
        {"state": "lost"},
        {"attempts": 6},
    ],
)
def test_the_table_refuses_what_the_data_model_does_not_allow(engine, values):
    base = {
        "id": uuid.uuid4(),
        "event_id": uuid.uuid4(),
        "template": "order_confirmation_buyer",
        "recipient_sub": uuid.uuid4(),
        "attempts": 0,
        "state": "queued",
        "created_at": datetime.now(UTC),
    }
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(notification).values(**{**base, **values}))


def test_the_app_migrates_starts_the_consumer_and_stops_it_cleanly(engine, settings, monkeypatch):
    """Startup on the real database: migrations at head, the consumer started (its broker is
    unreachable here, so it keeps retrying) and cancelled at shutdown."""
    import dataclasses

    from fastapi.testclient import TestClient

    from app import consumer as consumer_module
    from app.main import create_app

    started = []
    real = consumer_module.run_forever

    async def watched(*args, **kwargs):
        started.append(args[2])
        await real(*args, **{**kwargs, "retry_seconds": 0.05})

    monkeypatch.setattr(consumer_module, "run_forever", watched)
    app = create_app(
        dataclasses.replace(settings, prefetch=1),
        keys=object(),
        checks={},
        engine=engine,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert started == ["notifications.orders"]
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar_one() == (
            "0001_notifications"
        )
