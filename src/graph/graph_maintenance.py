"""그래프 유지보수 배치 — design §2 운영 규칙 구현.

패스 1 (형제 통합): 같은 부모 밑 저빈도(통행<=2) 노드가 더 인기 있는 형제와
    유사하면(코사인>=0.70 + LLM 동일 의도 확인) 그 형제로 흡수.
    -> 2단계 판정의 과분리를 사후 교정. 가중치·출처는 흡수한 형제로 합산.
패스 2 (잎 흡수): 생성 후 MIN_AGE_DAYS 지나도 통행<=1인 3층 말단을 부모(목표)로
    흡수 -> 장기 비대화 방지. (접근법이 추천 단위라 신생 노드는 건드리지 않음)
패스 3 (시간 감쇠): 유예 기간 넘게 통행 없는 3층 접근법의 가중치를 반감기 곡선으로
    가라앉힘 (삭제 금지 — design §2 운영 규칙 5). 근거: AWM 표11 — 분포가 다른
    낡은 워크플로우가 새 것을 훼손. 신선도는 원인이 아니라 조치(접근법)에 붙는다.

멱등. 야간 CronJob(03:20) 또는 수동 실행.
usage: python -m graph.graph_maintenance [--age-days 14]
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import oracledb

from graph.graph_pipeline import LAYER_KIND, SIM_THRESHOLD, cosine, llm_same
from core import config

LOW_COUNT = config.MAINT_LOW_COUNT       # 패스1: 이 이하 통행이면 흡수 후보
ABSORB_COUNT = config.MAINT_ABSORB_COUNT  # 패스2: 이 이하 통행인 잎만 흡수
MIN_AGE_DAYS = config.MAINT_MIN_AGE_DAYS  # 패스2: "오래 지나도" 기준 (--age-days로 오버라이드)


def evidence_count(cur, nid):
    cur.execute("SELECT COUNT(*) FROM node_evidence WHERE node_id=:1", [nid])
    return cur.fetchone()[0]  # PK(node_id,kind,ref)라 행 수 = 고유 출처 수


def transfer_evidence(cur, src, dst):
    """src 노드의 출처를 dst로 승계 (dst에 이미 있는 출처는 건너뜀)."""
    cur.execute("""INSERT INTO node_evidence (node_id, kind, ref)
                   SELECT :dst, e1.kind, e1.ref FROM node_evidence e1
                   WHERE e1.node_id = :src AND NOT EXISTS
                     (SELECT 1 FROM node_evidence e2 WHERE e2.node_id = :dst
                      AND e2.kind = e1.kind AND e2.ref = e1.ref)""",
                {"src": src, "dst": dst})


def merge_into(cur, src, dst, parent):
    """src 노드를 dst(형제)로 흡수: 출처·가중치 합산, 자식 엣지 재연결, src 삭제.
    src의 잔여 엣지·출처는 nodes 삭제 시 FK 캐스케이드가 정리."""
    transfer_evidence(cur, src, dst)
    cur.execute("""SELECT raw_count, weight FROM edges WHERE src=:1 AND dst=:2""",
                [parent, src])
    r = cur.fetchone()
    if r:
        cur.execute("""UPDATE edges SET raw_count = raw_count + :c, weight = weight + :w
                       WHERE src = :p AND dst = :d""",
                    {"c": r[0], "w": r[1], "p": parent, "d": dst})
    # src의 자식(4층 도구 등) 엣지를 dst로 재연결
    cur.execute("SELECT dst, raw_count, weight FROM edges WHERE src=:1", [src])
    for child, c, w in cur.fetchall():
        cur.execute("""MERGE INTO edges e USING dual ON (e.src=:d AND e.dst=:ch)
                       WHEN MATCHED THEN UPDATE SET raw_count=raw_count+:c, weight=weight+:w
                       WHEN NOT MATCHED THEN INSERT (src,dst,raw_count,weight)
                       VALUES (:d,:ch,:c,:w)""",
                    {"d": dst, "ch": child, "c": c, "w": w})
    cur.execute("DELETE FROM nodes WHERE id=:1", [src])  # 엣지·출처는 캐스케이드


def pass1_sibling_merge(cur):
    merged = 0
    cur.execute("SELECT DISTINCT src FROM edges e JOIN nodes n ON n.id=e.dst WHERE n.layer BETWEEN 2 AND 7")
    for (parent,) in cur.fetchall():
        cur.execute("""SELECT n.id, n.name, n.layer, n.embedding FROM edges e
                       JOIN nodes n ON n.id=e.dst WHERE e.src=:1 AND n.layer BETWEEN 2 AND 7""",
                    [parent])
        sibs = [(nid, name, layer,
                 np.asarray(json.loads(emb.read() if hasattr(emb, "read") else emb),
                            dtype=np.float32) if emb else None,
                 evidence_count(cur, nid))
                for nid, name, layer, emb in cur.fetchall()]
        dead = set()  # 이번 순회에서 이미 흡수(삭제)된 형제 — 스냅샷이라 추적 필수
        for nid, name, layer, vec, cnt in sibs:
            if nid in dead or cnt > LOW_COUNT or vec is None:
                continue
            best = None
            for nid2, name2, _, vec2, cnt2 in sibs:
                if nid2 == nid or nid2 in dead or vec2 is None or cnt2 < cnt:
                    continue
                if vec.shape[0] != vec2.shape[0]:
                    continue  # 임베딩 모델 교체로 차원 다른 벡터 → 비교 불가(잘못된 병합 방지)
                sim = cosine(vec.tolist(), vec2.tolist())
                if sim >= SIM_THRESHOLD and (best is None or sim > best[0]):
                    best = (sim, nid2, name2)
            if best and llm_same(LAYER_KIND.get(layer, "개념"), name, best[2]):
                merge_into(cur, nid, best[1], parent)
                dead.add(nid)
                merged += 1
                print(f"  [형제 통합] '{name[:40]}' -> '{best[2][:40]}' (sim {best[0]:.2f})",
                      flush=True)
    return merged


def pass2_leaf_absorb(cur, age_days):
    absorbed = 0
    cur.execute("""SELECT e.src, n.id, n.name FROM edges e JOIN nodes n ON n.id=e.dst
                   WHERE n.role_tag = 'solution'
                   AND n.valid_from < SYSTIMESTAMP - NUMTODSINTERVAL(:1, 'DAY')""",
                [age_days])
    for parent, nid, name in cur.fetchall():
        if evidence_count(cur, nid) > ABSORB_COUNT:
            continue
        # 잎의 출처를 부모(목표)로 승계 후 삭제 (도구 자식은 고아가 되면 함께 정리)
        cur.execute("SELECT dst FROM edges WHERE src=:1", [nid])
        children = [r[0] for r in cur.fetchall()]
        transfer_evidence(cur, nid, parent)
        cur.execute("DELETE FROM nodes WHERE id=:1", [nid])  # 엣지·출처는 캐스케이드
        for ch in children:  # 다른 부모가 없는 고아 도구 노드 정리
            cur.execute("SELECT COUNT(*) FROM edges WHERE dst=:1", [ch])
            if not cur.fetchone()[0]:
                cur.execute("DELETE FROM nodes WHERE id=:1", [ch])
        absorbed += 1
        print(f"  [잎 흡수] '{name[:50]}' -> 부모 목표로", flush=True)
    return absorbed


def pass3_decay(cur):
    """유예(GRACE) 넘게 통행 없는 3층 접근법 엣지의 weight를 반감기 곡선으로 감쇠.

    멱등 보장: 현재 weight에 배수를 곱하지 않는다(반복 실행 시 겹감쇠).
    대신 raw_count·채택률에서 기준 가중치를 재계산(graph_pipeline.recompute_weights와
    같은 식)한 뒤 유휴 기간에서 유도한 감쇠 배수를 곱한다 — 몇 번 돌려도 같은 값.
    마지막 통행 = 그 노드를 증거로 가진 세션의 최신 ts (없으면 노드 생성 시각).
    """
    half = config.MAINT_DECAY_HALF_LIFE_DAYS
    grace = config.MAINT_DECAY_GRACE_DAYS
    floor = config.MAINT_DECAY_FLOOR
    now = datetime.now()
    decayed = 0
    cur.execute("""SELECT e.src, e.dst, e.raw_count, e.weight, n.name,
                          NVL((SELECT MAX(s.ts) FROM node_evidence ev
                               JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                               WHERE ev.node_id = n.id AND ev.kind = 'session'),
                              n.valid_from) AS last_use
                   FROM edges e JOIN nodes n ON n.id = e.dst
                   WHERE n.role_tag = 'solution'""")
    for src, dst, raw, w, name, last in cur.fetchall():
        idle = (now - last).total_seconds() / 86400 if last else 0.0
        if idle <= grace:
            continue
        cur.execute("""SELECT COUNT(*), SUM(CASE WHEN adopted='Y' THEN 1 ELSE 0 END)
                       FROM suggestions WHERE node_id = :1 AND adopted IS NOT NULL""",
                    [dst])
        e_cnt, a_cnt = cur.fetchone()
        a_cnt = a_cnt or 0
        rate = (a_cnt / e_cnt) if e_cnt else 1.0
        base = max((raw or 0) - a_cnt, 0) + a_cnt * rate  # 노출 대비 채택률 보정 기준치
        factor = max(floor, 0.5 ** ((idle - grace) / half))
        new_w = round(base * factor, 2)
        if abs(new_w - (w or 0)) >= 0.01:
            cur.execute("UPDATE edges SET weight = :1 WHERE src = :2 AND dst = :3",
                        [new_w, src, dst])
            decayed += 1
            print(f"  [감쇠] '{name[:40]}' 유휴 {idle:.0f}d -> x{factor:.2f} "
                  f"(w {w} -> {new_w})", flush=True)
    return decayed


def pass_attr_embed(cur, limit=500):
    """속성 노드(9층) 임베딩 백필 — 벡터가 없으면 검색 진입에서 이 값에 닿지 못한다.
    병합 기준은 여전히 이름 정확일치라 벡터는 조회에만 쓴다 (merge.upsert_entity 주석).
    새로 만드는 노드는 생성 시 채워지고, 이 pass는 그 전에 만들어진 노드를 메꾼다."""
    cur.execute(f"""SELECT id, name FROM nodes
                    WHERE layer = 9 AND embedding IS NULL AND name IS NOT NULL
                    FETCH FIRST {int(limit)} ROWS ONLY""")
    rows = cur.fetchall()
    if not rows:
        print("  [속성 임베딩] 백필 대상 없음", flush=True)
        return 0
    from graph.graph_pipeline.llm import embed
    done = 0
    for nid, name in rows:
        try:
            vec = embed(name, "")
        except Exception as e:
            print(f"  [속성 임베딩] 중단 — {type(e).__name__}: {str(e)[:100]}", flush=True)
            break            # 엔드포인트가 죽었으면 나머지도 실패한다 — 다음 배치에서 재시도
        cur.execute("UPDATE nodes SET embedding = :1 WHERE id = :2",
                    [json.dumps(vec).encode(), nid])
        done += 1
    print(f"  [속성 임베딩] {done}/{len(rows)}건 백필", flush=True)
    return done


def _attr_norm(s: str) -> str:
    """표기 비교용 정규화 — 대소문자·공백·구분기호를 지운다 ('데이터 전처리'→'데이터전처리')."""
    return re.sub(r"[\s_·\-/]+", "", (s or "")).lower()


def _attr_mergeable(a: str, b: str) -> str:
    """표기 변이 판정 — "same"(정규화 동일) / "sub"(한쪽이 다른 쪽을 품음) / ""(아님).

    실측(2026-08-21, 속성 188개)에서 두 종류가 섞여 나왔다:
    - 대소문자·공백만 다른 확실한 변이 — TensorFlow/Tensorflow, pandas/Pandas,
      딥 러닝/딥러닝 → "same". 판정이 자명해 LLM 확인 없이 합친다.
    - 포함 관계 — 전처리/데이터 전처리(같은 값), sqlite/SQL(다른 값) → "sub".
      자명하지 않아 LLM 확인을 거친다.

    제외 규칙:
    - 숫자 포함: '2024-03'/'2024-04', 'Python 2'/'Python 3' — 버전·시점이 의미를 가른다
    - 쉼표 포함: 'pandas, matplotlib' — 한 칸에 여러 값을 넣은 추출 오류라 통합 대상이
      아니다 (프롬프트의 값 규칙으로 예방한다)"""
    if any(ch.isdigit() for ch in a + b) or "," in a or "," in b:
        return ""
    na, nb = _attr_norm(a), _attr_norm(b)
    if not na or not nb:
        return ""
    if na == nb:
        return "same"
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    return "sub" if len(short) >= 2 and short in long_ else ""


def merge_attr_into(cur, src, dst):
    """속성 노드 흡수 — 부모가 여럿이라 pass1의 merge_into(부모 1개)를 못 쓴다.
    모든 부모 엣지를 dst로 재연결하고 증거(run_id 포함)를 승계한 뒤 src를 삭제한다."""
    cur.execute("""INSERT INTO node_evidence (node_id, kind, ref, run_id)
                   SELECT :dst, e1.kind, e1.ref, e1.run_id FROM node_evidence e1
                   WHERE e1.node_id = :src AND NOT EXISTS
                     (SELECT 1 FROM node_evidence e2 WHERE e2.node_id = :dst
                      AND e2.kind = e1.kind AND e2.ref = e1.ref AND e2.run_id = e1.run_id)""",
                {"src": src, "dst": dst})
    cur.execute("SELECT src, raw_count, weight FROM edges WHERE dst = :1", [src])
    for parent, c, w in cur.fetchall():
        cur.execute("""MERGE INTO edges e USING dual ON (e.src = :p AND e.dst = :d)
                       WHEN MATCHED THEN UPDATE SET raw_count = raw_count + :c, weight = weight + :w
                       WHEN NOT MATCHED THEN INSERT (src, dst, raw_count, weight)
                       VALUES (:p, :d, :c, :w)""",
                    {"p": parent, "d": dst, "c": c, "w": w})
    cur.execute("DELETE FROM nodes WHERE id = :1", [src])   # 엣지·증거는 FK 캐스케이드


def pass_attr_consolidate(cur):
    """속성 값 표기 통합 — '전처리'/'데이터 전처리'처럼 같은 값이 표기만 달라 노드가
    갈라진 것을 합친다. 구조화 시점 병합은 정확일치만 하므로(2024-03/04 오병합 방지)
    표기 변이는 여기서 사후에 정리한다.

    가드: ① 같은 entity_type ② 문자 가드(_attr_mergeable) ③ 정규화 동일("same")은
    바로 병합, 포함 관계("sub")는 코사인 >= SIM_THRESHOLD를 예비 필터로 쓰고 **LLM이 판정**
    (pass1_sibling_merge와 같은 방식). SIM_HIGH(0.92)로 잡으면 '전처리'/'데이터 전처리'
    (실측 0.841) 같은 진짜 변이가 걸러져 나가고, 반대로 벡터만 높은 'SQL'/'MySQL'(0.864)은
    통과해버린다 — 의미 판정은 벡터가 아니라 LLM 몫이다.
    남기는 쪽 = 증거 많은 노드 (동수면 짧은 표기 = 표준형). 병합은 전부 로그로 남긴다."""
    cur.execute("""SELECT entity_type, id, name, embedding FROM nodes
                   WHERE layer = 9 AND embedding IS NOT NULL AND name IS NOT NULL
                   ORDER BY entity_type""")
    by_type = {}
    for etype, nid, name, emb in cur.fetchall():
        vec = json.loads(emb.read() if hasattr(emb, "read") else emb)
        by_type.setdefault(etype or "", []).append(
            [nid, name, vec, evidence_count(cur, nid)])
    merged = 0
    for etype, rows in by_type.items():
        dead = set()
        for i, (nid, name, vec, cnt) in enumerate(rows):
            if nid in dead:
                continue
            for nid2, name2, vec2, cnt2 in rows[i + 1:]:
                if nid2 in dead or len(vec) != len(vec2):
                    continue
                how = _attr_mergeable(name, name2)
                if not how:
                    continue
                if how == "sub":   # 포함 관계는 자명하지 않다 — 벡터는 예비 필터, 판정은 LLM
                    if cosine(vec, vec2) < SIM_THRESHOLD:
                        continue
                    if not llm_same(f"{etype or '값'}(같은 대상의 다른 표기인지)", name, name2):
                        continue
                # 남길 쪽: 증거 많은 노드, 동수면 짧은 표기(표준형)
                keep, drop = ((nid, name, cnt), (nid2, name2, cnt2)) \
                    if (cnt, -len(name)) >= (cnt2, -len(name2)) else \
                    ((nid2, name2, cnt2), (nid, name, cnt))
                merge_attr_into(cur, drop[0], keep[0])
                dead.add(drop[0])
                merged += 1
                print(f"  [속성 통합] '{drop[1]}'({drop[2]}건) -> '{keep[1]}'({keep[2]}건)"
                      f" · {etype} · {how}", flush=True)
                if drop[0] == nid:
                    break     # 지금 노드가 흡수됐으면 이 노드의 남은 비교는 무의미
    # 부모 없는 속성 노드 = 붙어 있던 목표가 사라진 죽은 값. 검색 인덱스에는 남아
    # 진입 후보로 잡히지만 올라갈 목표가 없어 아무 경로도 못 준다 — 지운다.
    cur.execute("""DELETE FROM nodes WHERE layer = 9
                   AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst = nodes.id)""")
    dead_attr = cur.rowcount
    if dead_attr:
        print(f"  [속성 통합] 부모 없는 속성 노드 {dead_attr}개 제거", flush=True)
    print(f"  [속성 통합] {merged}건", flush=True)
    return merged


def pass4_integrity(cur):
    """무결성 점검 리포트 — FK가 못 막는 참조를 검사해 위반을 침묵이 아닌 리포트로.
    (노드 참조는 FK 캐스케이드가 강제하므로 여기선 '바깥' 참조만: 세션·문서·도메인)
    자동 삭제하지 않는다 — 위반은 버그 신호이므로 사람이 원인을 봐야 한다."""
    checks = [
        ("세션 증거가 가리키는 세션 없음",
         """SELECT COUNT(*) FROM node_evidence ev WHERE ev.kind = 'session'
            AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = ev.ref)"""),
        ("문서 증거가 가리키는 corpus 문서 없음",
         """SELECT COUNT(*) FROM node_evidence ev WHERE ev.kind = 'doc'
            AND NOT EXISTS (SELECT 1 FROM corpus_docs d
                            WHERE d.source_name || ':' || d.src_id = ev.ref)"""),
        ("노출 기록이 가리키는 세션 없음",
         """SELECT COUNT(*) FROM suggestions g WHERE g.session_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = g.session_id)"""),
        ("1층 노드가 도메인 닫힌 목록에 없음",
         """SELECT COUNT(*) FROM nodes n WHERE n.layer = 1
            AND NOT EXISTS (SELECT 1 FROM domain_registry d WHERE d.name = n.name)"""),
    ]
    bad = 0
    for label, q in checks:
        cur.execute(q)
        n = cur.fetchone()[0]
        if n:
            print(f"  [무결성 위반] {label}: {n}건", flush=True)
            bad += n
    if not bad:
        print("  [무결성] 위반 없음", flush=True)
    return bad


def main():
    age = MIN_AGE_DAYS
    if "--age-days" in sys.argv:
        age = int(sys.argv[sys.argv.index("--age-days") + 1])
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM nodes")
    before = cur.fetchone()[0]
    print(f"유지보수 시작 (노드 {before})", flush=True)
    m = pass1_sibling_merge(cur)
    a = pass2_leaf_absorb(cur, age)
    d = pass3_decay(cur)
    e = pass_attr_embed(cur)
    con.commit()
    ac = pass_attr_consolidate(cur)
    con.commit()
    bad = pass4_integrity(cur)
    cur.execute("SELECT COUNT(*) FROM nodes")
    print(f"완료: 형제 통합 {m}건, 잎 흡수 {a}건 (age>={age}d), 감쇠 {d}건, "
          f"속성 임베딩 {e}건, 속성 통합 {ac}건, 무결성 위반 {bad}건 "
          f"— 노드 {before} -> {cur.fetchone()[0]}")
    con.close()
    # 활동 로그 보관 회전 — "전부 쌓기"의 무한 증가 방지 (config.EVENTS_RETAIN_DAYS)
    from core import events
    purged = events.purge_old()
    print(f"활동 로그 회전: {purged}건 삭제 (보관 {config.EVENTS_RETAIN_DAYS}일)")


if __name__ == "__main__":
    import time as _t
    from core import events as _ev
    _t0 = _t.time()
    try:
        main()
        _ev.log("batch", source="graph-maintenance", level="info", status="ok",
                duration_ms=int((_t.time() - _t0) * 1000), summary="graph-maintenance 완료")
    except Exception as _e:
        import traceback as _tb
        _ev.log("batch", source="graph-maintenance", level="error", status="fail",
                duration_ms=int((_t.time() - _t0) * 1000),
                summary=f"{type(_e).__name__}: {str(_e)[:200]}",
                detail=_tb.format_exc())
        raise
