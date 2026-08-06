"""등록된 원천 테이블 → corpus_docs 증분 적재 (docs/integration.md 접점 2).

- source_registry의 enabled 소스마다: ts_column 워터마크 이후 신규분만 SELECT,
  역할 매핑(source_registry.assemble_doc)으로 검색 문서를 조립해 corpus_docs에 MERGE.
- 원천 테이블은 저쪽 소유 — 이 스크립트는 원천에 SELECT만 날린다 (쓰기 금지).
- ts_column이 없는 소스는 전량 1회형: 최초 실행에만 적재하고 이후 스킵.
- blog_posts(소스 1호) 최초 이관은 특례 — 임베딩까지 SQL로 복사해 재계산을 피한다.
- 신규 문서의 임베딩은 scripts/embed_corpus.py(03:30 배치)가 corpus_docs 기준으로 백필.
usage: .venv/bin/python scripts/ingest_sources.py   (야간 CronJob 03:10과 동일)
"""
import re
import sys
from pathlib import Path

import oracledb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from tools import config, source_registry  # noqa: E402
from tools.blog_search import DSN, PASSWORD, USER  # noqa: E402

oracledb.defaults.fetch_lobs = False  # CLOB을 str로 바로 받는다
BATCH = 500


def _ident(name: str) -> str:
    """레지스트리 값이라도 SQL에 끼워 넣기 전 식별자 형식을 재검증 (2차 방어)."""
    if not re.fullmatch(r"[A-Za-z0-9_$#]+", name):
        raise ValueError(f"잘못된 식별자: {name!r}")
    return name


def ensure_corpus(cur):
    """통합 코퍼스 테이블 + Oracle Text 인덱스 (멱등). 렉서는 blog_lexer를 공유."""
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'CORPUS_DOCS'")
    if cur.fetchone()[0]:
        return
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
    # 렉서 프리퍼런스: load_oracle.py가 만든 blog_lexer 재사용, 없으면 생성
    lexer = config.ORACLE_TEXT_LEXER
    if not re.fullmatch(r"[A-Za-z0-9_]+", lexer):
        raise ValueError(f"잘못된 ORACLE_TEXT_LEXER: {lexer!r}")
    cur.execute(f"""
        BEGIN
          BEGIN ctx_ddl.create_preference('blog_lexer', '{lexer}');
          EXCEPTION WHEN OTHERS THEN NULL;  -- 이미 있으면 그대로 사용
          END;
        END;
    """)
    cur.execute("""
        CREATE INDEX corpus_docs_body_idx ON corpus_docs(body)
        INDEXTYPE IS CTXSYS.CONTEXT
        PARAMETERS ('LEXER blog_lexer SYNC (ON COMMIT)')
    """)
    print("corpus_docs 테이블 + Text 인덱스 생성")


def migrate_blog_posts(cur, src) -> int:
    """소스 1호 특례: blog_posts → corpus_docs 최초 이관을 임베딩까지 SQL 복사.

    임베딩은 '제목+본문 앞 N자'로 계산된 것이라 본문을 변형 없이 옮겨야 유효하다
    (assemble_doc의 태그 덧붙임을 생략하는 이유). 재계산 0건.
    """
    cur.execute("SELECT COUNT(*) FROM corpus_docs WHERE source_name = :1",
                [src["source_name"]])
    if cur.fetchone()[0]:
        return 0
    cur.execute("""
        INSERT INTO corpus_docs (source_name, src_id, title, body, kind, url, embedding)
        SELECT :sn, id, title, body, :kind, url, embedding FROM blog_posts""",
                {"sn": src["source_name"], "kind": src["content_kind"] or None})
    return cur.rowcount


