"""가중치 보정 — 노출 대비 채택률 재계산 + 재발 소급 취소.

- recompute_weights: weight = 자발 통행 + 채택 통행 × 채택률 (피드백 루프 차단)
- retract_recurrences: 같은 사용자·같은 증상 재방문 시 앞선 success를 소급 취소
"""
from core import config

from .gate import _read
from .llm import cosine, embed


def recompute_weights(cur):
    """노출 대비 채택률 보정: weight = 자발 통행 + 채택 통행 x 채택률.

    제안에 노출돼 생긴 통행은 채택률만큼 할인 -> "많이 보여줘서 많이 간 길"이
    "좋은 길"로 굳는 피드백 루프 차단 (research.md 위험 1 대책).
    채택 수는 노드 단위, raw_count는 엣지 단위 — 3층은 부모가 대개 1개라 근사 적용.
    """
    cur.execute("""SELECT node_id, COUNT(*) exposures,
                          SUM(CASE WHEN adopted='Y' THEN 1 ELSE 0 END) adoptions
                   FROM suggestions WHERE adopted IS NOT NULL
                   GROUP BY node_id""")
    updated = 0
    for nid, e, a in cur.fetchall():
        rate = (a / e) if e else 1.0
        cur.execute("SELECT src, raw_count FROM edges WHERE dst = :1", [nid])
        for src, raw in cur.fetchall():
            organic = max(raw - a, 0)
            corrected = round(organic + a * rate, 2)
            cur.execute("UPDATE edges SET weight = :w WHERE src = :s AND dst = :d",
                        {"w": corrected, "s": src, "d": nid})
            updated += 1
    if updated:
        print(f"가중치 보정: 엣지 {updated}건 (노출 대비 채택률 반영)", flush=True)


def retract_recurrences(cur, task_ids):
    """재발 = 지연 판정기 (design §3·§4 역방향 supersession).

    success로 판정된 UI 세션과 첫 질문이 유사한(코사인 >= SIG_REPEAT_SIM) UI 세션이
    RECUR_DAYS 안에 다시 시작되면 — 그때의 해결은 사실이 아니었으므로 — 앞선 판정을
    'retracted'로 소급 취소하고 그 세션이 올린 엣지 가중치·통행을 1씩 회수한다.
    노드·증거는 보존 (성공/실패는 불리언이 아니라 판정 카운트 — 'retracted'는
    path_suggest의 success 집계에서 자연히 빠진다).
    태스크 세션(selfplay)은 제외 — 반복 태스크는 의도된 재실행이지 재발이 아니다.
    재발 매칭은 같은 사용자(user_id — SSO 로그인) 안에서만 한다. user_id가 없는
    구세션끼리는 종전처럼 한 사용자로 근사한다(다른 사람이 같은 문제를 만난 건
    재발이 아니라 오히려 경로가 유효하다는 신호이므로 교차 매칭 금지).
    """
    cur.execute("SELECT id, ts, question, verdict, user_id FROM sessions "
                "WHERE turn = 1 ORDER BY ts")
    sess = [(sid, ts, _read(q), v, uid) for sid, ts, q, v, uid in cur.fetchall()
            if sid.split("-")[0] not in task_ids and ts is not None]
    vec_cache = {}

    def qvec(sid, q):
        if sid not in vec_cache:
            vec_cache[sid] = embed(q[:500])
        return vec_cache[sid]

    retracted = 0
    for i, (sid, ts0, q, v, uid) in enumerate(sess):
        if v != "success":
            continue
        for sid2, ts2, q2, _v2, uid2 in sess[i + 1:]:
            if (ts2 - ts0).total_seconds() / 86400 > config.RECUR_DAYS:
                break
            if uid != uid2:  # 다른 사용자의 같은 질문은 재발이 아님
                continue
            if cosine(qvec(sid, q), qvec(sid2, q2)) < config.SIG_REPEAT_SIM:
                continue
            cur.execute("""SELECT node_id FROM node_evidence
                           WHERE kind = 'session' AND ref = :1""", [sid])
            nids = [r[0] for r in cur.fetchall()]
            for j in range(0, len(nids), 100):
                chunk = nids[j:j + 100]
                src_marks = ",".join(f":s{k}" for k in range(len(chunk)))
                dst_marks = ",".join(f":d{k}" for k in range(len(chunk)))
                binds = {f"s{k}": v for k, v in enumerate(chunk)}
                binds.update({f"d{k}": v for k, v in enumerate(chunk)})
                cur.execute(  # 이 세션이 +1씩 올렸던 경로 엣지에서 기여 회수
                    f"""UPDATE edges SET raw_count = GREATEST(raw_count - 1, 0),
                                         weight = GREATEST(weight - 1, 0)
                        WHERE src IN ({src_marks}) AND dst IN ({dst_marks})""",
                    binds)
            cur.execute("UPDATE sessions SET verdict = 'retracted' "
                        "WHERE id = :1 AND turn = 1", [sid])
            retracted += 1
            print(f"  [재발 소급취소] {sid} <- {sid2} "
                  f"({(ts2 - ts0).total_seconds() / 86400:.1f}일 뒤 같은 증상)", flush=True)
            break
    return retracted
