"""청크 임베딩 백필 — Oracle에 원본 저장 (하이브리드 검색용, docs/schema.md §5).

- 대상: corpus_chunks에서 embedding IS NULL 이거나 embed_model이 현재 모델과 다른 행.
  → 임베딩 모델 교체 시 이 배치가 자동으로 점진 재백필한다 (검색은 현재 모델
  벡터만 로드 — 재백필 중 커버리지가 점증하고 lexical이 나머지를 받침).
- 대상 텍스트: 청크 text 전체 (title 접두 포함 — 청킹 때 이미 조립됨)
usage: .venv/bin/python scripts/embed_corpus.py   (야간 CronJob 03:30과 동일)
"""
import asyncio
import sys
import time
from pathlib import Path

import numpy as np
import oracledb
from openai import AsyncOpenAI

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from tools import config  # noqa: E402
from tools import model_registry  # noqa: E402

EMBED_URL_RESOLVED, EMB_MODEL = model_registry.embedding_endpoint()  # 레지스트리 우선

BATCH = config.EMBED_BATCH
CONCURRENCY = config.EMBED_CONCURRENCY  # 임베딩 서빙 동시 요청 수

llm = AsyncOpenAI(base_url=EMBED_URL_RESOLVED, api_key=config.MODEL_API_KEY)


async def main():
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'CORPUS_CHUNKS'")
    if not cur.fetchone()[0]:
        print("corpus_chunks 없음 — 먼저 scripts/chunk_corpus.py로 청킹하세요", flush=True)
        return
    cur.execute("""SELECT source_name, src_id, chunk_no, text FROM corpus_chunks
                   WHERE embedding IS NULL OR embed_model IS NULL
                      OR embed_model != :m""", m=EMB_MODEL)
    todo = [((r[0], r[1], r[2]), r[3].read() if hasattr(r[3], "read") else r[3])
            for r in cur.fetchall()]
    update_sql = ("UPDATE corpus_chunks SET embedding = :1, embed_model = :2 "
                  "WHERE source_name = :3 AND src_id = :4 AND chunk_no = :5")
    print(f"임베딩 대상 청크 {len(todo)}건 (모델 {EMB_MODEL})", flush=True)

    t0, n = time.time(), 0
    step = BATCH * CONCURRENCY
    for i in range(0, len(todo), step):
        chunk = todo[i:i + step]
        batches = [chunk[j:j + BATCH] for j in range(0, len(chunk), BATCH)]
        results = await asyncio.gather(*[
            llm.embeddings.create(model=EMB_MODEL, input=[t for _, t in b])
            for b in batches])
        rows = []
        for b, vecs in zip(batches, results):
            rows += [(np.asarray(v.embedding, dtype=np.float32).tobytes(),
                      EMB_MODEL, *b[k][0])
                     for k, v in enumerate(vecs.data)]
        cur.executemany(update_sql, rows)
        con.commit()
        n += len(chunk)
        if n % (step * 10) < step:
            rate = n / (time.time() - t0)
            print(f"{n}/{len(todo)} ({rate:.0f}건/s, 남은 {((len(todo)-n)/rate)/60:.0f}분)",
                  flush=True)
    print(f"완료: {n}건")


if __name__ == "__main__":
    import time as _t
    from tools import events as _ev
    _t0 = _t.time()
    try:
        asyncio.run(main())
        _ev.log("batch", source="embed-backfill", level="info", status="ok",
                duration_ms=int((_t.time() - _t0) * 1000), summary="embed-backfill 완료")
    except Exception as _e:
        import traceback as _tb
        _ev.log("batch", source="embed-backfill", level="error", status="fail",
                duration_ms=int((_t.time() - _t0) * 1000),
                summary=f"{type(_e).__name__}: {str(_e)[:200]}",
                detail=_tb.format_exc())
        raise
