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
from .llm import _llm_json, fill_prompt
from .merge import apply_extras, default_merge_cfg, get_or_create
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


def _active_snapshots(cur):
    """활성 엔티티·클러스터 라인 스냅샷 — (schema, criteria, mc, judged_with) 반환.
    세션 추출도 스키마 라인(문서와 공유)으로 조립 (경량 스키마화 — 라인 없으면 코드 기본).
    doc_pipeline.judge는 lazy import — judge가 graph_pipeline을 import해 상단 import는 순환."""
    from core import versioning
    from graph.doc_pipeline import judge as _j

    def _lob(v):
        return v.read() if hasattr(v, "read") else (v or "")

    def _jl(v):
        try:
            out = json.loads(_lob(v) or "[]")
            return out if isinstance(out, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    schema, crit, mc, en, ev, cn, cv = _j.DEFAULT_SCHEMA, "", None, None, None, None, None
    try:
        en, ev = versioning.active(cur, "entity_versions")
        if en and ev is not None:
            cur.execute("""SELECT criteria, etypes FROM entity_versions
                           WHERE name = :1 AND version = :2""", [en, ev])
            r = cur.fetchone()
            if r:
                crit = _lob(r[0])
                schema = _j.norm_schema(_jl(r[1]))
        cn, cv = versioning.active(cur, "cluster_versions")
        if cn and cv is not None:
            cur.execute("""SELECT sim_high, sim_threshold, short_name_chars, char_ratio,
                                  select_max, select_prompt
                           FROM cluster_versions WHERE name = :1 AND version = :2""", [cn, cv])
            r = cur.fetchone()
            if r:
                mc = {"sim_high": float(r[0]), "sim_threshold": float(r[1]),
                      "short_name_chars": int(r[2]), "char_ratio": float(r[3]),
                      "select_max": int(r[4]), "select_prompt": _lob(r[5]) or ""}
    except Exception as e:   # 버전 테이블 미생성 등 — 코드 기본으로 동작 (기존과 동일)
        print(f"[경고] 활성 라인 조회 실패 — 코드 기본 스키마 사용: {e}", flush=True)
    chain, _spos = _j.chain_view(schema)
    mc = {**default_merge_cfg(), **(mc or {}), "embed_model": "",
          "layer_kind": {2 + i: (c.get("label") or c["key"]) for i, c in enumerate(chain)}}
    judged_with = (f"엔티티 {en}·v{ev}" if en and ev is not None else "엔티티 코드기본") + \
                  (f" · 클러스터 {cn}·v{cv}" if cn and cv is not None else "")
    return schema, crit, mc, judged_with


def main():
    exp = expects()
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    ddl(cur)
    # 프롬프트 결정 — 관리 원문 override(app_settings) > 활성 라인 스키마 조립 > 코드 기본.
    from core import settings
    from graph.doc_pipeline import judge as _j   # lazy — 순환 import 회피
    schema, crit, mc, judged_with = _active_snapshots(cur)
    chain, spos = _j.chain_view(schema)
    _st = settings.get_all()
    judge_tmpl = (_st.get("entity_judge_prompt") or "").strip() \
        or _j.build_session_judge_prompt(schema, crit)
    extract_tmpl = (_st.get("entity_extract_prompt") or "").strip() \
        or _j.build_session_extract_prompt(schema, crit)
    print(f"세션 추출 구성: {judged_with} · 체인 {'→'.join(c['key'] for c in chain)}"
          f"{' · 속성 ' + str(len(schema.get('attrs') or [])) + '종' if schema.get('attrs') else ''}",
          flush=True)
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
            prompt = fill_prompt(judge_tmpl, domain=domain,
                hint=(hint or "").strip() or "(지침 없음 — 도메인명 기준으로 판정)",
                question=q, tools=json.dumps(calls, ensure_ascii=False)[:2000],
                answer=answer[:3000], expect=exp[task_id])
            if hint and "{hint}" not in judge_tmpl:  # 구식 원문 override 호환 — 접미 주입
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
                prompt = fill_prompt(extract_tmpl,
                    domain=domain,
                    hint=(hint or "").strip() or "(지침 없음 — 도메인명 기준으로 판정)",
                    question=seg[0]["q"][:2000],
                    tools=json.dumps(calls, ensure_ascii=False)[:2000],
                    answer=seg[-1]["a"][:3000])
                if hint and "{hint}" not in extract_tmpl:  # 구식 원문 override 호환
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
        cur.execute("UPDATE sessions SET verdict = :1, judged_with = :2 "
                    "WHERE id = :3 AND turn = 1",
                    [verdict, judged_with[:200], sid])
        for domain, j, v, tool_names in contribs:
            if not all(j.get(c["key"]) for c in chain):   # 체인 전 키 필수 (커스텀 키 동작)
                continue
            # 계층 체인 사다리 — 도메인(1) 밑으로 체인 칸을 순서대로 (layer 2+i).
            # 역할 태그: 첫 칸=entry(검색진입), spos 칸=solution(검증귀속·실패표식·도구 부모)
            d = get_or_create(cur, 1, domain, None, "session", sid,
                              use_embedding=False, mc=mc)
            parent, val2node, entry_node, sol = d, {}, None, None
            for i, c in enumerate(chain):
                cv_ = str(j[c["key"]]).strip()[:400]
                rt = "entry" if i == 0 else ("solution" if i == spos else None)
                parent = get_or_create(cur, 2 + i, cv_, parent, "session", sid,
                                       mc=mc, role_tag=rt)
                val2node[(c["key"], cv_)] = parent
                if i == 0:
                    entry_node = parent
                if i == spos:
                    sol = parent
            if v == "fail":
                cur.execute("""UPDATE nodes SET fail_flag='Y', fail_reason=:1
                               WHERE id=:2""", [(j.get("fail_reason") or "")[:1000], sol])
            for tool in sorted(tool_names):
                get_or_create(cur, 8, f"tool:{tool}", sol, "session", sid,
                              use_embedding=False, mc=mc)
            # 속성(9층) — 문서 파이프라인과 대칭 (apply_extras 공용, ref=세션id)
            ej = {t["key"]: j.get(t["key"]) for t in schema["attrs"]}
            apply_extras(cur, schema, ej, None, entry_node, val2node,
                         "session", sid)
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
