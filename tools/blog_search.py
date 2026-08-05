"""코퍼스 검색 함수 툴 — Oracle 19c 하이브리드 (Oracle Text + 인메모리 임베딩 행렬).

- 검색 대상은 통합 코퍼스 corpus_docs (등록 소스들을 scripts/ingest_sources.py가 조립).
  corpus_docs가 아직 없거나 비어 있으면 구 blog_posts로 폴백 — 전환기에도 무중단.
- 원본 임베딩은 Oracle(embedding BLOB)에 저장 (scripts/embed_corpus.py가 백필)
- 서버/프로세스 기동 시 임베딩을 numpy 행렬로 메모리 로드 (정규화 -> dot = cosine)
- 검색: Oracle Text(lexical) top-30 + 행렬 코사인(semantic) top-30 -> RRF 융합
- 임베딩이 아직 없으면 lexical 단독으로 동작 (백필 진행 중에도 사용 가능)
- 문서 id: "소스명:원천id" (구형 blog id도 read_blog_post가 받아준다)

에이전트에 함수 툴로 직접 등록해서 쓴다 (MCP 아님 — MCP는 DataHub 공식만 사용).
"""
import re
import threading

import numpy as np
import oracledb
from openai import OpenAI

from tools import config

DSN = config.ORACLE_DSN            # 접속 상수는 관례상 이 모듈에서 import
USER = config.ORACLE_USER
PASSWORD = config.ORACLE_PASSWORD
EMB_MODEL = config.EMBED_MODEL     # .env로 제어 (임베딩 호스트가 단일 모델 서빙)
RRF_K = config.RRF_K

_pool = oracledb.create_pool(user=USER, password=PASSWORD, dsn=DSN,
                             min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                             increment=config.ORACLE_POOL_INCREMENT)
_llm = OpenAI(base_url=config.EMBED_URL, api_key=config.MODEL_API_KEY)
_matrix, _ids, _lock = None, None, threading.Lock()
_use_corpus = None  # corpus_docs 사용 여부 — load_matrix가 재판정


def _corpus_ready(cur) -> bool:
    """corpus_docs가 있고 비어 있지 않은가 (전환기 폴백 판단)."""
    global _use_corpus
    if _use_corpus is None:
        cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'CORPUS_DOCS'")
        if cur.fetchone()[0]:
            cur.execute("SELECT COUNT(*) FROM corpus_docs WHERE ROWNUM = 1")
            _use_corpus = bool(cur.fetchone()[0])
        else:
            _use_corpus = False
    return _use_corpus


def load_matrix():
    """Oracle의 임베딩을 메모리 행렬로 로드. 서버 기동 시 1회 호출 권장."""
    global _matrix, _ids, _use_corpus
    with _lock:
        _use_corpus = None  # 적재 배치 후 /reload로 코퍼스 전환을 재감지
        with _pool.acquire() as con:
            cur = con.cursor()
            if _corpus_ready(cur):
                cur.execute("""SELECT source_name || ':' || src_id, embedding
                               FROM corpus_docs WHERE embedding IS NOT NULL""")
            else:
                cur.execute("SELECT id, embedding FROM blog_posts "
                            "WHERE embedding IS NOT NULL")
            ids, vecs = [], []
            for pid, blob in cur:
                ids.append(pid)
                vecs.append(np.frombuffer(blob.read(), dtype=np.float32))
        if vecs:
            m = np.stack(vecs)
            m /= np.linalg.norm(m, axis=1, keepdims=True)
            _matrix, _ids = m, ids
        print(f"[blog_search] 임베딩 행렬 로드: {len(ids)}건"
              f" (대상: {'corpus_docs' if _use_corpus else 'blog_posts'})")
    return len(ids)


def _lexical(cur, query: str, n: int):
    terms = [t for t in re.findall(r"[\w가-힣]+", query) if len(t) >= 2]
    if not terms:
        return []
    if _corpus_ready(cur):
        cur.execute(
            """SELECT source_name || ':' || src_id FROM corpus_docs
               WHERE CONTAINS(body, :q, 1) > 0
               ORDER BY SCORE(1) DESC FETCH FIRST :n ROWS ONLY""",
            q=" ACCUM ".join(terms), n=n)
    else:
        cur.execute(
            """SELECT id FROM blog_posts WHERE CONTAINS(body, :q, 1) > 0
               ORDER BY SCORE(1) DESC FETCH FIRST :n ROWS ONLY""",
            q=" ACCUM ".join(terms), n=n)
    return [r[0] for r in cur.fetchall()]


def _semantic(query: str, n: int):
    if _matrix is None:
        return []
    q = np.asarray(
        _llm.embeddings.create(model=EMB_MODEL, input=query).data[0].embedding,
        dtype=np.float32)
    q /= np.linalg.norm(q)
    top = np.argsort(_matrix @ q)[::-1][:n]
    return [_ids[i] for i in top]


def search_blog(query: str, limit: int = 5) -> str:
    """사내 지식 코퍼스(블로그·QA·가이드 등 등록된 원천)를 키워드+시맨틱 하이브리드로 검색한다.

    Args:
        query: 검색어 (한국어/영어, 자연어 가능)
        limit: 최대 결과 수 (기본 5)
    """
    with _pool.acquire() as con:
        cur = con.cursor()
        lex = _lexical(cur, query, config.SEARCH_TOP_LEXICAL)
        sem = _semantic(query, config.SEARCH_TOP_SEMANTIC)
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
            if _corpus_ready(cur):
                src, _, sid = pid.partition(":")
                cur.execute(
                    """SELECT title, kind, url, dbms_lob.substr(body, 200, 1)
                       FROM corpus_docs WHERE source_name = :1 AND src_id = :2""",
                    [src, sid])
                t, kind, url, snip = cur.fetchone()
                out.append(f"[{pid}] {t} (유형: {kind or src}, 매칭: {tag})\n{snip}\n{url or ''}")
            else:
                cur.execute(
                    """SELECT title, source, url, dbms_lob.substr(body, 200, 1)
                       FROM blog_posts WHERE id = :1""", [pid])
                t, src, url, snip = cur.fetchone()
                out.append(f"[{pid}] {t} (출처: {src}, 매칭: {tag})\n{snip}\n{url}")
    return "\n\n".join(out)


def read_blog_post(post_id: str) -> str:
    """검색 결과의 문서 id(post_id)로 전문을 읽는다. '소스명:id' 또는 구형 blog id."""
    with _pool.acquire() as con:
        cur = con.cursor()
        if _corpus_ready(cur):
            src, sep, sid = post_id.partition(":")
            if not sep:  # 구형 blog id — 소스 1호로 조회
                src, sid = "blog_posts", post_id
            cur.execute("""SELECT title, body, kind, url FROM corpus_docs
                           WHERE source_name = :1 AND src_id = :2""", [src, sid])
            row = cur.fetchone()
            if row:
                body = row[1].read() if hasattr(row[1], "read") else (row[1] or "")
                return (f"# {row[0]}\n(유형: {row[2] or src}"
                        f"{' · ' + row[3] if row[3] else ''})\n\n{body}")
        cur.execute(
            "SELECT title, body, source, url FROM blog_posts WHERE id = :1",
            [post_id])
        row = cur.fetchone()
        if not row:
            return f"글을 찾을 수 없습니다: {post_id}"
        return f"# {row[0]}\n(출처: {row[2]} {row[3]})\n\n{row[1].read()}"


if __name__ == "__main__":
    load_matrix()
    print(search_blog("파이썬 패키지 설치가 사내망에서 안 됨", 5))
