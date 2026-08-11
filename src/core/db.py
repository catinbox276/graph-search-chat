"""SQLAlchemy 엔진·세션 — ORM 도입의 단일 진입점 (18c/19c, thin/thick 공용).

- 접속은 creator로 oracledb.connect를 그대로 사용 — config의 thick 초기화·DSN 관례와
  URL 파싱 없이 호환. 풀은 SQLAlchemy가 관리.
- init_schema(): models.py의 전 테이블 create_all(멱등) + ORM이 표현 못 하는
  후처리(Oracle Text CONTEXT 인덱스, 코퍼스 소스 시드). 서버 기동 시 1회 호출.
- 단순 CRUD는 ORM 세션으로, 복잡한 검색·MERGE·배치는 기존 raw 경로 유지 (models.py 규약).
"""
import re
from contextlib import contextmanager

import oracledb
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from core import config
from core.models import Base

_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            "oracle+oracledb://",
            creator=lambda: oracledb.connect(
                user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                dsn=config.ORACLE_DSN),
            pool_size=config.ORACLE_POOL_MAX, max_overflow=2,
            pool_pre_ping=True)
    return _engine


@contextmanager
def session():
    """with db.session() as s: ... — 커밋/롤백/반납 자동."""
    s = Session(engine())
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_schema():
    """전 테이블 생성(멱등) + ORM 표현 밖 후처리(소스 시드). 서버 기동 시 1회.
    검색은 SQLite 인메모리 인덱스(search/inmemory_index.py)라 Oracle Text 권한 불필요."""
    Base.metadata.create_all(engine())
    with engine().begin() as con:
        _seed_sources(con)


def _seed_sources(con):
    """소스 1호(blog_posts) 시드 — create_all은 시드를 모른다. 빈 테이블에만."""
    from ingestion.source_registry import SEED_SOURCES
    import json
    n = con.execute(text("SELECT COUNT(*) FROM source_registry")).scalar()
    if n:
        return
    for name, tbl, idc, tsc, fmap, kind in SEED_SOURCES:
        con.execute(text("""INSERT INTO source_registry
            (source_name, table_name, id_column, ts_column, field_map, content_kind)
            VALUES (:n, :t, :i, :ts, :f, :k)"""),
            {"n": name, "t": tbl, "i": idc, "ts": tsc or None,
             "f": json.dumps(fmap, ensure_ascii=False), "k": kind})
