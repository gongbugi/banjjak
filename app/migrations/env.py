import os

from alembic import context
from sqlalchemy import create_engine

from banjjak.db import metadata

with create_engine(os.environ["DATABASE_URL"]).connect() as conn:
    context.configure(connection=conn, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
