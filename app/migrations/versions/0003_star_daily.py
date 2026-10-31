"""하루 한 번 찍는 별 기록 (지금 뜨는 레포 탭)

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "star_daily",
        sa.Column("repo_id", sa.BigInteger, sa.ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("captured_at", TIMESTAMP(timezone=True), primary_key=True),
        sa.Column("stars", sa.Integer, nullable=False),
    )


def downgrade():
    op.drop_table("star_daily")
