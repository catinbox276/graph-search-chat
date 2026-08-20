"""DB 스냅샷 덤프 (SELECT 전용) — 로컬 스토리지 소실 대비 백업.

dalgo 클러스터의 Oracle은 노드 고정 로컬 PV(ReclaimPolicy=Delete)라 노드 이동·교체로
소실될 수 있다. 게다가 원천 테이블이 사라진 소스(예: BLOG_POSTS)는 corpus_docs를
재적재할 방법이 없다 — 그 코퍼스와 설정·버전 테이블이 이 덤프의 핵심.

제외 대상 (배치가 재생성 가능 — 덤프를 작게 유지):
- corpus_chunks: ingestion.chunk_corpus
- embedding BLOB: ingestion.embed_corpus (모델 불일치분 자동 재백필)

사용:
    PYTHONPATH=src python -m core.db_dump [출력디렉터리]         # 기본 /tmp/dbdump
    # 클러스터 밖으로 빼기 (파드에서 실행 후)
    kubectl exec -n <ns> <pod> -- sh -c 'cd /srv && PYTHONPATH=src python -m core.db_dump /tmp/dbdump'
    kubectl exec -n <ns> <pod> -- tar czf - -C /tmp dbdump > dbdump-$(date +%F).tgz

복원은 core.db_restore.
"""
import datetime
import decimal
import json
import os
import sys

import oracledb

from core import config

# 복원 순서와 같은 순서로 나열 (FK 부모 먼저) — db_restore.TABLES와 한 곳에서 공유
from core.db_restore import TABLES

SKIP_COLS = {"EMBEDDING"}   # BLOB — 백필 배치가 재생성


def _val(v):
    """JSON 직렬화 가능한 값으로 — LOB은 읽고, 시각은 ISO, BLOB은 버린다."""
    if hasattr(v, "read"):
        return v.read()
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return None
    return v


def dump(out_dir: str = "/tmp/dbdump") -> dict:
    """테이블별 JSONL 파일로 덤프 — {테이블: 행수|'없음'} 반환."""
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                           dsn=config.ORACLE_DSN)
    cur = con.cursor()
    os.makedirs(out_dir, exist_ok=True)
    summary = {}
    for t in TABLES:
        cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [t])
        if not cur.fetchone()[0]:
            summary[t] = "없음"
            continue
        cur.execute(f"SELECT * FROM {t}")                      # noqa: S608 — 상수 목록
        cols = [d[0] for d in cur.description]
        keep = [i for i, c in enumerate(cols) if c not in SKIP_COLS]
        names = [cols[i] for i in keep]
        n = 0
        with open(os.path.join(out_dir, t.lower() + ".jsonl"), "w", encoding="utf-8") as f:
            while True:
                rows = cur.fetchmany(500)
                if not rows:
                    break
                for r in rows:
                    f.write(json.dumps({names[j]: _val(r[i]) for j, i in enumerate(keep)},
                                       ensure_ascii=False) + "\n")
                    n += 1
        summary[t] = n
    con.close()
    with open(os.path.join(out_dir, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"dumped_at": datetime.datetime.now().isoformat(),
                   "dsn": config.ORACLE_DSN, "tables": summary,
                   "excluded": ["CORPUS_CHUNKS(청킹 배치 재생성)", "EMBEDDING(백필 재생성)"]},
                  f, ensure_ascii=False, indent=1)
    return summary


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dbdump"
    print(json.dumps(dump(out), ensure_ascii=False, indent=1))
    print(f"→ {out} (_manifest.json 포함)", flush=True)
