"""그래프 저장소 DDL + 1층 도메인 닫힌 목록(시드·분류).

- ddl: nodes/edges/node_evidence 생성 + 구버전 ALTER (멱등)
- ensure_domain_registry: 도메인 닫힌 목록 테이블 + 기본 2종 시드
- classify_domain: 세션의 1층 분류 — LLM이 아니라 도구 사용으로 결정적으로
"""
from core import config  # noqa: F401 — 시그니처 일관용 (현재 직접 참조 없음)

DATAHUB_TOOLS = {"search", "get_entities", "list_schema_fields", "get_lineage",
                 "get_lineage_paths_between", "get_dataset_queries"}

# 기본 도메인 시드: (이름, 도구 csv, 우선순위, 추출 지침)
# 추출 지침은 이 도메인으로 분류된 세션의 목표·접근법 추출 프롬프트에 그대로 주입된다.
SEED_DOMAINS = (
    ("데이터 조회", None, 1,  # tools=None → DATAHUB_TOOLS에서 채움
     "목표는 데이터 탐색 의도(무엇을 찾고/조인하고/추적하려 했나)로, "
     "접근법은 도구+방법(테이블 탐색, 스키마 확인, 조인 키, 리니지)으로 일반화하라"),
    ("사내 노하우", "search_docs,read_doc", 2,
     "목표는 해결하려던 문제 증상으로, 접근법은 검색으로 찾은 해법의 핵심 조치로 일반화하라"),
)


