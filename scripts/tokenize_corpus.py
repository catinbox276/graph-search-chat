"""토큰화 백필 — corpus_chunks.text_tokenized 를 Kiwi 원형으로 채운다.

embed_corpus.py 와 같은 멱등 백필 패턴:
- 대상: text_tokenized IS NULL 인 행 (재청킹으로 새로 들어온 청크 포함)
- 적재/쿼리 공용 tools.ko_tokenize.tokenize_for_search() 사용 (설계문서 §6 조건)
usage: .venv/bin/python scripts/tokenize_corpus.py  (야간 배치 03:15 청킹 직후 권장)

TODO: 원문 갱신 시 재토큰화가 필요하면 updated_at 비교 조건 추가.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from tools import config  # noqa: E402
from tools import ko_tokenize  # noqa: E402
import oracledb  # noqa: E402


def _ensure_column(cur):
    """구버전 DB 대비 컬럼 ensure (모듈 ensure 패턴). 이미 있으면 무시."""
    cur.execute("SELECT COUNT(*) FROM user_tab_columns "
                "WHERE table_name='CORPUS_CHUNKS' AND column_name='TEXT_TOKENIZED'")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE corpus_chunks ADD (text_tokenized CLOB)")


def main():
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                           dsn=config.ORACLE_DSN)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name='CORPUS_CHUNKS'")
    if not cur.fetchone()[0]:
        print("corpus_chunks 없음 — 먼저 scripts/chunk_corpus.py", flush=True)
        return
    _ensure_column(cur)
    con.commit()

    cur.execute("""SELECT source_name, src_id, chunk_no, text FROM corpus_chunks
                   WHERE text_tokenized IS NULL""")
    todo = [((r[0], r[1], r[2]), r[3].read() if hasattr(r[3], "read") else (r[3] or ""))
            for r in cur.fetchall()]
    print(f"토큰화 대상 {len(todo)}건", flush=True)

    upd = ("UPDATE corpus_chunks SET text_tokenized = :1 "
           "WHERE source_name = :2 AND src_id = :3 AND chunk_no = :4")
    rows = [(ko_tokenize.tokenize_for_search(text), *key) for key, text in todo]
    for i in range(0, len(rows), 500):
        cur.executemany(upd, rows[i:i + 500])
        con.commit()
    print(f"완료: {len(rows)}건")


if __name__ == "__main__":
    main()
