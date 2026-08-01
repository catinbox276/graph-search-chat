"""blog_posts 임베딩 백필 — Oracle에 원본 저장 (하이브리드 검색용).

- 대상 텍스트: 제목 + 본문 앞 300자
- 점수(코퍼스 JSONL) 높은 순으로 처리 -> 인기 문서부터 하이브리드 가능
- 이어하기: embedding IS NULL 인 것만 처리
usage: .venv/bin/python scripts/embed_corpus.py
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
from tools.blog_search import DSN, PASSWORD, USER  # noqa: E402

CORPUS = ROOT / "data" / "corpus" / "blog_corpus.jsonl"
EMB_MODEL = "text-embedding-qwen3-embedding-0.6b"
BATCH = 64
CONCURRENCY = 4  # LM Studio 동시 요청 수

llm = AsyncOpenAI(base_url="http://127.0.0.1:1234/v1", api_key="lm-studio")


async def main():
    con = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = con.cursor()
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name='BLOG_POSTS' AND column_name='EMBEDDING'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE blog_posts ADD (embedding BLOB)")

    # 점수 내림차순 우선순위 (JSONL에서), 이미 임베딩된 id는 스킵
    cur.execute("SELECT id FROM blog_posts WHERE embedding IS NOT NULL")
    done = {r[0] for r in cur.fetchall()}
    order = sorted(
        (json.loads(l) for l in open(CORPUS, encoding="utf-8")),
        key=lambda d: d["score"], reverse=True)
    todo = [(d["id"], (d["title"] + " " + d["body"][:300])) for d in order
            if d["id"] not in done]
    print(f"임베딩 대상 {len(todo)}건 (완료 {len(done)}건 스킵)", flush=True)

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
            rows += [(np.asarray(v.embedding, dtype=np.float32).tobytes(), b[k][0])
                     for k, v in enumerate(vecs.data)]
        cur.executemany("UPDATE blog_posts SET embedding = :1 WHERE id = :2", rows)
        con.commit()
        n += len(chunk)
        if n % (step * 10) < step:
            rate = n / (time.time() - t0)
            print(f"{n}/{len(todo)} ({rate:.0f}건/s, 남은 {((len(todo)-n)/rate)/60:.0f}분)",
                  flush=True)
    print(f"완료: {n}건")


if __name__ == "__main__":
    asyncio.run(main())
