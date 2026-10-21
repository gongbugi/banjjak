import os
from functools import lru_cache

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, MetaData, Table, Text, create_engine
from sqlalchemy.dialects.postgresql import TIMESTAMP

metadata = MetaData()

repos = Table(
    "repos", metadata,
    Column("id", BigInteger, primary_key=True),          # GitHub 레포 id
    Column("full_name", Text, nullable=False),
    Column("html_url", Text, nullable=False),
    Column("description", Text),
    Column("language", Text),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("last_seen_at", TIMESTAMP(timezone=True), nullable=False),
)

# 매시간 찍는 사진: (레포, 시각, 별 수)
star_snapshots = Table(
    "star_snapshots", metadata,
    Column("repo_id", BigInteger, ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True),
    Column("captured_at", TIMESTAMP(timezone=True), primary_key=True),
    Column("stars", Integer, nullable=False),
)


@lru_cache
def engine():
    # pool_pre_ping: DB가 잠깐 끊겼다 돌아와도 죽은 연결을 쓰지 않는다 (커넥션 고착 방지)
    return create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
