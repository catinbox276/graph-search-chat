"""blog_corpus.jsonl -> Oracle blog_posts 테이블 + Oracle Text 인덱스.

usage: python3 scripts/load_oracle.py
전제: Oracle 기동 + .env의 ORACLE_DSN/USER/PASSWORD (tools/config.py)
"""
import json
import re
import sys
from pathlib import Path

import oracledb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from tools import config  # noqa: E402

CORPUS = ROOT / "data" / "corpus" / "blog_corpus.jsonl"
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

    # Oracle Text 렉서는 .env의 ORACLE_TEXT_LEXER로 제어 (기본 WORLD_LEXER = 한/영 혼합 자동).
    # 사내 19c 한국어 정밀도가 필요하면 KOREAN_MORPH_LEXER로. 렉서명은 DDL이라 바인드 불가 → 검증 후 삽입.
    lexer = config.ORACLE_TEXT_LEXER
    if not re.fullmatch(r"[A-Za-z0-9_]+", lexer):
        raise ValueError(f"잘못된 ORACLE_TEXT_LEXER: {lexer!r} (영숫자·밑줄만 허용)")
    cur.execute(f"""
        BEGIN
          BEGIN ctx_ddl.drop_preference('blog_lexer'); EXCEPTION WHEN OTHERS THEN NULL; END;
          ctx_ddl.create_preference('blog_lexer', '{lexer}');
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
