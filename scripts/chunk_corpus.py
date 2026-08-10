"""문서 청킹 배치 — corpus_docs -> corpus_chunks (docs/schema.md §5).

- 대상: 청크가 없거나, 문서가 청크 생성 이후 갱신된(updated_at) 문서. 멱등.
- 청크 텍스트 = title 접두 + 본문 슬라이스(오버랩 포함). char_start/end는 본문 기준.
- 임베딩은 여기서 만들지 않는다 — 03:30 embed_corpus.py가 embedding IS NULL 청크를 백필.
- 파라미터: app_settings(chunk_chars/chunk_overlap) > .env(CHUNK_CHARS/CHUNK_OVERLAP)

usage: python scripts/chunk_corpus.py   (야간 CronJob 03:15과 동일)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb

from tools import config, settings, source_registry


def spans(length: int, size: int, overlap: int):
    """본문 길이를 (start, end) 슬라이스 목록으로. size 이하면 1개."""
    if length <= size:
        return [(0, length)]
    out, start = [], 0
    step = max(size - overlap, 1)
    while start < length:
        end = min(length, start + size)
        out.append((start, end))
        if end >= length:
            break
        start += step
    return out


def main():
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    source_registry.ensure_corpus_chunks(cur)
    st = settings.get_all()
    size = max(200, settings.get_int(st, "chunk_chars", config.CHUNK_CHARS))
    overlap = min(max(0, settings.get_int(st, "chunk_overlap", config.CHUNK_OVERLAP)),
                  size - 1)
    # 청크가 없거나 문서가 청크보다 새로운 것만 (멱등 재청킹)
    cur.execute("""
        SELECT d.source_name, d.src_id, d.title, d.body FROM corpus_docs d
        WHERE NOT EXISTS (SELECT 1 FROM corpus_chunks c
                          WHERE c.source_name = d.source_name AND c.src_id = d.src_id
                          AND c.created_at >= NVL(d.updated_at, d.created_at))""")
    rows = cur.fetchall()
    print(f"청킹 대상 {len(rows)}건 (크기 {size}자, 겹침 {overlap}자)", flush=True)
    total, del_batch, ins_batch = 0, [], []

    def flush():
        nonlocal del_batch, ins_batch
        if del_batch:
            cur.executemany("""DELETE FROM corpus_chunks
                               WHERE source_name = :1 AND src_id = :2""", del_batch)
            del_batch = []
        if ins_batch:
            cur.executemany("""INSERT INTO corpus_chunks
                               (source_name, src_id, chunk_no, text, char_start, char_end)
                               VALUES (:1, :2, :3, :4, :5, :6)""", ins_batch)
            ins_batch = []
        con.commit()

    for sn, sid, title, body in rows:
        body = body.read() if hasattr(body, "read") else (body or "")
        prefix = (title or "").strip()[:300]
        del_batch.append([sn, sid])
        for no, (s, e) in enumerate(spans(len(body), size, overlap)):
            text = (prefix + "\n" + body[s:e]) if prefix else body[s:e]
            ins_batch.append([sn, sid, no, text, s, e])
            total += 1
        if len(ins_batch) >= 500:
            flush()
    flush()
    cur.execute("""SELECT COUNT(*), COUNT(CASE WHEN embedding IS NOT NULL THEN 1 END)
                   FROM corpus_chunks""")  # COUNT(BLOB)은 ORA-00932
    c, e = cur.fetchone()
    print(f"완료: 신규/갱신 청크 {total}건 — 전체 {c}건 (임베딩 {e}건, "
          f"나머지는 03:30 백필)")
    con.close()


if __name__ == "__main__":
    import time as _t
    from tools import events as _ev
    _t0 = _t.time()
    try:
        main()
        _ev.log("batch", source="chunk-corpus", level="info", status="ok",
                duration_ms=int((_t.time() - _t0) * 1000), summary="chunk-corpus 완료")
    except Exception as _e:
        import traceback as _tb
        _ev.log("batch", source="chunk-corpus", level="error", status="fail",
                duration_ms=int((_t.time() - _t0) * 1000),
                summary=f"{type(_e).__name__}: {str(_e)[:200]}",
                detail=_tb.format_exc())
        raise
