"""파이프라인 오케스트레이션 — sessions 조회 → 게이트 → 추출 → 병합 → 보정 → 요약.

세션 게이트 2갈래 (design §3):
- 태스크 세션(selfplay) — tasks.yaml의 expect 기준 LLM 판정 (JUDGE_PROMPT)
- 실사용(UI) 세션 — 세그먼트 분할 후 행동 신호 코드 판정, LLM은 적합성(fits·grounded)
  판정과 목표·접근법 표현 추출만 (EXTRACT_PROMPT)
"""
import json
from pathlib import Path

import oracledb
import yaml

from core import config

from .gate import judge_by_signals, session_turns, split_segments
from .llm import EXTRACT_PROMPT, JUDGE_PROMPT, _llm_json
from .merge import get_or_create
from .schema import classify_domain, ddl
from .weights import recompute_weights, retract_recurrences

TASKS_YAML = Path(__file__).resolve().parents[1] / "tasks.yaml"


def expects():
    tasks = yaml.safe_load(open(TASKS_YAML))
    out = {}
    for group in ("repeat", "single", "fail"):
        for t in tasks[group]:
            out[t["id"]] = t.get("expect") or f"실패 인정 기대: {t['expect_fail']}"
    return out


def main():
    exp = expects()
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    ddl(cur)
    cur.execute("""SELECT id, question, tool_calls, answer FROM sessions
                   WHERE turn = 1 AND verdict IS NULL ORDER BY id""")
    rows = [(r[0], r[1].read(), r[2].read(), r[3].read()) for r in cur.fetchall()]
    print(f"판정 대상 {len(rows)}세션")
    for n, (sid, q, calls_json, answer) in enumerate(rows, 1):
        calls = json.loads(calls_json or "[]")
        task_id = sid.split("-")[0]
        sig_detail = ""
        if task_id in exp:
            # 태스크 세션(selfplay) — expect 기준 LLM 판정 (기존 흐름)
            tool_names = {c["name"] for c in calls}
            domain, hint = classify_domain(cur, tool_names)
            prompt = JUDGE_PROMPT.format(
                question=q, tools=json.dumps(calls, ensure_ascii=False)[:2000],
                answer=answer[:3000], expect=exp[task_id])
            if hint:
                prompt += f"\n\n[도메인 추출 지침 — {domain}] {hint}"
            j = _llm_json(prompt)
            verdict = j.get("verdict", "unknown")
            if verdict not in ("success", "fail"):
                verdict = "unknown"
            contribs = [(domain, j, verdict, tool_names)] if verdict != "unknown" else []
        else:
            # 실사용(UI) 세션 — 판단은 코드(행동 신호), LLM은 표현 추출만 (design §3 보강).
            # 세션을 태스크 세그먼트로 분할해 세그먼트마다 게이트·추출 독립 적용 —
            # "세션 1개 = 문제 1개" 가정 보강 (A 풀고 B로 넘어간 세션의 자산 회수).
            turns = session_turns(cur, sid)
            segs = split_segments(turns)
            contribs, details = [], []
            for seg in segs:
                v, det = judge_by_signals(seg)
                details.append(f"{v}" + (f":{det}" if det else ""))
                if v == "unknown":
                    continue
                calls = [c for t in seg for c in t["calls"]]
                tool_names = {c["name"] for c in calls}
                domain, hint = classify_domain(cur, tool_names)
                prompt = EXTRACT_PROMPT.format(
                    domain=domain,
                    question=seg[0]["q"][:2000],
                    tools=json.dumps(calls, ensure_ascii=False)[:2000],
                    answer=seg[-1]["a"][:3000])
                if hint:
                    prompt += f"\n\n[도메인 추출 지침 — {domain}] {hint}"
                j = _llm_json(prompt)
                # 도메인 게이트(문서와 대칭): 잡담·일반 상식은 그래프 기여 없음
                if not j.get("fits"):
                    details[-1] += "→도메인 밖(기여 제외)"
                    continue
                # 공로 귀속: 도구가 기여하지 않은 답변은 경로로 기록하지 않음
                # ("검색으로 해결" 거짓 경로 방지 — 성공 판정 자체는 유지)
                if not j.get("grounded"):
                    details[-1] += "→도구 기여 없음(기여 보류)"
                    continue
                if v == "fail":
                    j["fail_reason"] = f"행동 신호: {det}"
                contribs.append((domain, j, v, tool_names))
            # 세션 대표 판정: 세그먼트 판정이 한 방향일 때만 채택.
            # 성공·실패 혼합은 unknown + 기여 없음 — 카운트 조인(ref=세션id ↔
            # turn=1 verdict) 계약을 지키는 안전 폴백 (혼합을 세그먼트별로 세려면
            # 증거-세그먼트 연결이 필요해 스키마가 커진다. 필요해지면 그때).
            seg_verdicts = {v for (_d, _j, v, _t) in contribs}
            if seg_verdicts == {"success"}:
                verdict = "success"
            elif seg_verdicts == {"fail"}:
                verdict = "fail"
            else:
                verdict = "unknown"
                contribs = []
            sig_detail = " | ".join(details) + \
                (f" [{len(segs)}세그먼트]" if len(segs) > 1 else "")
        cur.execute("UPDATE sessions SET verdict = :1 WHERE id = :2 AND turn = 1",
                    [verdict, sid])
        for domain, j, v, tool_names in contribs:
            if not (j.get("goal") and j.get("approach")):
                continue
            d = get_or_create(cur, 1, domain, None, "session", sid, use_embedding=False)
            g = get_or_create(cur, 2, j["goal"], d, "session", sid)
            a = get_or_create(cur, 3, j["approach"], g, "session", sid)
            if v == "fail":
                cur.execute("""UPDATE nodes SET fail_flag='Y', fail_reason=:1
                               WHERE id=:2""", [(j.get("fail_reason") or "")[:1000], a])
            for tool in sorted(tool_names):
                get_or_create(cur, 4, f"tool:{tool}", a, "session", sid,
                              use_embedding=False)
        # 채택 판정: 이 세션에 노출된 제안 노드를 실제로 사용했는가 (유도 vs 자발 구분의 기초)
        cur.execute("""UPDATE suggestions s SET adopted =
            CASE WHEN EXISTS (SELECT 1 FROM node_evidence ev
                              WHERE ev.kind = 'session' AND ev.ref = :sid
                                AND ev.node_id = s.node_id)
                 THEN 'Y' ELSE 'N' END
            WHERE s.session_id = :sid AND s.adopted IS NULL""", {"sid": sid})
        con.commit()
        tag = f" ({sig_detail})" if sig_detail else ""
        print(f"[{n}/{len(rows)}] {sid} -> {verdict}{tag}", flush=True)

    r = retract_recurrences(cur, set(exp))  # 재발 = 지연 판정기 (소급 취소)
    if r:
        print(f"재발 소급 취소: {r}건", flush=True)
    recompute_weights(cur)
    con.commit()

    # 결과 요약
    cur.execute("SELECT verdict, COUNT(*) FROM sessions WHERE REGEXP_LIKE(id,'^[RSF]') GROUP BY verdict")
    print("판정 분포:", dict(cur.fetchall()))
    cur.execute("SELECT layer, COUNT(*) FROM nodes GROUP BY layer ORDER BY layer")
    print("계층별 노드:", dict(cur.fetchall()))
    cur.execute("""SELECT n1.name, n2.name, e.raw_count FROM edges e
                   JOIN nodes n1 ON n1.id=e.src JOIN nodes n2 ON n2.id=e.dst
                   WHERE n1.layer=2 ORDER BY e.raw_count DESC FETCH FIRST 8 ROWS ONLY""")
    print("\n상위 가중치 경로 (목표 -> 접근법):")
    for a, b, w in cur.fetchall():
        print(f"  [{w}] {a} -> {b}")
    cur.execute("SELECT name, fail_reason FROM nodes WHERE fail_flag='Y'")
    print("\n실패 표식 노드:")
    for name, reason in cur.fetchall():
        print(f"  ⚠ {name} — {reason}")
    con.close()