def ingest_source(cur, src) -> int:
    """일반 경로: 워터마크 이후 신규분을 역할 매핑으로 조립해 MERGE."""
    tbl = _ident(src["table_name"])
    idc = _ident(src["id_column"])
    tsc = _ident(src["ts_column"]) if src["ts_column"] else None
    fmap = {role: _ident(col) for role, col in src["field_map"].items()}

    if not tsc and src["last_ingest_ts"]:
        return 0  # 전량 1회형 — 이미 적재됨

    roles = list(fmap)
    cols = ", ".join([idc] + ([tsc] if tsc else []) + [fmap[r] for r in roles])
    sql = f"SELECT {cols} FROM {tbl}"
    binds = {}
    if tsc and src["last_ingest_ts"]:
        sql += f" WHERE {tsc} > :w"
        binds["w"] = src["last_ingest_ts"]
    if tsc:
        sql += f" ORDER BY {tsc}"
    cur.execute(sql, binds)

    n, max_ts, batch = 0, None, []
    off = 2 if tsc else 1
    merge = """MERGE INTO corpus_docs c
               USING (SELECT :sn AS sn, :sid AS sid FROM dual) x
               ON (c.source_name = x.sn AND c.src_id = x.sid)
               WHEN MATCHED THEN UPDATE SET title = :t, body = :b, kind = :k,
                    url = :u, src_ts = :ts, embedding = NULL,
                    updated_at = SYSTIMESTAMP  -- 본문 변경 → 재청킹·재임베딩 신호
               WHEN NOT MATCHED THEN INSERT
                    (source_name, src_id, title, body, kind, url, src_ts, updated_at)
               VALUES (:sn, :sid, :t, :b, :k, :u, :ts, SYSTIMESTAMP)"""

    def flush():
        nonlocal batch
        if batch:
            cur.executemany(merge, batch)
            batch = []

    for row in cur.fetchall():
        rid, rts = str(row[0]), (row[1] if tsc else None)
        vals = {r: (str(row[off + i]) if row[off + i] is not None else "")
                for i, r in enumerate(roles)}
        title, body, url = source_registry.assemble_doc(vals)
        if not body.strip():
            continue  # 본문 없는 행은 검색 문서가 못 됨
        batch.append({"sn": src["source_name"], "sid": rid[:200], "t": title[:1000],
                      "b": body, "k": src["content_kind"] or None,
                      "u": url[:1000] or None, "ts": rts})
        if rts and (max_ts is None or rts > max_ts):
            max_ts = rts
        n += 1
        if len(batch) >= BATCH:
            flush()
    flush()
    cur.execute("""UPDATE source_registry
                   SET last_ingest_ts = NVL(:w, SYSTIMESTAMP)
                   WHERE source_name = :n""",
                {"w": max_ts, "n": src["source_name"]})
    return n


def main():
    con = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = con.cursor()
    source_registry.ensure(cur)
    ensure_corpus(cur)
    total = 0
    for src in source_registry.list_sources(cur):
        if not src["enabled"]:
            continue
        if not source_registry.table_allowed(src["table_name"]):
            # allowlist가 등록 후 좁아진 경우 — 등록돼 있어도 적재 차단
            print(f"[{src['source_name']}] 건너뜀 — 허용되지 않은 테이블: {src['table_name']}")
            continue
        if src["source_name"] == "blog_posts" and not src["last_ingest_ts"]:
            n = migrate_blog_posts(cur, src)
            if n:
                cur.execute("""UPDATE source_registry SET last_ingest_ts = SYSTIMESTAMP
                               WHERE source_name = :1""", [src["source_name"]])
                print(f"[{src['source_name']}] 최초 이관 {n}건 (임베딩 SQL 복사)")
                con.commit()
                total += n
                continue
        n = ingest_source(cur, src)
        con.commit()
        print(f"[{src['source_name']}] 적재 {n}건")
        total += n
    cur.execute("""SELECT COUNT(*), COUNT(CASE WHEN embedding IS NOT NULL THEN 1 END)
                   FROM corpus_docs""")
    c, e = cur.fetchone()
    print(f"완료: 신규 {total}건 / corpus_docs 총 {c}건 (임베딩 {e}건 — 나머지는 03:30 백필)")
    con.close()


if __name__ == "__main__":
    main()
