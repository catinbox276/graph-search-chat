"""구조화 원천 테이블 레지스트리 — docs/integration.md 접점 2.

원천 테이블은 저쪽 소유(읽기 전용)이고, 어떤 테이블의 어떤 컬럼을 어떤 역할로
구조화할지는 사람이 이 레지스트리에 등록한다 (domain_registry와 같은
'사람 전용 시드' 패턴 — LLM에게 쓰기 경로 없음).

- field_map: {역할: 컬럼명} JSON. 역할 어휘는 닫혀 있다(ROLES) — 역할 조합이
  검색 문서 조립 방식을 결정한다. 본문 1컬럼형은 body 하나, QA형은 question+answer.
- ts_column: 증분 적재 워터마크 기준. 없으면(빈값) 전량 1회 적재 소스.
- 적재 배치(scripts/ingest_sources.py)가 이 레지스트리를 읽어 corpus_docs로 조립한다.
"""
import json
import re

from core import config

# 역할 어휘 (닫힌 목록) — title은 목록 표시·검색용, meta는 태그류 부가 텍스트,
# url은 원문 참조 링크(검색 결과에 노출)
ROLES = ("title", "body", "question", "answer", "meta", "url")
TEXT_ROLES = ("body", "question", "answer")  # 최소 하나는 있어야 검색 문서가 됨

# 기본 시드: 기존 blog_posts 코퍼스를 '소스 1호'로 흡수 (ts 컬럼이 없어 전량 1회형)
SEED_SOURCES = (
    ("blog_posts", "BLOG_POSTS", "ID", "",
     {"title": "TITLE", "body": "BODY", "meta": "TAGS", "url": "URL"}, "문제해결 노하우"),
)

# 테이블 브라우저에서 걸러낼 것: Oracle 내부(SYSTEM 스키마 노이즈) + 우리 소유 테이블
_NOISE_PREFIX = ("LOGMNR", "LOGSTDBY", "MVIEW$", "AQ$", "OL$", "ROLLING$",
                 "REDO_", "SCHEDULER_", "SQLPLUS_", "DR$")
OUR_TABLES = {"SESSIONS", "NODES", "EDGES", "NODE_EVIDENCE", "SUGGESTIONS",
              "MODEL_REGISTRY", "DOMAIN_REGISTRY", "SOURCE_REGISTRY",
              "CORPUS_DOCS", "LG_CHECKPOINTS", "LG_WRITES", "HELP"}


