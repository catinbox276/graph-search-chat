"""블로그 검색 함수 툴 — Oracle 19c 하이브리드 (Oracle Text + 인메모리 임베딩 행렬).

- 원본 임베딩은 Oracle(blog_posts.embedding BLOB)에 저장 (scripts/embed_corpus.py)
- 서버/프로세스 기동 시 임베딩을 numpy 행렬로 메모리 로드 (정규화 -> dot = cosine)
- 검색: Oracle Text(lexical) top-30 + 행렬 코사인(semantic) top-30 -> RRF 융합
- 임베딩이 아직 없으면 lexical 단독으로 동작 (백필 진행 중에도 사용 가능)

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


def load_matrix():
    """Oracle의 임베딩을 메모리 행렬로 로드. 서버 기동 시 1회 호출 권장."""
    global _matrix, _ids
    with _lock:
        with _pool.acquire() as con:
            cur = con.cursor()
            cur.execute("SELECT id, embedding FROM blog_posts WHERE embedding IS NOT NULL")
            ids, vecs = [], []
            for pid, blob in cur:
                ids.append(pid)
                vecs.append(np.frombuffer(blob.read(), dtype=np.float32))
        if vecs:
            m = np.stack(vecs)
            m /= np.linalg.norm(m, axis=1, keepdims=True)
            _matrix, _ids = m, ids
        print(f"[blog_search] 임베딩 행렬 로드: {len(ids)}건")
    return len(ids)


def _lexical(cur, query: str, n: int):
    terms = [t for t in re.findall(r"[\w가-힣]+", query) if len(t) >= 2]
    if not terms:
        return []
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
    """사내 블로그(문제해결 노하우 글)를 키워드+시맨틱 하이브리드로 검색한다.

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
            cur.execute(
                """SELECT title, source, url, dbms_lob.substr(body, 200, 1)
                   FROM blog_posts WHERE id = :1""", [pid])
            t, src, url, snip = cur.fetchone()
            tag = ("L+S" if pid in lex and pid in sem
                   else "L" if pid in lex else "S")
            out.append(f"[{pid}] {t} (출처: {src}, 매칭: {tag})\n{snip}\n{url}")
    return "\n\n".join(out)


def read_blog_post(post_id: str) -> str:
    """검색 결과의 post_id로 글 전문(질문 + 해결 답변)을 읽는다."""
    with _pool.acquire() as con:
        cur = con.cursor()
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
