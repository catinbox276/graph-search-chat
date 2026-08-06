"""코퍼스 검색 함수 툴 — Oracle 19c 하이브리드 (Oracle Text + 인메모리 임베딩 행렬).

- 검색 대상은 통합 코퍼스 corpus_docs 단일 (등록 소스들을 scripts/ingest_sources.py가
  조립). 특정 원천 테이블 직조회 없음 — 소스 추가는 코드가 아니라 source_registry 등록.
- 원본 임베딩은 Oracle(embedding BLOB)에 저장 (scripts/embed_corpus.py가 백필)
- 서버/프로세스 기동 시 임베딩을 numpy 행렬로 메모리 로드 (정규화 -> dot = cosine)
- 검색: Oracle Text(lexical) top-30 + 행렬 코사인(semantic) top-30 -> RRF 융합
- 임베딩이 아직 없으면 lexical 단독으로 동작 (백필 진행 중에도 사용 가능)
- 문서 id: "소스명:원천id" 단일 형식

에이전트에 함수 툴로 직접 등록해서 쓴다 (MCP 아님 — MCP는 DataHub 공식만 사용).
"""
import re
import threading

import numpy as np
import oracledb
from tools import config, model_registry

DSN = config.ORACLE_DSN            # 접속 상수는 관례상 이 모듈에서 import
USER = config.ORACLE_USER
PASSWORD = config.ORACLE_PASSWORD
RRF_K = config.RRF_K

_pool = oracledb.create_pool(user=USER, password=PASSWORD, dsn=DSN,
                             min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                             increment=config.ORACLE_POOL_INCREMENT)
_matrix, _ids, _chunk_nos, _lock = None, None, None, threading.Lock()


def _corpus_ready(cur) -> bool:
    """corpus_docs가 존재하는가 (최초 적재 전 빈 환경 가드)."""
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'CORPUS_DOCS'")
    return bool(cur.fetchone()[0])


def load_matrix():
    """청크 임베딩을 메모리 행렬로 로드 (현재 모델 벡터만 — 모델 교체 중엔
    백필된 만큼 커버리지 점증, lexical이 나머지를 받침). 서버 기동 시 1회."""
    global _matrix, _ids, _chunk_nos
    with _lock:
        with _pool.acquire() as con:
            cur = con.cursor()
            ids, nos, vecs = [], [], []
            _, emb_name = model_registry.embedding_endpoint()
            cur.execute("""SELECT COUNT(*) FROM user_tables
                           WHERE table_name = 'CORPUS_CHUNKS'""")
            if cur.fetchone()[0]:
                cur.execute("""SELECT source_name || ':' || src_id, chunk_no, embedding
                               FROM corpus_chunks
                               WHERE embedding IS NOT NULL AND embed_model = :m""",
                            m=emb_name)
                for pid, no, blob in cur:
                    ids.append(pid)
                    nos.append(no)
                    vecs.append(np.frombuffer(blob.read(), dtype=np.float32))
        if vecs:
            m = np.stack(vecs)
            m /= np.linalg.norm(m, axis=1, keepdims=True)
            _matrix, _ids, _chunk_nos = m, ids, nos
        print(f"[blog_search] 청크 임베딩 행렬 로드: {len(ids)}건 (모델 {emb_name})")
    return len(ids)


def _lexical(cur, query: str, n: int, source: str = ""):
    terms = [t for t in re.findall(r"[\w가-힣]+", query) if len(t) >= 2]
    if not terms:
        return []
    if not _corpus_ready(cur):
        return []
    cur.execute(
        f"""SELECT source_name || ':' || src_id FROM corpus_docs
           WHERE CONTAINS(body, :q, 1) > 0
           {"AND source_name = :src" if source else ""}
           ORDER BY SCORE(1) DESC FETCH FIRST :n ROWS ONLY""",
        # {{}} 이스케이프 — 예약어(AND/OR 등)가 검색어에 섞여도 구문 오류 없게
        **({"src": source} if source else {}),
        q=" ACCUM ".join("{" + t + "}" for t in terms), n=n)
    return [r[0] for r in cur.fetchall()]


def _semantic(query: str, n: int, source: str = ""):
    """청크 코사인 → 문서 단위 집계(best-chunk). 반환: (문서 pid 목록, {pid: 최고 청크 no})."""
    if _matrix is None:
        return [], {}
    cli, emb_name = model_registry.embedding_client()
    q = np.asarray(
        cli.embeddings.create(model=emb_name, input=query).data[0].embedding,
        dtype=np.float32)
    q /= np.linalg.norm(q)
    scores = _matrix @ q
    prefix = f"{source}:" if source else ""
    best = {}  # pid -> (score, chunk_no)
    for i in np.argsort(scores)[::-1]:
        pid = _ids[i]
        if prefix and not pid.startswith(prefix):
            continue
        if pid not in best:
            best[pid] = (float(scores[i]), _chunk_nos[i])
            if len(best) >= n:
                break
    ordered = sorted(best, key=lambda p: best[p][0], reverse=True)
    return ordered, {p: best[p][1] for p in ordered}


def search_blog(query: str, limit: int = 5) -> str:
    """사내 지식 코퍼스(등록된 모든 원천 통합)를 키워드+시맨틱 하이브리드로 검색한다.

    Args:
        query: 검색어 (한국어/영어, 자연어 가능)
        limit: 최대 결과 수 (기본 5)
    """
    return _search(query, limit)


def _search(query: str, limit: int = 5, source: str = "") -> str:
    with _pool.acquire() as con:
        cur = con.cursor()
        lex = _lexical(cur, query, config.SEARCH_TOP_LEXICAL, source)
        sem, best_chunk = _semantic(query, config.SEARCH_TOP_SEMANTIC, source)
        scores = {}
        for rank_list in (lex, sem):
            for r, pid in enumerate(rank_list):
                scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + r + 1)
        top = sorted(scores, key=scores.get, reverse=True)[:limit]
        if not top:
            return "검색 결과가 없습니다."
        out = []
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
    from tools import source_registry
    with _pool.acquire() as con:
        cur = con.cursor()
        rows = source_registry.list_sources(cur)
        con.commit()

    def make(src: str, kind: str):
        def f(query: str, limit: int = 5) -> str:
            return _search(query, limit, source=src)
        f.__name__ = f"search_{src}"
        f.__doc__ = (f"'{src}' 소스({kind or '문서'})만 하이브리드 검색한다. "
                     f"전체 통합 검색은 search_blog.\n\n"
                     "    Args:\n"
                     "        query: 검색어 (한국어/영어, 자연어 가능)\n"
                     "        limit: 최대 결과 수 (기본 5)\n")
        return f
    return [make(r["source_name"], r["content_kind"])
            for r in rows if r["enabled"]]


def read_blog_post(post_id: str) -> str:
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
    load_matrix()
    print(search_blog("파이썬 패키지 설치가 사내망에서 안 됨", 5))
