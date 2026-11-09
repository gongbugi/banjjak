
from alembic import context
from sqlalchemy import create_engine

from banjjak import secret
from banjjak.db import metadata

with create_engine(secret("DATABASE_URL", required=True)).connect() as conn:
    context.configure(connection=conn, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
