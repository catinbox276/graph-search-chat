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
    """전 테이블 생성(멱등) + ORM 표현 밖 후처리. 서버 기동 시 1회."""
    Base.metadata.create_all(engine())
    with engine().begin() as con:
        _ensure_text_index(con)
        _seed_sources(con)


def _ensure_text_index(con):
    """corpus_docs.body의 Oracle Text CONTEXT 인덱스 — CREATE INDEX ... INDEXTYPE는
    ORM 표현 밖이라 여기서. 렉서 프리퍼런스가 먼저(권한 없으면 명확히 실패)."""
    n = con.execute(text("""SELECT COUNT(*) FROM user_indexes
                            WHERE index_name = 'CORPUS_DOCS_BODY_IDX'""")).scalar()
    if n:
        return
    lexer = config.ORACLE_TEXT_LEXER
    if not re.fullmatch(r"[A-Za-z0-9_]+", lexer):
        raise ValueError(f"잘못된 ORACLE_TEXT_LEXER: {lexer!r}")
    try:
        con.execute(text(f"""
            BEGIN
              BEGIN ctx_ddl.create_preference('blog_lexer', '{lexer}');
              EXCEPTION WHEN OTHERS THEN NULL;  -- 이미 있으면 그대로 사용
              END;
            END;"""))
    except Exception as e:
        if "PLS-00201" in str(e) or "06550" in str(e):
            raise RuntimeError(
                "Oracle Text 권한이 없습니다 — 검색 인덱스 생성에 필요합니다.\n"
                "DBA 권한 계정에서 1회 실행하세요:\n"
                "  GRANT CTXAPP TO <앱 계정>;\n"
                "  GRANT EXECUTE ON CTXSYS.CTX_DDL TO <앱 계정>;") from e
        raise
    con.execute(text("""
        CREATE INDEX corpus_docs_body_idx ON corpus_docs(body)
        INDEXTYPE IS CTXSYS.CONTEXT
        PARAMETERS ('LEXER blog_lexer SYNC (ON COMMIT)')"""))
    print("corpus_docs Oracle Text 인덱스 생성")


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
