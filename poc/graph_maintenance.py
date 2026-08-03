"""그래프 유지보수 배치 — design §2 운영 규칙 구현.

패스 1 (형제 통합): 같은 부모 밑 저빈도(통행<=2) 노드가 더 인기 있는 형제와
    유사하면(코사인>=0.70 + LLM 동일 의도 확인) 그 형제로 흡수.
    -> 2단계 판정의 과분리를 사후 교정. 가중치·출처는 흡수한 형제로 합산.
패스 2 (잎 흡수): 생성 후 MIN_AGE_DAYS 지나도 통행<=1인 3층 말단을 부모(목표)로
    흡수 -> 장기 비대화 방지. (접근법이 추천 단위라 신생 노드는 건드리지 않음)

멱등. 야간 CronJob(03:20) 또는 수동 실행.
usage: python -m poc.graph_maintenance [--age-days 14]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import oracledb

from poc.graph_pipeline import LAYER_KIND, SIM_THRESHOLD, cosine, llm_same
from tools import config
from tools.blog_search import DSN, PASSWORD, USER

LOW_COUNT = config.MAINT_LOW_COUNT       # 패스1: 이 이하 통행이면 흡수 후보
ABSORB_COUNT = config.MAINT_ABSORB_COUNT  # 패스2: 이 이하 통행인 잎만 흡수
MIN_AGE_DAYS = config.MAINT_MIN_AGE_DAYS  # 패스2: "오래 지나도" 기준 (--age-days로 오버라이드)


def evidence_count(cur, nid):
    cur.execute("SELECT COUNT(DISTINCT session_id) FROM node_evidence WHERE node_id=:1", [nid])
    return cur.fetchone()[0]


def merge_into(cur, src, dst, parent):
    """src 노드를 dst(형제)로 흡수: 출처·가중치 합산, 자식 엣지 재연결, src 삭제."""
    cur.execute("""INSERT INTO node_evidence (node_id, session_id)
                   SELECT :dst, session_id FROM node_evidence
                   WHERE node_id = :src AND session_id NOT IN
                     (SELECT session_id FROM node_evidence WHERE node_id = :dst)""",
                {"src": src, "dst": dst})
    cur.execute("DELETE FROM node_evidence WHERE node_id = :1", [src])
    cur.execute("""SELECT raw_count, weight FROM edges WHERE src=:1 AND dst=:2""",
                [parent, src])
    r = cur.fetchone()
    if r:
        cur.execute("""UPDATE edges SET raw_count = raw_count + :c, weight = weight + :w
                       WHERE src = :p AND dst = :d""",
                    {"c": r[0], "w": r[1], "p": parent, "d": dst})
        cur.execute("DELETE FROM edges WHERE src=:1 AND dst=:2", [parent, src])
    # src의 자식(4층 도구 등) 엣지를 dst로 재연결
    cur.execute("SELECT dst, raw_count, weight FROM edges WHERE src=:1", [src])
    for child, c, w in cur.fetchall():
        cur.execute("""MERGE INTO edges e USING dual ON (e.src=:d AND e.dst=:ch)
                       WHEN MATCHED THEN UPDATE SET raw_count=raw_count+:c, weight=weight+:w
                       WHEN NOT MATCHED THEN INSERT (src,dst,raw_count,weight)
                       VALUES (:d,:ch,:c,:w)""",
                    {"d": dst, "ch": child, "c": c, "w": w})
    cur.execute("DELETE FROM edges WHERE src=:1", [src])
    cur.execute("DELETE FROM nodes WHERE id=:1", [src])


def pass1_sibling_merge(cur):
    merged = 0
    cur.execute("SELECT DISTINCT src FROM edges e JOIN nodes n ON n.id=e.dst WHERE n.layer IN (2,3)")
    for (parent,) in cur.fetchall():
        cur.execute("""SELECT n.id, n.name, n.layer, n.embedding FROM edges e
                       JOIN nodes n ON n.id=e.dst WHERE e.src=:1 AND n.layer IN (2,3)""",
                    [parent])
        sibs = [(nid, name, layer,
                 np.asarray(json.loads(emb.read()), dtype=np.float32) if emb else None,
                 evidence_count(cur, nid))
                for nid, name, layer, emb in cur.fetchall()]
        for nid, name, layer, vec, cnt in sibs:
            if cnt > LOW_COUNT or vec is None:
                continue
            best = None
            for nid2, name2, _, vec2, cnt2 in sibs:
                if nid2 == nid or vec2 is None or cnt2 < cnt:
                    continue
                sim = cosine(vec.tolist(), vec2.tolist())
                if sim >= SIM_THRESHOLD and (best is None or sim > best[0]):
                    best = (sim, nid2, name2)
            if best and llm_same(LAYER_KIND.get(layer, "개념"), name, best[2]):
                merge_into(cur, nid, best[1], parent)
                merged += 1
                print(f"  [형제 통합] '{name[:40]}' -> '{best[2][:40]}' (sim {best[0]:.2f})",
                      flush=True)
    return merged


def pass2_leaf_absorb(cur, age_days):
    absorbed = 0
    cur.execute("""SELECT e.src, n.id, n.name FROM edges e JOIN nodes n ON n.id=e.dst
                   WHERE n.layer = 3
                   AND n.valid_from < SYSTIMESTAMP - NUMTODSINTERVAL(:1, 'DAY')""",
                [age_days])
    for parent, nid, name in cur.fetchall():
        if evidence_count(cur, nid) > ABSORB_COUNT:
            continue
        # 잎의 출처를 부모(목표)로 승계 후 삭제 (도구 자식은 고아가 되면 함께 정리)
        cur.execute("SELECT dst FROM edges WHERE src=:1", [nid])
        children = [r[0] for r in cur.fetchall()]
        cur.execute("""INSERT INTO node_evidence (node_id, session_id)
                       SELECT :p, session_id FROM node_evidence
                       WHERE node_id = :n AND session_id NOT IN
                         (SELECT session_id FROM node_evidence WHERE node_id = :p)""",
                    {"p": parent, "n": nid})
        cur.execute("DELETE FROM node_evidence WHERE node_id=:1", [nid])
        cur.execute("DELETE FROM edges WHERE src=:1 OR dst=:1", [nid])
        cur.execute("DELETE FROM nodes WHERE id=:1", [nid])
        for ch in children:  # 다른 부모가 없는 고아 도구 노드 정리
            cur.execute("SELECT COUNT(*) FROM edges WHERE dst=:1", [ch])
            if not cur.fetchone()[0]:
                cur.execute("DELETE FROM node_evidence WHERE node_id=:1", [ch])
                cur.execute("DELETE FROM nodes WHERE id=:1", [ch])
        absorbed += 1
        print(f"  [잎 흡수] '{name[:50]}' -> 부모 목표로", flush=True)
    return absorbed


def main():
    age = MIN_AGE_DAYS
    if "--age-days" in sys.argv:
        age = int(sys.argv[sys.argv.index("--age-days") + 1])
    con = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM nodes")
    before = cur.fetchone()[0]
    print(f"유지보수 시작 (노드 {before})", flush=True)
    m = pass1_sibling_merge(cur)
    a = pass2_leaf_absorb(cur, age)
    con.commit()
    cur.execute("SELECT COUNT(*) FROM nodes")
    print(f"완료: 형제 통합 {m}건, 잎 흡수 {a}건 (age>={age}d) — 노드 {before} -> {cur.fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()
