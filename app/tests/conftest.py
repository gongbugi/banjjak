import os
import tempfile

# 운영 AI의 판단 · 조치 기록 (SQLite). 운영은 운영 VM 디스크, 시험은 임시 파일
os.environ.setdefault("OPSAI_DB", os.path.join(tempfile.mkdtemp(), "opsai.db"))

# 진짜 PostgreSQL로 테스트한다 (CI는 서비스 컨테이너, 로컬은 Docker). 앱을 import하기 전에 정한다.
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres:test@localhost:55432/banjjak_test"),
)

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from banjjak import db, main

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
        conn.execute(text("TRUNCATE analyses, readmes, star_daily, star_snapshots, repos"))
    main._cache.clear()   # 앞 테스트의 화면이 남지 않게 (응답 캐시 60초)
