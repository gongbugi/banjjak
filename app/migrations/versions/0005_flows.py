"""흐름 묶음: 탭마다 하루 한 번 AI가 순위를 묶은 결과와 토큰 · 비용

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "flows",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("view", sa.Text, nullable=False),
        sa.Column("made_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("groups", JSONB, nullable=False, server_default="[]"),
        sa.Column("tokens", sa.Integer),
        sa.Column("cost", sa.Float),
    )


def downgrade():
    op.drop_table("flows")
