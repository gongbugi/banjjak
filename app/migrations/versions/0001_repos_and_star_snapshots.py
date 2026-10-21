"""repos와 star_snapshots

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "repos",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("html_url", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("language", sa.Text),
        sa.Column("created_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "star_snapshots",
        sa.Column("repo_id", sa.BigInteger, sa.ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("captured_at", TIMESTAMP(timezone=True), primary_key=True),
        sa.Column("stars", sa.Integer, nullable=False),
    )
    op.create_index("star_snapshots_captured_at", "star_snapshots", ["captured_at"])


def downgrade():
    op.drop_table("star_snapshots")
    op.drop_table("repos")
