"""등록된 원천 테이블 → corpus_docs 증분 적재 (docs/integration.md 접점 2).

- source_registry의 enabled 소스마다: ts_column 워터마크 이후 신규분만 SELECT,
  역할 매핑(source_registry.assemble_doc)으로 검색 문서를 조립해 corpus_docs에 MERGE.
- 원천 테이블은 저쪽 소유 — 이 스크립트는 원천에 SELECT만 날린다 (쓰기 금지).
- ts_column이 없는 소스는 전량 1회형: 최초 실행에만 적재하고 이후 스킵.
- blog_posts(소스 1호) 최초 이관은 특례 — 임베딩까지 SQL로 복사해 재계산을 피한다.
- 신규 문서의 임베딩은 ingestion/embed_corpus.py(03:30 배치)가 corpus_docs 기준으로 백필.
usage: .venv/bin/python ingestion/ingest_sources.py   (야간 CronJob 03:10과 동일)
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import oracledb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from core import config  # noqa: E402
from ingestion import source_registry  # noqa: E402

oracledb.defaults.fetch_lobs = False  # CLOB을 str로 바로 받는다
BATCH = 500


def _ident(name: str) -> str:
    """레지스트리 값이라도 SQL에 끼워 넣기 전 식별자 형식을 재검증 (2차 방어)."""
    if not re.fullmatch(r"[A-Za-z0-9_$#]+", name):
        raise ValueError(f"잘못된 식별자: {name!r}")
    return name


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
        # list_sources는 워터마크를 isoformat 문자열로 준다 — datetime으로 되돌려야
        # oracledb가 네이티브 TIMESTAMP로 바인딩(문자열이면 NLS 변환 → ORA-01843).
        w = src["last_ingest_ts"]
        binds["w"] = datetime.fromisoformat(w) if isinstance(w, str) else w
    if tsc:
        sql += f" ORDER BY {tsc}"
    if "w" in binds:
        # 기본 바인딩은 datetime을 DATE(초 정밀)로 잘라 같은 초의 행을 매번 재스캔한다 —
        # TIMESTAMP로 바인딩해 마이크로초까지 비교. (아래 워터마크 저장도 동일)
        cur.setinputsizes(w=oracledb.DB_TYPE_TIMESTAMP)
    cur.execute(sql, binds)

    n, max_ts, batch = 0, None, []
    off = 2 if tsc else 1
    # body는 CLOB — 같은 바인드(:b)를 UPDATE·INSERT 두 곳에 쓰면 본문이 길 때
    # ORA-22284(duplicate long binds). 서로 다른 이름(:b_u/:b_i)으로 분리한다.
    merge = """MERGE INTO corpus_docs c
               USING (SELECT :sn AS sn, :sid AS sid FROM dual) x
               ON (c.source_name = x.sn AND c.src_id = x.sid)
               WHEN MATCHED THEN UPDATE SET title = :t, body = :b_u, kind = :k,
                    url = :u, src_ts = :ts, embedding = NULL,
                    updated_at = SYSTIMESTAMP  -- 본문 변경 → 재청킹·재임베딩 신호
               WHEN NOT MATCHED THEN INSERT
                    (source_name, src_id, title, body, kind, url, src_ts, updated_at)
               VALUES (:sn, :sid, :t, :b_i, :k, :u, :ts, SYSTIMESTAMP)"""

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
                      "b_u": body, "b_i": body, "k": src["content_kind"] or None,
                      "u": url[:1000] or None, "ts": rts})
        if rts and (max_ts is None or rts > max_ts):
            max_ts = rts
        n += 1
        if len(batch) >= BATCH:
            flush()
    flush()
    cur.setinputsizes(w=oracledb.DB_TYPE_TIMESTAMP)  # 마이크로초 보존 저장 (위 주석 참조)
    cur.execute("""UPDATE source_registry
                   SET last_ingest_ts = NVL(:w, SYSTIMESTAMP)
                   WHERE source_name = :n""",
                {"w": max_ts, "n": src["source_name"]})
    return n


def main():
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    source_registry.ensure(cur)
    source_registry.ensure_corpus(cur)
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
    import time as _t
    from core import events as _ev
    _t0 = _t.time()
    try:
        main()
        _ev.log("batch", source="ingest-sources", level="info", status="ok",
                duration_ms=int((_t.time() - _t0) * 1000), summary="ingest-sources 완료")
    except Exception as _e:
        import traceback as _tb
        _ev.log("batch", source="ingest-sources", level="error", status="fail",
                duration_ms=int((_t.time() - _t0) * 1000),
                summary=f"{type(_e).__name__}: {str(_e)[:200]}",
                detail=_tb.format_exc())
        raise
