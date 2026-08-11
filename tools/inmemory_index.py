"""인메모리 하이브리드 검색 인덱스 (SQLite :memory: — FTS5 + sqlite-vec).

Oracle(SoT)에서 corpus_chunks를 전량 로드해 프로세스 내 SQLite로 인덱싱한다.
- 렉시컬: FTS5(BM25) — text_tokenized(Kiwi 원형) 대상, tokenize='unicode61'
- 벡터  : sqlite-vec vec0 — embedding(float32[]) 코사인 KNN
- 파일 없음(:memory:), Oracle Text/CTXAPP 권한 불필요.

corpus_search 와 인터페이스를 맞춘다: lexical()/semantic() 반환형이 기존
_lexical()/_semantic() 과 동일(pid 목록 / (pid목록, {pid:chunk_no})).

동기화(설계문서 §5 조건): 버전 변경 시 통째 리로드. 증분은 PoC 범위 밖.
의존: sqlite-vec (pip install sqlite-vec).

TODO(구현 확정 시): embedding dim은 첫 행에서 추론. 대량 시 로드시간·RAM 확인.
"""
import sqlite3
import threading

import numpy as np
import oracledb

from tools import config, model_registry
from tools import ko_tokenize

_pool = oracledb.create_pool(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                             dsn=config.ORACLE_DSN, min=1, max=2, increment=1)
_con = None            # sqlite :memory: 연결
_meta = {}             # chunk_id(int) -> (pid, chunk_no)
_version = None        # 현재 인덱스가 반영한 Oracle 버전
_has_vec = False       # 벡터 테이블 존재 여부(임베딩 미서빙 시 False → 렉시컬 단독)
_lock = threading.Lock()


def _current_version(cur) -> tuple:
    """Oracle 데이터 버전 스냅샷. 바뀌면 리로드 트리거.
    렉시컬 색인 기준(text_tokenized)으로 잡음 — 임베딩 유무와 무관하게 반영.
    TODO: 정확도 필요 시 전용 카운터(app_settings) 사용. PoC는 (건수, 최신시각)."""
    cur.execute("SELECT COUNT(*), MAX(created_at) FROM corpus_chunks "
                "WHERE text_tokenized IS NOT NULL")
    return tuple(cur.fetchone())


def _load_rows(cur):
    """(pid, chunk_no, tokenized, vec) 로드. FTS는 text_tokenized 있는 전 청크,
    벡터는 현재 모델 임베딩이 있는 청크만(없으면 vec=None → 렉시컬 단독 동작)."""
    _, emb_name = model_registry.embedding_endpoint()
    cur.execute("""SELECT source_name || ':' || src_id, chunk_no, text_tokenized,
                          CASE WHEN embed_model = :m THEN embedding END
                   FROM corpus_chunks
                   WHERE text_tokenized IS NOT NULL""", m=emb_name)
    for pid, no, tok, blob in cur:
        tok = tok.read() if hasattr(tok, "read") else (tok or "")
        vec = np.frombuffer(blob.read(), dtype=np.float32) if blob is not None else None
        yield pid, no, tok, vec


def build_index():
    """Oracle → SQLite :memory: 전체 재빌드. 기동/버전변경 시 호출.
    임베딩 없으면 FTS(렉시컬)만 채워지고 벡터 경로는 비활성(폴백)."""
    global _con, _meta, _version, _has_vec
    import sqlite_vec  # 지연 임포트
    with _lock:
        with _pool.acquire() as ora:
            cur = ora.cursor()
            version = _current_version(cur)
            rows = list(_load_rows(cur))

        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        con.execute("CREATE VIRTUAL TABLE fts USING fts5(cid UNINDEXED, body, tokenize='unicode61')")
        meta, dim = {}, None
        for cid, (pid, no, tok, vec) in enumerate(rows):
            con.execute("INSERT INTO fts(cid, body) VALUES (?,?)", (cid, tok))   # 렉시컬은 항상
            if vec is not None:
                if dim is None:                   # 첫 임베딩 행에서 차원 확정 → vec 테이블 생성
                    dim = len(vec)
                    con.execute(f"CREATE VIRTUAL TABLE vec USING vec0(cid INTEGER PRIMARY KEY, embedding float[{dim}])")
                con.execute("INSERT INTO vec(cid, embedding) VALUES (?,?)",
                            (cid, sqlite_vec.serialize_float32(vec.tolist())))
            meta[cid] = (pid, no)
        con.commit()
        _con, _meta, _version, _has_vec = con, meta, version, dim is not None
        print(f"[inmemory_index] 빌드: {len(meta)}청크 (렉시컬 전량, 벡터 dim={dim}, ver={version})")
    return len(_meta)


def ensure_fresh():
    """버전 바뀌었으면 리로드. 검색 진입 전 호출(또는 타이머).
    TODO: 매 검색 확인이 부담이면 INMEM_RELOAD_SECS 간격 캐시."""
    with _pool.acquire() as ora:
        v = _current_version(ora.cursor())
    if _con is None or v != _version:
        build_index()


def _fts_query(query: str) -> str:
    """Kiwi 토큰 → FTS5 OR 질의(각 토큰 따옴표로 특수문자 무력화)."""
    toks = [t for t in ko_tokenize.tokenize_for_search(query).split() if t]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)


def lexical(query: str, n: int, source: str = "") -> list:
    """FTS5 BM25 → 문서(pid) 단위. 반환: [pid...] (기존 _lexical과 동형)."""
    if _con is None:
        return []
    q = _fts_query(query)
    if not q:
        return []
    prefix = f"{source}:" if source else ""
    rows = _con.execute(
        "SELECT cid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
        (q, n * 5)).fetchall()  # 청크→문서 집계 고려해 여유있게
    out = []
    for (cid,) in rows:
        pid = _meta[cid][0]
        if prefix and not pid.startswith(prefix):
            continue
        if pid not in out:
            out.append(pid)
            if len(out) >= n:
                break
    return out


def semantic(query: str, n: int, source: str = "") -> tuple:
    """vec0 코사인 KNN → 문서 best-chunk 집계. 반환: ([pid...], {pid:chunk_no}).
    임베딩 미서빙(벡터 테이블 없음)이면 빈 결과 → 렉시컬 단독으로 폴백."""
    if _con is None or not _has_vec:
        return [], {}
    import sqlite_vec
    try:
        cli, emb_name = model_registry.embedding_client()
        q = cli.embeddings.create(model=emb_name, input=query).data[0].embedding
    except Exception:   # 임베딩 엔드포인트 장애/미서빙 → 렉시컬 단독 폴백(§8 조건)
        return [], {}   # ponytail: 매 검색 1회 연결시도 비용 감수(미서빙 지속 시 캐시-차단은 추후)
    prefix = f"{source}:" if source else ""
    rows = _con.execute(   # sqlite-vec KNN은 LIMIT? 대신 k=? 제약 필요
        "SELECT cid, distance FROM vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(q), n * 5)).fetchall()
    best = {}
    for cid, dist in rows:
        pid, no = _meta[cid]
        if prefix and not pid.startswith(prefix):
            continue
        if pid not in best:                       # 이미 distance 오름차순 → 첫 등장이 best
            best[pid] = no
            if len(best) >= n:
                break
    return list(best), best


if __name__ == "__main__":
    build_index()
    print(lexical("사내망 패키지 설치", 5))
    print(semantic("사내망 패키지 설치", 5)[0])
