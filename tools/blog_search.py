"""블로그 검색 함수 툴 — Oracle 19c(로컬 PoC: 23ai Free(ARM))의 Oracle Text 조회.

에이전트에 함수 툴로 직접 등록해서 쓴다 (MCP 아님 — MCP는 DataHub 공식만 사용).
"""
import os
import re

import oracledb

DSN = os.environ.get("ORACLE_DSN", "localhost:1521/FREEPDB1")
USER = os.environ.get("ORACLE_USER", "system")
PASSWORD = os.environ.get("ORACLE_PASSWORD", "poc1234")

_pool = oracledb.create_pool(user=USER, password=PASSWORD, dsn=DSN, min=1, max=4)


def search_blog(query: str, limit: int = 5) -> str:
    """사내 블로그(문제해결 노하우 글)를 키워드로 검색한다.

    Args:
        query: 검색어 (한국어/영어, 자연어 가능)
        limit: 최대 결과 수 (기본 5)
    """
    terms = re.findall(r"[\w가-힣]+", query)
    if not terms:
        return "검색어가 비어 있습니다."
    match = " ACCUM ".join(terms)  # ACCUM: OR보다 다중어 매칭에 높은 점수
    with _pool.acquire() as con:
        cur = con.cursor()
        cur.execute(
            """SELECT id, title, source, url, SCORE(1),
                      dbms_lob.substr(body, 200, 1)
               FROM blog_posts
               WHERE CONTAINS(body, :q, 1) > 0
               ORDER BY SCORE(1) DESC
               FETCH FIRST :n ROWS ONLY""",
            q=match, n=limit,
        )
        rows = cur.fetchall()
    if not rows:
        return "검색 결과가 없습니다."
    return "\n\n".join(
        f"[{r[0]}] {r[1]} (출처: {r[2]}, 점수: {r[4]})\n{r[5]}\n{r[3]}" for r in rows
    )


def read_blog_post(post_id: str) -> str:
    """검색 결과의 post_id로 글 전문(질문 + 해결 답변)을 읽는다."""
    with _pool.acquire() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT title, body, source, url FROM blog_posts WHERE id = :1",
            [post_id],
        )
        row = cur.fetchone()
        if not row:
            return f"글을 찾을 수 없습니다: {post_id}"
        return f"# {row[0]}\n(출처: {row[2]} {row[3]})\n\n{row[1].read()}"


if __name__ == "__main__":
    print(search_blog("pip install proxy", 3))
