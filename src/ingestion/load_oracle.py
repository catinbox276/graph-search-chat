"""blog_corpus.jsonl -> Oracle blog_posts 테이블 + Oracle Text 인덱스.

usage: python3 ingestion/load_oracle.py
전제: Oracle 기동 + .env의 ORACLE_DSN/USER/PASSWORD (core/config.py)
"""
import json
import sys
from pathlib import Path

import oracledb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from core import config  # noqa: E402

CORPUS = ROOT.parent / "data" / "corpus" / "blog_corpus.jsonl"
DSN = config.ORACLE_DSN


def main():
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=DSN)
    cur = con.cursor()

    cur.execute("""
        SELECT COUNT(*) FROM user_tables WHERE table_name = 'BLOG_POSTS'
    """)
    if cur.fetchone()[0]:
        cur.execute("DROP TABLE blog_posts PURGE")
    cur.execute("""
        CREATE TABLE blog_posts (
          id     VARCHAR2(64) PRIMARY KEY,
          title  VARCHAR2(1000),
          body   CLOB,
          tags   VARCHAR2(1000),
          source VARCHAR2(50),
          url    VARCHAR2(500)
        )
    """)

    total, batch = 0, []
    for line in open(CORPUS, encoding="utf-8"):
        d = json.loads(line)
        batch.append((d["id"], d["title"][:1000], d["body"],
                      " ".join(d["tags"])[:1000], d["source"], d["url"][:500]))
        if len(batch) >= 5000:
            cur.executemany("INSERT INTO blog_posts VALUES (:1,:2,:3,:4,:5,:6)", batch)
            total += len(batch); batch = []
    if batch:
        cur.executemany("INSERT INTO blog_posts VALUES (:1,:2,:3,:4,:5,:6)", batch)
        total += len(batch)
    con.commit()
    print(f"적재: {total}건")

    con.close()


if __name__ == "__main__":
    main()
