import os

# 진짜 PostgreSQL로 테스트한다 (CI는 서비스 컨테이너, 로컬은 Docker). 앱을 import하기 전에 정한다.
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres:test@localhost:55432/banjjak_test"),
)

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from banjjak import db

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    cfg = Config(os.path.join(_HERE, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def _clean():
    with db.engine().begin() as conn:
        conn.execute(text("TRUNCATE star_snapshots, repos"))
