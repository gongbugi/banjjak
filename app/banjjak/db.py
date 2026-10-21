import os
from functools import lru_cache

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Identity, Integer, MetaData, Table, Text, create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP

metadata = MetaData()

repos = Table(
    "repos", metadata,
    Column("id", BigInteger, primary_key=True),          # GitHub 레포 id
    Column("full_name", Text, nullable=False),
    Column("html_url", Text, nullable=False),
    Column("description", Text),
    Column("language", Text),
    Column("topics", ARRAY(Text), nullable=False, server_default="{}"),
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

# README 원문. 분석 방식이 달라도(코드판 · AI판) 같은 원문으로 돌려 비교한다
readmes = Table(
    "readmes", metadata,
    Column("repo_id", BigInteger, ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True),
    Column("sha", Text, primary_key=True),
    Column("content", Text, nullable=False),
    Column("fetched_at", TIMESTAMP(timezone=True), nullable=False),
)

# 분석 결과. 덮어쓰지 않고 방식(method)마다 따로 쌓는다 → 코드판 기준선이 남는다
analyses = Table(
    "analyses", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("repo_id", BigInteger, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
    Column("method", Text, nullable=False),
    Column("readme_sha", Text),
    Column("analyzed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("checked_at", TIMESTAMP(timezone=True), nullable=False),   # README가 그대로인지 마지막으로 본 때
    Column("ok", Boolean, nullable=False),
    Column("error", Text),
    Column("category", Text),
    Column("usage", Text),
    Column("notes", JSONB, nullable=False, server_default="[]"),
)


@lru_cache
def engine():
    # pool_pre_ping: DB가 잠깐 끊겼다 돌아와도 죽은 연결을 쓰지 않는다 (커넥션 고착 방지)
    return create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
