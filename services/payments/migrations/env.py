"""Alembic runs on the connection app.db.migrate hands it; there is no alembic.ini."""

from alembic import context

connection = context.config.attributes["connection"]
context.configure(connection=connection, transaction_per_migration=True)
with context.begin_transaction():
    context.run_migrations()