def ddl(cur):
    for stmt in (
        """CREATE TABLE nodes (
             id VARCHAR2(36) PRIMARY KEY, layer NUMBER(1) NOT NULL,
             name VARCHAR2(400), embedding BLOB,
             fail_flag CHAR(1) DEFAULT 'N', fail_reason VARCHAR2(1000),
             valid_from TIMESTAMP DEFAULT SYSTIMESTAMP, valid_to TIMESTAMP)""",
        """CREATE TABLE edges (
             src VARCHAR2(36) NOT NULL, dst VARCHAR2(36) NOT NULL,
             weight NUMBER DEFAULT 0, raw_count NUMBER DEFAULT 0,
             PRIMARY KEY (src, dst),
             CONSTRAINT edges_src_fk FOREIGN KEY (src)
               REFERENCES nodes(id) ON DELETE CASCADE,
             CONSTRAINT edges_dst_fk FOREIGN KEY (dst)
               REFERENCES nodes(id) ON DELETE CASCADE)""",
        """CREATE TABLE node_evidence (
             node_id VARCHAR2(36) NOT NULL,
             kind VARCHAR2(10) NOT NULL CHECK (kind IN ('session','doc')),
             ref VARCHAR2(400) NOT NULL,
             CONSTRAINT node_evidence_pk PRIMARY KEY (node_id, kind, ref),
             CONSTRAINT node_evidence_node_fk FOREIGN KEY (node_id)
               REFERENCES nodes(id) ON DELETE CASCADE)""",
    ):
        table = stmt.split()[2]
        cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :1",
                    [table.upper()])
        if not cur.fetchone()[0]:
            cur.execute(stmt)
    # FK 캐스케이드 삭제 성능용 (dst는 PK 선두가 아님)
    cur.execute("SELECT COUNT(*) FROM user_indexes WHERE index_name = 'EDGES_DST_IX'")
    if not cur.fetchone()[0]:
        cur.execute("CREATE INDEX edges_dst_ix ON edges (dst)")
    # 구버전 sessions 테이블에 ts가 없으면 추가 (신호 계산·재발 판정에 필요)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SESSIONS' AND column_name = 'TS'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE sessions ADD (ts TIMESTAMP DEFAULT SYSTIMESTAMP)")
    # user_id(SSO 로그인)가 없으면 추가 — 재발 판정을 사용자 단위로 매칭하는 데 쓴다
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SESSIONS' AND column_name = 'USER_ID'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE sessions ADD (user_id VARCHAR2(64))")
    # judged_with — 이 세션을 어떤 (엔티티·클러스터 라인 버전)으로 판정했는지 기록.
    # 재현성 최소 단위 — 세션 run(활성 전환) 기계 없이도 귀속이 남는다.
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SESSIONS' AND column_name = 'JUDGED_WITH'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE sessions ADD (judged_with VARCHAR2(200))")
    # entity_type — 관리자 정의 타입 엔티티(layer 5)의 타입 라벨 (코어 1~4층은 NULL).
    # 범용 노드 + 타입 라벨 패턴 (Graphiti/GraphRAG 방식 — DDL 반복 없이 타입 확장)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'NODES' AND column_name = 'ENTITY_TYPE'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE nodes ADD (entity_type VARCHAR2(100))")
    # entity_relations — 타입드 관계 (Graphiti edge_type_map 포팅). 카운트 없는 존재 기반:
    # 활성 여부는 조회 시 run 스코핑(doc_runs.active), 회수는 ref 단위 DELETE — ±1 기계 불필요.
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'ENTITY_RELATIONS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE entity_relations (
            src     VARCHAR2(36) NOT NULL,
            dst     VARCHAR2(36) NOT NULL,
            rtype   VARCHAR2(64) NOT NULL,
            ref     VARCHAR2(400) NOT NULL,
            run_id  VARCHAR2(32) DEFAULT '-' NOT NULL,
            created TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT entity_relations_pk PRIMARY KEY (src, dst, rtype, ref, run_id),
            CONSTRAINT entity_relations_src_fk FOREIGN KEY (src)
              REFERENCES nodes(id) ON DELETE CASCADE,
            CONSTRAINT entity_relations_dst_fk FOREIGN KEY (dst)
              REFERENCES nodes(id) ON DELETE CASCADE)""")
        cur.execute("CREATE INDEX entity_relations_dst_ix ON entity_relations (dst)")
    ensure_domain_registry(cur)
    from graph.doc_pipeline.runs import ensure_runs  # 지연 import — 순환 방지
    ensure_runs(cur)


def ensure_domain_registry(cur):
    """1층 도메인의 닫힌 목록 저장소 — 없으면 만들고 기본 2종을 시드.

    확장은 사람만 한다(관리자 API /admin/domains 또는 SQL). LLM에게 쓰기 경로 없음.
    design §2 결정 1(위는 닫고 아래는 연다)의 '닫힌 목록'이 코드 하드코딩에서
    이 테이블로 옮겨진 것 — 도메인 추가에 재배포가 필요 없어진다.
    extract_hint = 도메인별 추출 지침. 분류(도구 대조)는 코드가, 표현(목표·접근법을
    어떻게 일반화할지)은 이 지침이 프롬프트에 실려 LLM에 전달된다.
    """
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DOMAIN_REGISTRY'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE domain_registry (
            name         VARCHAR2(100) PRIMARY KEY,
            tools        VARCHAR2(2000),           -- 쉼표구분 도구명: 이 도구를 쓰면 이 도메인
            priority     NUMBER DEFAULT 100,       -- 낮을수록 먼저 대조. 최하순위가 폴백
            extract_hint VARCHAR2(2000),           -- 도메인별 추출 지침 (프롬프트 주입)
            scope        VARCHAR2(10) DEFAULT 'both',  -- 사용 목적: both|chat(대화 전용)|doc(문서 전용)
            created      TIMESTAMP DEFAULT SYSTIMESTAMP)""")
        for name, tools, prio, hint in SEED_DOMAINS:
            cur.execute("INSERT INTO domain_registry (name, tools, priority, extract_hint) "
                        "VALUES (:1, :2, :3, :4)",
                        [name, tools or ",".join(sorted(DATAHUB_TOOLS)), prio, hint])
        return
    # 기존 테이블에 extract_hint가 없으면 추가하고 기본 시드 지침을 백필
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'DOMAIN_REGISTRY' AND column_name = 'EXTRACT_HINT'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE domain_registry ADD (extract_hint VARCHAR2(2000))")
        for name, _tools, _prio, hint in SEED_DOMAINS:
            cur.execute("UPDATE domain_registry SET extract_hint = :1 "
                        "WHERE name = :2 AND extract_hint IS NULL", [hint, name])
    # 사용 목적(scope) 컬럼 — 등록 때 대화/문서/둘 다를 명시 선택 (기존 행은 both)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'DOMAIN_REGISTRY' AND column_name = 'SCOPE'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE domain_registry ADD (scope VARCHAR2(10) DEFAULT 'both')")
        cur.execute("UPDATE domain_registry SET scope = 'both' WHERE scope IS NULL")


def classify_domain(cur, tool_names):
    """닫힌 1층 분류 — LLM이 아니라 도구 사용으로 결정적으로. priority 순 첫 매칭,
    매칭 없으면 최하순위 도메인(범용 폴백). 반환: (도메인명, 추출 지침).

    사용 목적(scope)이 doc(문서 전용)인 도메인은 대화 분류·폴백에서 제외 —
    소스 구조화용 도메인이 최하순위 폴백이 되어 대화를 먹는 사고 방지.
    """
    cur.execute("""SELECT name, tools, extract_hint FROM domain_registry
                   WHERE NVL(scope, 'both') != 'doc' ORDER BY priority, name""")
    rows = [(n, t, h) for n, t, h in cur.fetchall() if (t or "").strip()]
    for name, tools, hint in rows:
        if tool_names & {t.strip() for t in tools.split(",") if t.strip()}:
            return name, (hint or "")
    return (rows[-1][0], rows[-1][2] or "") if rows else ("사내 노하우", "")
