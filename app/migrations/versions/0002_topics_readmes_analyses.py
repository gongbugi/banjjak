"""토픽 · README 원문 · 분석 결과

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("repos", sa.Column("topics", ARRAY(sa.Text), nullable=False, server_default="{}"))
    op.create_table(
        "readmes",
        sa.Column("repo_id", sa.BigInteger, sa.ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sha", sa.Text, primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("fetched_at", TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "analyses",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("repo_id", sa.BigInteger, sa.ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("readme_sha", sa.Text),
        sa.Column("analyzed_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("checked_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("category", sa.Text),
        sa.Column("usage", sa.Text),
        sa.Column("notes", JSONB, nullable=False, server_default="[]"),
    )
    op.create_index("analyses_repo_method_checked", "analyses", ["repo_id", "method", "checked_at"])
    op.create_index("analyses_analyzed_at", "analyses", ["analyzed_at"])


def downgrade():
    op.drop_table("analyses")
    op.drop_table("readmes")
    op.drop_column("repos", "topics")
