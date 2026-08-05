"""코퍼스 임베딩 백필 — Oracle에 원본 저장 (하이브리드 검색용).

- 대상: corpus_docs(통합 코퍼스 — ingest_sources.py가 적재)의 embedding IS NULL 행.
  corpus_docs가 아직 없으면 구 blog_posts 경로로 폴백 (전환기 호환).
- 대상 텍스트: 제목 + 본문 앞 300자
- 이어하기: embedding IS NULL 인 것만 처리
usage: .venv/bin/python scripts/embed_corpus.py   (야간 CronJob 03:30과 동일)
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
import oracledb
from openai import AsyncOpenAI

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from tools import config  # noqa: E402
from tools.blog_search import DSN, PASSWORD, USER  # noqa: E402

CORPUS = ROOT / "data" / "corpus" / "blog_corpus.jsonl"
EMB_MODEL = config.EMBED_MODEL  # .env로 제어
BATCH = config.EMBED_BATCH
CONCURRENCY = config.EMBED_CONCURRENCY  # 임베딩 서빙 동시 요청 수
TEXT_CHARS = config.EMBED_TEXT_CHARS    # 임베딩 대상: 제목 + 본문 앞 N자

llm = AsyncOpenAI(base_url=config.EMBED_URL, api_key=config.MODEL_API_KEY)


async def main():
    con = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'CORPUS_DOCS'")
    use_corpus = bool(cur.fetchone()[0])

    if use_corpus:
        # 통합 코퍼스 — 소스 무관하게 미임베딩분 백필
        cur.execute("""SELECT source_name, src_id,
                              NVL(title, ' ') || ' ' || dbms_lob.substr(body, :n, 1)
                       FROM corpus_docs WHERE embedding IS NULL""", n=TEXT_CHARS)
        todo = [((r[0], r[1]), r[2]) for r in cur.fetchall()]
        update_sql = ("UPDATE corpus_docs SET embedding = :1 "
                      "WHERE source_name = :2 AND src_id = :3")
        print(f"임베딩 대상 {len(todo)}건 (corpus_docs)", flush=True)
    else:
        # 전환기 폴백: 구 blog_posts 경로
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name='BLOG_POSTS' AND column_name='EMBEDDING'""")
        if not cur.fetchone()[0]:
            cur.execute("ALTER TABLE blog_posts ADD (embedding BLOB)")
        # 점수 내림차순 우선순위 (JSONL에서). 파일이 없으면(파드 등) DB에서 직접 조회
        cur.execute("SELECT id FROM blog_posts WHERE embedding IS NOT NULL")
        done = {r[0] for r in cur.fetchall()}
        try:
            order = sorted(
                (json.loads(l) for l in open(CORPUS, encoding="utf-8")),
                key=lambda d: d["score"], reverse=True)
            todo = [((d["id"],), (d["title"] + " " + d["body"][:TEXT_CHARS]))
                    for d in order if d["id"] not in done]
        except FileNotFoundError:
            cur.execute("""SELECT id, title || ' ' || dbms_lob.substr(body, :n, 1)
                           FROM blog_posts WHERE embedding IS NULL""", n=TEXT_CHARS)
            todo = [((r[0],), r[1]) for r in cur.fetchall()]
        update_sql = "UPDATE blog_posts SET embedding = :1 WHERE id = :2"
        print(f"임베딩 대상 {len(todo)}건 (blog_posts, 완료 {len(done)}건 스킵)", flush=True)

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
            rows += [(np.asarray(v.embedding, dtype=np.float32).tobytes(), *b[k][0])
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
    asyncio.run(main())
