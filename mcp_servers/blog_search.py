"""블로그 검색 MCP 서버 — blog_corpus.jsonl 위의 SQLite FTS5(BM25) 검색.

첫 실행 시 인덱스(blog_index.db)를 자동 생성한다 (약 10초).
Claude Code 등록: 프로젝트 루트의 .mcp.json 참조.
"""
import json
import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "data" / "corpus" / "blog_corpus.jsonl"
INDEX = ROOT / "data" / "corpus" / "blog_index.db"

mcp = FastMCP("blog-search")


def build_index():
    con = sqlite3.connect(INDEX)
    con.execute(
        "CREATE VIRTUAL TABLE posts USING fts5"
        "(id UNINDEXED, title, body, tags, source UNINDEXED, url UNINDEXED)"
    )
    with open(CORPUS, encoding="utf-8") as f:
        rows = (
            (d["id"], d["title"], d["body"], " ".join(d["tags"]), d["source"], d["url"])
            for d in map(json.loads, f)
        )
        con.executemany("INSERT INTO posts VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


if not INDEX.exists():
    build_index()
_con = sqlite3.connect(INDEX, check_same_thread=False)


@mcp.tool()
def search_blog(query: str, limit: int = 5) -> str:
    """사내 블로그(문제해결 노하우 글)를 키워드로 검색한다.

    Args:
        query: 검색어 (한국어/영어, 자연어 가능)
        limit: 최대 결과 수 (기본 5)
    """
    terms = re.findall(r"\w+", query)
    if not terms:
        return "검색어가 비어 있습니다."
    match = " OR ".join(f'"{t}"' for t in terms)
    rows = _con.execute(
        "SELECT id, title, source, url, snippet(posts, 2, '[', ']', '…', 25),"
        " bm25(posts) FROM posts WHERE posts MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    ).fetchall()
    if not rows:
        return "검색 결과가 없습니다."
    return "\n\n".join(
        f"[{r[0]}] {r[1]} (출처: {r[2]}, 관련도: {-r[5]:.1f})\n{r[4]}\n{r[3]}"
        for r in rows
    )


@mcp.tool()
def read_blog_post(post_id: str) -> str:
    """검색 결과의 post_id로 글 전문(질문 + 해결 답변)을 읽는다."""
    row = _con.execute(
        "SELECT title, body, source, url FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    if not row:
        return f"글을 찾을 수 없습니다: {post_id}"
    return f"# {row[0]}\n(출처: {row[2]} {row[3]})\n\n{row[1]}"


if __name__ == "__main__":
    mcp.run()
