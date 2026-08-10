"""스키마 v2 정리 마이그레이션 (개발 단계 1회성) — docs/schema.md 무결성 모델 반영.

- node_evidence: 다형 참조(session_id에 세션id 또는 'doc:...') 제거
  → (node_id, kind, ref) + PK + nodes FK(캐스케이드)
- edges: nodes FK(캐스케이드) + dst 인덱스, 고아 엣지 선삭제
- suggestions: identity PK + node FK(캐스케이드) + 인덱스, session_id 폭 36 통일
- corpus_docs -> source_registry FK, source_registry.domain -> domain_registry FK
- 인덱스: corpus_docs(graph_status), sessions(user_id)

멱등: 이미 적용된 항목은 건너뜀.
usage: python scripts/migrate_schema_v2.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb

from tools import config


def has_constraint(cur, name):
    cur.execute("SELECT COUNT(*) FROM user_constraints WHERE constraint_name = :1",
                [name.upper()])
    return bool(cur.fetchone()[0])


def has_index(cur, name):
    cur.execute("SELECT COUNT(*) FROM user_indexes WHERE index_name = :1", [name.upper()])
    return bool(cur.fetchone()[0])


def has_column(cur, table, col):
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = :1 AND column_name = :2""", [table, col])
    return bool(cur.fetchone()[0])


def main():
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()

    # 1) node_evidence 재구축 (다형 참조 -> kind/ref)
    if has_column(cur, "NODE_EVIDENCE", "SESSION_ID"):
        print("[1] node_evidence 재구축 (kind/ref)")
        cur.execute("""CREATE TABLE node_evidence_v2 (
            node_id VARCHAR2(36) NOT NULL,
            kind VARCHAR2(10) NOT NULL CHECK (kind IN ('session','doc')),
            ref VARCHAR2(400) NOT NULL,
            CONSTRAINT node_evidence_pk PRIMARY KEY (node_id, kind, ref),
            CONSTRAINT node_evidence_node_fk FOREIGN KEY (node_id)
              REFERENCES nodes(id) ON DELETE CASCADE)""")
        cur.execute("""INSERT INTO node_evidence_v2 (node_id, kind, ref)
            SELECT DISTINCT e.node_id,
                   CASE WHEN e.session_id LIKE 'doc:%' THEN 'doc' ELSE 'session' END,
                   CASE WHEN e.session_id LIKE 'doc:%' THEN SUBSTR(e.session_id, 5)
                        ELSE e.session_id END
            FROM node_evidence e
            WHERE e.node_id IN (SELECT id FROM nodes)""")  # 고아 증거는 버림
        moved = cur.rowcount
        cur.execute("SELECT COUNT(*) FROM node_evidence")
        orig = cur.fetchone()[0]
        cur.execute("DROP TABLE node_evidence PURGE")
        cur.execute("ALTER TABLE node_evidence_v2 RENAME TO node_evidence")
        print(f"    {orig}행 -> {moved}행 (중복·고아 제거분 차이)")
    else:
        print("[1] node_evidence 이미 v2 — 건너뜀")

    # 2) edges FK + dst 인덱스 (고아 엣지 선삭제)
    if not has_constraint(cur, "EDGES_SRC_FK"):
        print("[2] edges FK 추가")
        cur.execute("""DELETE FROM edges WHERE src NOT IN (SELECT id FROM nodes)
                       OR dst NOT IN (SELECT id FROM nodes)""")
        if cur.rowcount:
            print(f"    고아 엣지 {cur.rowcount}건 삭제")
        cur.execute("""ALTER TABLE edges ADD CONSTRAINT edges_src_fk
                       FOREIGN KEY (src) REFERENCES nodes(id) ON DELETE CASCADE""")
        cur.execute("""ALTER TABLE edges ADD CONSTRAINT edges_dst_fk
                       FOREIGN KEY (dst) REFERENCES nodes(id) ON DELETE CASCADE""")
    if not has_index(cur, "EDGES_DST_IX"):
        cur.execute("CREATE INDEX edges_dst_ix ON edges (dst)")

    # 3) suggestions 재구축 (identity PK + FK + 인덱스)
    if not has_column(cur, "SUGGESTIONS", "ID"):
        print("[3] suggestions 재구축 (identity PK)")
        cur.execute("""CREATE TABLE suggestions_v2 (
            id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ts TIMESTAMP DEFAULT SYSTIMESTAMP, problem VARCHAR2(2000),
            node_id VARCHAR2(36) NOT NULL, weight NUMBER,
            session_id VARCHAR2(36), adopted CHAR(1),
            CONSTRAINT suggestions_node_fk FOREIGN KEY (node_id)
              REFERENCES nodes(id) ON DELETE CASCADE)""")
        cur.execute("""INSERT INTO suggestions_v2
                       (ts, problem, node_id, weight, session_id, adopted)
            SELECT ts, problem, node_id, weight, SUBSTR(session_id, 1, 36), adopted
            FROM suggestions WHERE node_id IN (SELECT id FROM nodes)""")
        print(f"    {cur.rowcount}행 이전 (고아 노드 참조분 제외)")
        cur.execute("DROP TABLE suggestions PURGE")
        cur.execute("ALTER TABLE suggestions_v2 RENAME TO suggestions")
        cur.execute("CREATE INDEX suggestions_session_ix ON suggestions (session_id)")
        cur.execute("CREATE INDEX suggestions_node_ix ON suggestions (node_id)")
    else:
        print("[3] suggestions 이미 v2 — 건너뜀")

    # 4) 설정 체인 FK
    if not has_constraint(cur, "CORPUS_DOCS_SRC_FK"):
        print("[4] corpus_docs -> source_registry FK")
        cur.execute("""ALTER TABLE corpus_docs ADD CONSTRAINT corpus_docs_src_fk
                       FOREIGN KEY (source_name) REFERENCES source_registry(source_name)""")
    if not has_constraint(cur, "SOURCE_REGISTRY_DOMAIN_FK"):
        print("[4] source_registry.domain -> domain_registry FK")
        cur.execute("""ALTER TABLE source_registry ADD CONSTRAINT
                       source_registry_domain_fk FOREIGN KEY (domain)
                       REFERENCES domain_registry(name)""")

    # 5) 접근 경로 인덱스
    for ix, stmt in (
        ("CORPUS_DOCS_STATUS_IX", "CREATE INDEX corpus_docs_status_ix ON corpus_docs (graph_status)"),
        ("SESSIONS_USER_IX", "CREATE INDEX sessions_user_ix ON sessions (user_id, ts)"),
    ):
        if not has_index(cur, ix):
            print(f"[5] 인덱스 {ix}")
            cur.execute(stmt)

    con.commit()
    # 검증 요약
    for t in ("node_evidence", "edges", "suggestions"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {t}: {cur.fetchone()[0]}행")
    cur.execute("""SELECT constraint_name FROM user_constraints
                   WHERE constraint_type = 'R' ORDER BY constraint_name""")
    print("  FK:", [r[0] for r in cur.fetchall()])
    con.close()
    print("마이그레이션 완료")


if __name__ == "__main__":
    main()
