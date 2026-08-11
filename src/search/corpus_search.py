"""코퍼스 검색 함수 툴 — SQLite 인메모리 하이브리드 (FTS5 BM25 + sqlite-vec).

- 검색 대상은 통합 코퍼스 corpus_docs 단일 (등록 소스들을 ingestion/ingest_sources.py가
  조립). 특정 원천 테이블 직조회 없음 — 소스 추가는 코드가 아니라 source_registry 등록.
- 원본·임베딩은 Oracle이 진실 소스(corpus_docs·corpus_chunks). 검색 인덱스는
  기동 시 Oracle → SQLite :memory:로 빌드(search/inmemory_index.py) — Oracle Text 권한 불필요.
- 검색: FTS5(lexical) top-30 + sqlite-vec 코사인(semantic) top-30 → 문서 best-chunk 집계 → RRF.
- 임베딩이 아직 없으면 lexical(FTS5) 단독으로 동작 (백필 진행 중에도 사용 가능).
- 문서 id: "소스명:원천id" 단일 형식.

에이전트에 함수 툴로 직접 등록해서 쓴다 (MCP 아님 — MCP는 DataHub 공식만 사용).
"""
import oracledb
from core import config

DSN, USER, PASSWORD = (config.ORACLE_DSN, config.ORACLE_USER,
                       config.ORACLE_PASSWORD)  # 모듈 내부용 — 외부는 config 직접 참조
RRF_K = config.RRF_K

# 원본·스니펫 조회용 Oracle 풀 (검색 랭킹은 SQLite 인메모리 인덱스가 담당)
_pool = oracledb.create_pool(user=USER, password=PASSWORD, dsn=DSN,
                             min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                             increment=config.ORACLE_POOL_INCREMENT)


def reload_index():
    """인메모리 검색 인덱스를 Oracle에서 재빌드 (기동 시·임베딩 모델 교체·/reload).
    반환: 인덱싱된 청크 수."""
    from search import inmemory_index as ix
    return ix.build_index()


def search_docs(query: str, limit: int = 5) -> str:
    """사내 지식 코퍼스(등록된 모든 원천 통합)를 키워드+시맨틱 하이브리드로 검색한다.

    Args:
        query: 검색어 (한국어/영어, 자연어 가능)
        limit: 최대 결과 수 (기본 5)
    """
    return _search(query, limit)


def _search(query: str, limit: int = 5, source: str = "") -> str:
    from search import inmemory_index as ix
    ix.ensure_fresh()
    lex = ix.lexical(query, config.SEARCH_TOP_LEXICAL, source)
    sem, best_chunk = ix.semantic(query, config.SEARCH_TOP_SEMANTIC, source)
    scores = {}
    for rank_list in (lex, sem):
        for r, pid in enumerate(rank_list):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + r + 1)
    top = sorted(scores, key=scores.get, reverse=True)[:limit]
    if not top:
        return "검색 결과가 없습니다."
    out = []
    with _pool.acquire() as con:
        cur = con.cursor()
        for pid in top:
            tag = ("L+S" if pid in lex and pid in sem
                   else "L" if pid in lex else "S")
            src, _, sid = pid.partition(":")
            cur.execute(
                """SELECT d.title, d.kind,
                          CASE WHEN NVL(r.url_enabled, 'Y') = 'Y' THEN d.url END,
                          dbms_lob.substr(d.body, 200, 1)
                   FROM corpus_docs d
                   JOIN source_registry r ON r.source_name = d.source_name
                   WHERE d.source_name = :1 AND d.src_id = :2""",
                [src, sid])
            t, kind, url, snip = cur.fetchone()
            if pid in best_chunk:  # 시맨틱 매칭 — 실제로 맞은 청크를 스니펫으로
                cur.execute("""SELECT dbms_lob.substr(text, 200, 1) FROM corpus_chunks
                               WHERE source_name = :1 AND src_id = :2 AND chunk_no = :3""",
                            [src, sid, best_chunk[pid]])
                r = cur.fetchone()
                if r and r[0]:
                    snip = r[0]
            out.append(f"[{pid}] {t} (유형: {kind or src}, 매칭: {tag})\n{snip}"
                       + (f"\n링크: {url}" if url else ""))
    return "\n\n".join(out)


def source_search_tools() -> list:
    """등록 소스마다 소스 한정 검색 함수 생성 — 에이전트 도구로 자동 등록.
    소스 등록(관리 페이지) = 검색 도구 등록. 이름: search_{소스명}."""
    from ingestion import source_registry
    with _pool.acquire() as con:
        cur = con.cursor()
        rows = source_registry.list_sources(cur)
        con.commit()

    def make(src: str, kind: str):
        def f(query: str, limit: int = 5) -> str:
            return _search(query, limit, source=src)
        f.__name__ = f"search_{src}"
        f.__doc__ = (f"'{src}' 소스({kind or '문서'})만 하이브리드 검색한다. "
                     f"전체 통합 검색은 search_docs.\n\n"
                     "    Args:\n"
                     "        query: 검색어 (한국어/영어, 자연어 가능)\n"
                     "        limit: 최대 결과 수 (기본 5)\n")
        return f
    return [make(r["source_name"], r["content_kind"])
            for r in rows if r["enabled"]]


def read_doc(post_id: str) -> str:
    """검색 결과의 문서 id(post_id)로 전문을 읽는다. '소스명:id' 형식."""
    src, sep, sid = post_id.partition(":")
    if not sep:
        return f"글을 찾을 수 없습니다: {post_id} (문서 id 형식: 소스명:원천id)"
    with _pool.acquire() as con:
        cur = con.cursor()
        cur.execute("""SELECT d.title, d.body, d.kind,
                              CASE WHEN NVL(r.url_enabled, 'Y') = 'Y' THEN d.url END
                       FROM corpus_docs d
                       JOIN source_registry r ON r.source_name = d.source_name
                       WHERE d.source_name = :1 AND d.src_id = :2""", [src, sid])
        row = cur.fetchone()
        if not row:
            return f"글을 찾을 수 없습니다: {post_id}"
        body = row[1].read() if hasattr(row[1], "read") else (row[1] or "")
        return (f"# {row[0]}\n(유형: {row[2] or src}"
                f"{' · ' + row[3] if row[3] else ''})\n\n{body}")


if __name__ == "__main__":
    reload_index()
    print(search_docs("파이썬 패키지 설치가 사내망에서 안 됨", 5))
