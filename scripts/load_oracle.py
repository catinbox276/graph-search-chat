"""blog_corpus.jsonl -> Oracle blog_posts 테이블 + Oracle Text 인덱스.

usage: python3 scripts/load_oracle.py
전제: docker run gvenzl/oracle-xe (localhost:1521/FREEPDB1, system/poc1234)
"""
import json
from pathlib import Path

import oracledb

CORPUS = Path(__file__).parent.parent / "data" / "corpus" / "blog_corpus.jsonl"
DSN = "localhost:1521/FREEPDB1"


def main():
    con = oracledb.connect(user="system", password="poc1234", dsn=DSN)
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

    # Oracle Text: WORLD_LEXER = 한국어/영어 혼합 자동 처리
    # (사내 19c에서 한국어 정밀도가 필요하면 KOREAN_MORPH_LEXER로 교체)
    cur.execute("""
        BEGIN
          BEGIN ctx_ddl.drop_preference('blog_lexer'); EXCEPTION WHEN OTHERS THEN NULL; END;
          ctx_ddl.create_preference('blog_lexer', 'WORLD_LEXER');
        END;
    """)
    cur.execute("""
        CREATE INDEX blog_posts_body_idx ON blog_posts(body)
        INDEXTYPE IS CTXSYS.CONTEXT
        PARAMETERS ('LEXER blog_lexer SYNC (ON COMMIT)')
    """)
    print("Oracle Text 인덱스 생성 완료")

    cur.execute("""
        SELECT title FROM blog_posts
        WHERE CONTAINS(body, 'proxy AND pip', 1) > 0
        AND ROWNUM <= 3 ORDER BY SCORE(1) DESC
    """)
    print("검색 스모크:", [r[0][:60] for r in cur.fetchall()])
    con.close()


if __name__ == "__main__":
    main()
