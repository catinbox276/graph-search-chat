"""ORM 모델 — 스키마의 단일 선언 지점 (SQLAlchemy 2.x, Oracle 18c/19c 대상).

규약 (CLAUDE.md):
- 새 테이블·컬럼은 여기 선언 → `db.init_schema()`(create_all)가 생성한다.
- 단순 CRUD(레지스트리·계정·설정·기록)는 ORM으로, 복잡한 검색(RRF 융합은 SQLite 인메모리 인덱스)·MERGE 업서트·대량 배치·PL/SQL은 raw SQL 유지.
- 23ai/23.6 전환 시 VECTOR 컬럼은 여기 추가 (SQLAlchemy 2.0.41+ 네이티브 지원) —
  그때까지 임베딩은 BLOB + 인메모리 검색 (docs/schema.md §5.5).
- lg_checkpoints/lg_writes는 LangGraph 체크포인터 소유 — 모델로 선언하지 않는다.

기존 DB와의 관계: create_all은 이미 있는 테이블을 건드리지 않는다(이름 기준 스킵).
구버전 테이블의 컬럼 추가(ALTER) 마이그레이션은 각 모듈의 ensure가 담당 유지.
"""
from sqlalchemy import (CHAR, TIMESTAMP, CheckConstraint, Column, ForeignKey,
                        ForeignKeyConstraint, Identity, Index, Numeric,
                        String, Text, text)
from sqlalchemy.dialects.oracle import BLOB, CLOB
from sqlalchemy.orm import DeclarativeBase

_NOW = text("SYSTIMESTAMP")


class Base(DeclarativeBase):
    pass


# ── 시드 레지스트리 (사람 전용) ─────────────────────────────────
class DomainRegistry(Base):
    __tablename__ = "domain_registry"
    name = Column(String(100), primary_key=True)
    tools = Column(String(2000))          # 쉼표구분 도구명 — 이 도구를 쓰면 이 도메인
    priority = Column(Numeric, server_default=text("100"))  # 낮을수록 먼저, 최하순위 폴백
    extract_hint = Column(String(2000))   # 도메인별 추출 지침 (프롬프트 주입)
    scope = Column(String(10), server_default=text("'both'"))  # both|chat|doc
    created = Column(TIMESTAMP, server_default=_NOW)


class SourceRegistry(Base):
    __tablename__ = "source_registry"
    source_name = Column(String(100), primary_key=True)
    table_name = Column(String(128), nullable=False)   # 원천 테이블 (읽기 전용)
    id_column = Column(String(128), nullable=False)
    ts_column = Column(String(128))                     # 증분 워터마크 (없으면 전량 1회)
    field_map = Column(String(4000), nullable=False)    # JSON {역할: 컬럼}
    content_kind = Column(String(100))
    domain = Column(String(100),
                    ForeignKey("domain_registry.name", name="source_registry_domain_fk"))
    enabled = Column(CHAR(1), server_default=text("'Y'"))
    url_enabled = Column(CHAR(1), server_default=text("'Y'"))  # 원본 링크 노출 스위치
    last_ingest_ts = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP, server_default=_NOW)


# ── 통합 코퍼스 (검색 대상) ─────────────────────────────────────
class CorpusDoc(Base):
    __tablename__ = "corpus_docs"
    source_name = Column(String(100),
                         ForeignKey("source_registry.source_name",
                                    name="corpus_docs_src_fk"),
                         primary_key=True)
    src_id = Column(String(200), primary_key=True)
    title = Column(String(1000))
    body = Column(CLOB)          # 역할 매핑으로 조립된 검색 문서 (검색은 SQLite 인메모리 인덱스)
    kind = Column(String(100))
    url = Column(String(1000))
    embedding = Column(BLOB)     # 구버전 문서 단위 벡터 (검색은 청크 벡터 사용)
    src_ts = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP, server_default=_NOW)
    updated_at = Column(TIMESTAMP, server_default=_NOW)   # 재청킹·재임베딩 신호
    graph_status = Column(String(20))                     # done|excluded|error|NULL
    graph_note = Column(String(1000))
    __table_args__ = (Index("corpus_docs_status_ix", "graph_status"),)


class CorpusChunk(Base):
    __tablename__ = "corpus_chunks"
    source_name = Column(String(100), primary_key=True)
    src_id = Column(String(200), primary_key=True)
    chunk_no = Column(Numeric, primary_key=True)
    text_ = Column("text", CLOB, nullable=False)  # title 접두 + 본문 슬라이스
    char_start = Column(Numeric)
    char_end = Column(Numeric)
    embedding = Column(BLOB)                      # float32[] — 백필 배치가 채움
    embed_model = Column(String(200))             # 이 벡터를 만든 모델 (모델 버저닝)
    text_tokenized = Column(CLOB)                 # Kiwi 형태소(원형) 공백조인 — FTS5 렉시컬용
    #                                               ingestion/tokenize_corpus.py가 백필
    created_at = Column(TIMESTAMP, server_default=_NOW)
    __table_args__ = (
        ForeignKeyConstraint(["source_name", "src_id"],
                             ["corpus_docs.source_name", "corpus_docs.src_id"],
                             name="corpus_chunks_doc_fk", ondelete="CASCADE"),
    )


