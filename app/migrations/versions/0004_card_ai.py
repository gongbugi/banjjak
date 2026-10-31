"""카드 AI 행의 요약 · 근거 · 토큰 · 비용 (#19). 코드판 행은 비어 있다

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analyses", sa.Column("summary", sa.Text))
    op.add_column("analyses", sa.Column("evidence", JSONB))
    op.add_column("analyses", sa.Column("tokens", sa.Integer))
    op.add_column("analyses", sa.Column("cost", sa.Float))


def downgrade():
    for column in ("cost", "tokens", "evidence", "summary"):
        op.drop_column("analyses", column)