def ensure(cur):
    """source_registry가 없으면 만들고 기본 시드를 넣는다 (멱등)."""
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'SOURCE_REGISTRY'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE source_registry (
            source_name    VARCHAR2(100) PRIMARY KEY,
            table_name     VARCHAR2(128) NOT NULL,   -- 원천 테이블 (읽기 전용)
            id_column      VARCHAR2(128) NOT NULL,   -- 고유 id 필드
            ts_column      VARCHAR2(128),            -- 생성시간 필드 (증분 워터마크, 없으면 전량 1회)
            field_map      VARCHAR2(4000) NOT NULL,  -- JSON {역할: 컬럼} 역할=title|body|question|answer|meta|url
            content_kind   VARCHAR2(100),            -- 내용 유형 (문제해결/가이드 등) — 프롬프트 힌트
            domain         VARCHAR2(100),            -- 그래프 구조화 도메인 (NULL=검색만, 지정 시 doc_pipeline 대상)
            enabled        CHAR(1) DEFAULT 'Y',
            last_ingest_ts TIMESTAMP,                -- 증분 적재 워터마크 (배치가 갱신)
            created_at     TIMESTAMP DEFAULT SYSTIMESTAMP
        )""")
        for name, tbl, idc, tsc, fmap, kind in SEED_SOURCES:
            cur.execute("""INSERT INTO source_registry
                           (source_name, table_name, id_column, ts_column, field_map, content_kind)
                           VALUES (:1, :2, :3, :4, :5, :6)""",
                        [name, tbl, idc, tsc or None,
                         json.dumps(fmap, ensure_ascii=False), kind])
        return
    # 구버전 테이블에 domain 컬럼이 없으면 추가 (그래프 구조화 대상 지정)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SOURCE_REGISTRY' AND column_name = 'DOMAIN'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE source_registry ADD (domain VARCHAR2(100))")
    # 원본 링크 노출 스위치 (N이면 검색·출처·문서 뷰에서 url 숨김 — 즉시 반영)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SOURCE_REGISTRY' AND column_name = 'URL_ENABLED'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE source_registry ADD (url_enabled CHAR(1) DEFAULT 'Y')")
    _ensure_domain_fk(cur)


def _ensure_domain_fk(cur):
    """source_registry.domain -> domain_registry(name) FK (멱등).
    domain_registry가 아직 없으면(기동 순서) 다음 ensure 때 다시 시도."""
    cur.execute("""SELECT COUNT(*) FROM user_constraints
                   WHERE constraint_name = 'SOURCE_REGISTRY_DOMAIN_FK'""")
    if cur.fetchone()[0]:
        return
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DOMAIN_REGISTRY'")
    if cur.fetchone()[0]:
        cur.execute("""ALTER TABLE source_registry ADD CONSTRAINT
                       source_registry_domain_fk FOREIGN KEY (domain)
                       REFERENCES domain_registry(name)""")


def table_columns(cur, table_name: str) -> dict:
    """테이블의 {컬럼명: 타입} — 없으면 빈 dict."""
    cur.execute("""SELECT column_name, data_type FROM user_tab_columns
                   WHERE table_name = :1 ORDER BY column_id""", [table_name.upper()])
    return {r[0]: r[1] for r in cur.fetchall()}


def table_allowed(table_name: str) -> bool:
    """원천 테이블 접근 화이트리스트 (.env SOURCE_TABLE_ALLOWLIST) — 빈값이면 제한 없음.
    브라우저 조회·등록 검증·야간 적재가 전부 이 함수 하나를 거친다."""
    return (not config.SOURCE_TABLE_ALLOWLIST
            or table_name.upper() in config.SOURCE_TABLE_ALLOWLIST)


def browse_tables(cur) -> list:
    """등록 후보 테이블 목록 — Oracle 내부·우리 소유 테이블 제외, 화이트리스트 적용."""
    cur.execute("SELECT table_name FROM user_tables ORDER BY table_name")
    return [r[0] for r in cur.fetchall()
            if r[0] not in OUR_TABLES
            and not any(r[0].startswith(p) for p in _NOISE_PREFIX)
            and table_allowed(r[0])]


def validate(cur, table_name: str, id_column: str, ts_column: str,
             field_map: dict) -> str | None:
    """등록값 검증 — 문제가 있으면 사유 문자열, 없으면 None.

    테이블·컬럼 실존과 역할 어휘를 확인한다. 원천 훼손 위험이 없는 SELECT 검증뿐.
    """
    if not table_allowed(table_name):
        return (f"허용되지 않은 테이블입니다: {table_name} "
                "(.env SOURCE_TABLE_ALLOWLIST에 등록 필요)")
    cols = table_columns(cur, table_name)
    if not cols:
        return f"테이블이 없습니다: {table_name}"
    if id_column.upper() not in cols:
        return f"id 컬럼이 테이블에 없습니다: {id_column}"
    if ts_column and ts_column.upper() not in cols:
        return f"시간 컬럼이 테이블에 없습니다: {ts_column}"
    if not field_map:
        return "field_map(역할→컬럼 매핑)은 필수입니다"
    for role, col in field_map.items():
        if role not in ROLES:
            return f"허용되지 않은 역할: {role} (허용: {', '.join(ROLES)})"
        if not col or col.upper() not in cols:
            return f"역할 {role}의 컬럼이 테이블에 없습니다: {col}"
    if not any(r in field_map for r in TEXT_ROLES):
        return f"본문 역할({'/'.join(TEXT_ROLES)}) 중 최소 하나는 매핑해야 합니다"
    return None


def assemble_doc(row: dict) -> tuple:
    """원천 행(역할→값 dict)을 검색 문서로 조립 — (title, body, url).

    역할 조합이 조립 방식을 결정한다: 본문형은 body 그대로, QA형은 질문/답변 라벨링,
    meta(태그류)는 끝에 덧붙인다. 값은 이미 문자열로 읽힌 상태여야 한다.
    """
    parts = []
    if row.get("body"):
        parts.append(row["body"])
    if row.get("question"):
        parts.append("질문: " + row["question"])
    if row.get("answer"):
        parts.append("답변: " + row["answer"])
    if row.get("meta"):
        parts.append("태그: " + row["meta"])
    return (row.get("title") or "", "\n\n".join(parts), row.get("url") or "")


def list_sources(cur) -> list:
    ensure(cur)
    cur.execute("""SELECT source_name, table_name, id_column, ts_column, field_map,
                          content_kind, domain, enabled, last_ingest_ts,
                          NVL(url_enabled, 'Y')
                   FROM source_registry ORDER BY source_name""")
    return [{"source_name": r[0], "table_name": r[1], "id_column": r[2],
             "ts_column": r[3] or "", "field_map": json.loads(r[4]),
             "content_kind": r[5] or "", "domain": r[6] or "",
             "enabled": r[7] == "Y",
             "last_ingest_ts": r[8].isoformat() if r[8] else None,
             "url_enabled": r[9] == "Y"}
            for r in cur.fetchall()]


def upsert(cur, source_name: str, table_name: str, id_column: str, ts_column: str,
           field_map: dict, content_kind: str, enabled: bool, domain: str = "",
           url_enabled: bool = True):
    ensure(cur)
    cur.execute("""MERGE INTO source_registry s USING dual ON (s.source_name = :n)
                   WHEN MATCHED THEN UPDATE SET table_name = :t, id_column = :i,
                        ts_column = :ts, field_map = :f, content_kind = :k,
                        domain = :dm, enabled = :e, url_enabled = :ue
                   WHEN NOT MATCHED THEN INSERT
                        (source_name, table_name, id_column, ts_column, field_map,
                         content_kind, domain, enabled, url_enabled)
                   VALUES (:n, :t, :i, :ts, :f, :k, :dm, :e, :ue)""",
                {"n": source_name, "t": table_name.upper(), "i": id_column.upper(),
                 "ts": ts_column.upper() if ts_column else None,
                 "f": json.dumps({k: v.upper() for k, v in field_map.items()},
                                 ensure_ascii=False),
                 "k": content_kind or None, "dm": domain or None,
                 "e": "Y" if enabled else "N", "ue": "Y" if url_enabled else "N"})

def ensure_corpus(cur):
    """통합 코퍼스 테이블 + Oracle Text 인덱스 (멱등). 렉서는 blog_lexer를 공유.

    적재 배치뿐 아니라 서버 기동 시에도 호출된다 — 새 DB에서 배치 전에
    드라이런·검색이 먼저 와도 ORA-00942가 나지 않도록.
    """
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'CORPUS_DOCS'")
    if cur.fetchone()[0]:
        return
    # 렉서 프리퍼런스를 테이블보다 먼저 — 권한이 없으면 테이블도 안 만들어
    # 어중간한 상태(테이블만 있고 Text 인덱스 없음)를 남기지 않는다
    lexer = config.ORACLE_TEXT_LEXER
    if not re.fullmatch(r"[A-Za-z0-9_]+", lexer):
        raise ValueError(f"잘못된 ORACLE_TEXT_LEXER: {lexer!r}")
    try:
        cur.execute(f"""
            BEGIN
              BEGIN ctx_ddl.create_preference('blog_lexer', '{lexer}');
              EXCEPTION WHEN OTHERS THEN NULL;  -- 이미 있으면 그대로 사용
              END;
            END;
        """)
    except Exception as e:
        if "PLS-00201" in str(e) or "06550" in str(e):
            raise RuntimeError(
                "Oracle Text 권한이 없습니다 — 검색 인덱스 생성에 필요합니다.\n"
                "DBA 권한 계정에서 1회 실행하세요:\n"
                "  GRANT CTXAPP TO <앱 계정>;\n"
                "  GRANT EXECUTE ON CTXSYS.CTX_DDL TO <앱 계정>;") from e
        raise
    cur.execute("""CREATE TABLE corpus_docs (
        source_name VARCHAR2(100) NOT NULL,   -- source_registry.source_name
        src_id      VARCHAR2(200) NOT NULL,   -- 원천 테이블의 고유 id 값
        title       VARCHAR2(1000),
        body        CLOB,                     -- 역할 매핑으로 조립된 검색 문서
        kind        VARCHAR2(100),            -- content_kind (검색 라벨·프롬프트 힌트)
        url         VARCHAR2(1000),           -- 원문 참조 (url 역할, 있으면)
        embedding   BLOB,
        src_ts      TIMESTAMP,                -- 원천 ts_column 값 (있으면)
        created_at  TIMESTAMP DEFAULT SYSTIMESTAMP,
        updated_at  TIMESTAMP DEFAULT SYSTIMESTAMP,  -- 재청킹·재임베딩 신호
        graph_status VARCHAR2(20),            -- 구조화 상태 (doc_pipeline)
        graph_note   VARCHAR2(1000),
        PRIMARY KEY (source_name, src_id),
        CONSTRAINT corpus_docs_src_fk FOREIGN KEY (source_name)
          REFERENCES source_registry(source_name)
    )""")
    cur.execute("CREATE INDEX corpus_docs_status_ix ON corpus_docs (graph_status)")
    cur.execute("""
        CREATE INDEX corpus_docs_body_idx ON corpus_docs(body)
        INDEXTYPE IS CTXSYS.CONTEXT
        PARAMETERS ('LEXER blog_lexer SYNC (ON COMMIT)')
    """)
    print("corpus_docs 테이블 + Text 인덱스 생성")


def ensure_corpus_chunks(cur):
    """corpus_chunks 테이블 + corpus_docs.updated_at (멱등)."""
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'CORPUS_CHUNKS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE corpus_chunks (
            source_name VARCHAR2(100) NOT NULL,
            src_id      VARCHAR2(200) NOT NULL,
            chunk_no    NUMBER        NOT NULL,   -- 0부터, 문서 내 순서
            text        CLOB          NOT NULL,   -- title 접두 + 본문 슬라이스
            char_start  NUMBER,                   -- 본문 내 위치 (뷰 하이라이트용)
            char_end    NUMBER,
            embedding   BLOB,                     -- float32[] (백필 배치가 채움)
            embed_model VARCHAR2(200),            -- 이 벡터를 만든 모델 (모델 버저닝)
            created_at  TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT corpus_chunks_pk PRIMARY KEY (source_name, src_id, chunk_no),
            CONSTRAINT corpus_chunks_doc_fk FOREIGN KEY (source_name, src_id)
              REFERENCES corpus_docs(source_name, src_id) ON DELETE CASCADE)""")
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'CORPUS_DOCS' AND column_name = 'UPDATED_AT'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE corpus_docs ADD (updated_at TIMESTAMP)")
        cur.execute("UPDATE corpus_docs SET updated_at = created_at")