# ── 대화 기록 ───────────────────────────────────────────────────
class Session_(Base):
    __tablename__ = "sessions"
    id = Column(String(36), primary_key=True)
    turn = Column(Numeric, primary_key=True)
    ts = Column(TIMESTAMP, server_default=_NOW)
    question = Column(CLOB)
    tool_calls = Column(CLOB)
    answer = Column(CLOB)
    verdict = Column(String(20))       # success|fail|unknown|retracted (게이트 판정)
    user_id = Column(String(64))       # 자체 계정 로그인 사용자


# ── 지식그래프 4계층 ────────────────────────────────────────────
class Node(Base):
    __tablename__ = "nodes"
    id = Column(String(36), primary_key=True)
    layer = Column(Numeric(1), nullable=False)   # 1도메인 2목표 3접근법 4행동
    name = Column(String(400))
    embedding = Column(BLOB)                     # dedup·경로 진입점 매칭용
    fail_flag = Column(CHAR(1), server_default=text("'N'"))
    fail_reason = Column(String(1000))
    valid_from = Column(TIMESTAMP, server_default=_NOW)
    valid_to = Column(TIMESTAMP)                 # bi-temporal supersession


class Edge(Base):
    __tablename__ = "edges"
    src = Column(String(36),
                 ForeignKey("nodes.id", name="edges_src_fk", ondelete="CASCADE"),
                 primary_key=True)
    dst = Column(String(36),
                 ForeignKey("nodes.id", name="edges_dst_fk", ondelete="CASCADE"),
                 primary_key=True)
    weight = Column(Numeric, server_default=text("0"))     # 채택률 보정 가중치
    raw_count = Column(Numeric, server_default=text("0"))  # 원시 통행 수
    __table_args__ = (Index("edges_dst_ix", "dst"),)  # FK 캐스케이드 삭제 성능


class NodeEvidence(Base):
    __tablename__ = "node_evidence"
    node_id = Column(String(36),
                     ForeignKey("nodes.id", name="node_evidence_node_fk",
                                ondelete="CASCADE"),
                     primary_key=True)
    kind = Column(String(10), primary_key=True)   # session|doc
    ref = Column(String(400), primary_key=True)   # 세션id 또는 소스명:원천id
    __table_args__ = (CheckConstraint("kind IN ('session','doc')",
                                      name="node_evidence_kind_ck"),)


class Suggestion(Base):
    __tablename__ = "suggestions"
    id = Column(Numeric, Identity(always=True), primary_key=True)
    ts = Column(TIMESTAMP, server_default=_NOW)
    problem = Column(String(2000))
    node_id = Column(String(36),
                     ForeignKey("nodes.id", name="suggestions_node_fk",
                                ondelete="CASCADE"),
                     nullable=False)
    weight = Column(Numeric)
    session_id = Column(String(36))
    adopted = Column(CHAR(1))          # 노출 대비 채택률 보정 재료


# ── 운영 레지스트리·설정·계정 ────────────────────────────────────
class ModelRegistry(Base):
    __tablename__ = "model_registry"
    kind = Column(String(20), primary_key=True)    # llm|embedding|reranker
    name = Column(String(200), primary_key=True)   # served-model-name
    enabled = Column(CHAR(1), server_default=text("'Y'"))
    is_default = Column(CHAR(1), server_default=text("'N'"))
    base_url = Column(String(500))                 # 빈값=역할별 .env 폴백
    registered = Column(TIMESTAMP, server_default=_NOW)


class McpRegistry(Base):
    __tablename__ = "mcp_registry"
    name = Column(String(100), primary_key=True)
    transport = Column(String(20), server_default=text("'streamable_http'"))
    url = Column(String(500))       # http 계열 전용
    command = Column(String(500))   # stdio 전용
    enabled = Column(CHAR(1), server_default=text("'Y'"))
    created = Column(TIMESTAMP, server_default=_NOW)
    __table_args__ = (CheckConstraint(
        "transport IN ('streamable_http', 'sse', 'stdio')",
        name="mcp_transport_ck"),)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key = Column(String(100), primary_key=True)
    value = Column(CLOB)
    updated = Column(TIMESTAMP, server_default=_NOW)


class AppUser(Base):
    __tablename__ = "app_users"
    user_id = Column(String(64), primary_key=True)
    pw_hash = Column(String(200), nullable=False)
    approved = Column(CHAR(1), server_default=text("'N'"))
    is_admin = Column(CHAR(1), server_default=text("'N'"))
    created_at = Column(TIMESTAMP, server_default=_NOW)
    approved_at = Column(TIMESTAMP)


class AppEvent(Base):
    """활동 로그 — 정상·비정상 전부 (core/events.py). 보관 기간 회전으로 무한 증가 방지.

    kind: request(웹 요청) · tool(에이전트 도구) · batch(야간 배치) · admin(관리 행동)
          · model(모델 호출) · error(미처리 예외). level: info | warn | error.
    """
    __tablename__ = "app_events"
    id = Column(Numeric, Identity(always=True), primary_key=True)
    ts = Column(TIMESTAMP, server_default=_NOW)
    kind = Column(String(20))
    level = Column("lvl", String(10), server_default=text("'info'"))
    source = Column(String(200))      # 경로 / 배치명 / 도구명
    actor = Column(String(64))        # 사용자 id
    ref = Column(String(200))         # 세션 id / 문서 id
    status = Column(String(20))       # HTTP 코드 / ok / fail
    duration_ms = Column(Numeric)
    summary = Column(String(1000))
    detail = Column(CLOB)             # 스택트레이스 / 인자 / 컨텍스트
    __table_args__ = (Index("app_events_ts_ix", "ts"),
                      Index("app_events_kind_ix", "kind", "lvl"))
