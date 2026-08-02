"""그래프 파이프라인 — sessions -> 게이트 판정 -> 4계층 추출 -> nodes/edges 적재.

design.md §2~§5 구현:
- 세션 게이트: LLM이 tasks.yaml의 expect 기준으로 success/fail/unknown 판정
- 4계층: 도메인(닫힌 목록, 툴 사용으로 결정) -> 목표 -> 접근법 (LLM 추출)
          -> 행동(tool_calls에서 결정적으로 생성)
- dedup: 같은 부모 밑 형제 노드와 임베딩 코사인 >= 0.85면 병합 (브루트포스)
- 실패 세션: 접근법 노드에 fail_flag + 이유
- 출처: node_evidence(node_id, session_id)

usage: .venv/bin/python poc/graph_pipeline.py
"""
import json
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb
import yaml
from openai import OpenAI

from tools.blog_search import DSN, PASSWORD, USER

llm = OpenAI(base_url=os.environ.get("MODEL_URL", "http://127.0.0.1:1234/v1"), api_key="lm-studio")
CHAT_MODEL = "qwen/qwen3.6-35b-a3b"
EMB_MODEL = "text-embedding-qwen3-embedding-0.6b"
SIM_HIGH = 0.92       # 이 이상은 명백히 동일 — LLM 확인 생략하고 병합
SIM_THRESHOLD = 0.70  # 후보 하한 — 이 구간(0.70~0.92)은 LLM이 동일 의도 여부 확인
# 캘리브레이션: 같은 의도 0.81~0.98, 다른 의도 0.34~0.46. 인접 주제 과병합(도커 사례)이 0.7대에서 발생
DATAHUB_TOOLS = {"search", "get_entities", "list_schema_fields", "get_lineage",
                 "get_lineage_paths_between", "get_dataset_queries"}

JUDGE_PROMPT = """세션을 판정하고 지식을 추출하라. JSON만 출력.

[질문] {question}
[사용한 도구] {tools}
[답변] {answer}
[판정 기준] {expect}

출력 형식:
{{"verdict": "success|fail|unknown",
  "goal": "사용자 목표 (10단어 이내, 일반화된 표현)",
  "approach": "해결 접근법 (15단어 이내, 도구+방법. 예: 'DataHub 검색으로 테이블 탐색 후 스키마 조인 키 확인')",
  "fail_reason": "실패 시 이유 한 줄, 성공이면 null"}}

판정 규칙:
- success: 답변이 판정 기준의 핵심(문제 해결)을 달성함. 인용 형식이 미흡해도 해결책이 맞으면 success
- fail: 접근 자체가 막힌 경우만 — 데이터/글이 존재하지 않아 목표 달성이 불가능했고 답변이 이를 인정함
  (기준이 '실패 인정'이면 인정했을 때 fail)
- unknown: 판단 불가, 근거 없이 지어냄, 또는 답변 품질이 미달이지만 접근이 막힌 건 아닌 경우"""


def ddl(cur):
    for stmt in (
        """CREATE TABLE nodes (
             id VARCHAR2(36) PRIMARY KEY, layer NUMBER(1) NOT NULL,
             name VARCHAR2(400), embedding BLOB,
             fail_flag CHAR(1) DEFAULT 'N', fail_reason VARCHAR2(1000),
             valid_from TIMESTAMP DEFAULT SYSTIMESTAMP, valid_to TIMESTAMP)""",
        """CREATE TABLE edges (
             src VARCHAR2(36), dst VARCHAR2(36),
             weight NUMBER DEFAULT 0, raw_count NUMBER DEFAULT 0,
             PRIMARY KEY (src, dst))""",
        """CREATE TABLE node_evidence (
             node_id VARCHAR2(36), session_id VARCHAR2(36))""",
    ):
        table = stmt.split()[2]
        cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :1",
                    [table.upper()])
        if not cur.fetchone()[0]:
            cur.execute(stmt)


def embed(text: str) -> list:
    return llm.embeddings.create(model=EMB_MODEL, input=text).data[0].embedding


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


LAYER_KIND = {2: "목표(사용자가 이루려는 것)", 3: "접근법(문제를 푸는 방법)"}


def llm_same(kind: str, a: str, b: str) -> bool:
    """2단계 판정: 임베딩 후보를 LLM이 최종 확인 (인접 주제 과병합 차단)."""
    resp = llm.chat.completions.create(
        model=CHAT_MODEL, temperature=0,
        messages=[{"role": "user", "content":
                   f'두 문구가 같은 {kind}를 가리키면 true. '
                   f'주제·도구가 비슷해도 의도가 다르면 false. JSON만 출력: {{"same": true|false}}\n'
                   f'A: {a}\nB: {b}'}])
    m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
    try:
        return bool(json.loads(m.group()).get("same")) if m else False
    except json.JSONDecodeError:
        return False


def get_or_create(cur, layer, name, parent_id, sid, use_embedding=True):
    """같은 부모 밑 형제와 2단계(임베딩→LLM) 비교 -> 병합 또는 신규. 엣지 raw_count 증가."""
    vec = embed(name) if use_embedding else None
    node_id = None
    if parent_id:
        cur.execute("""SELECT n.id, n.name, n.embedding FROM nodes n
                       JOIN edges e ON e.dst = n.id
                       WHERE e.src = :1 AND n.layer = :2""", [parent_id, layer])
        cands = []
        for nid, nname, nemb in cur.fetchall():
            if nname == name:
                node_id = nid; break
            if vec is not None and nemb:
                sim = cosine(vec, json.loads(nemb.read()))
                if sim >= SIM_THRESHOLD:
                    cands.append((sim, nid, nname))
        if node_id is None:
            for sim, nid, nname in sorted(cands, reverse=True):
                if sim >= SIM_HIGH or llm_same(LAYER_KIND.get(layer, "개념"), name, nname):
                    node_id = nid; break
    else:
        cur.execute("SELECT id FROM nodes WHERE layer = :1 AND name = :2",
                    [layer, name])
        r = cur.fetchone()
        node_id = r[0] if r else None
    if node_id is None:
        node_id = uuid.uuid4().hex[:32]
        cur.execute(
            "INSERT INTO nodes (id, layer, name, embedding) VALUES (:1,:2,:3,:4)",
            [node_id, layer, name,
             json.dumps(vec).encode() if vec is not None else None])
    if parent_id:
        cur.execute("""MERGE INTO edges e USING dual ON (e.src=:src AND e.dst=:dst)
                       WHEN MATCHED THEN UPDATE SET raw_count = raw_count+1, weight = weight+1
                       WHEN NOT MATCHED THEN INSERT (src, dst, weight, raw_count)
                       VALUES (:src, :dst, 1, 1)""",
                    {"src": parent_id, "dst": node_id})
        # ponytail: weight=raw_count. 노출 대비 채택률 보정은 제안 기능이 생긴 뒤에
    cur.execute("INSERT INTO node_evidence VALUES (:1, :2)", [node_id, sid])
    return node_id


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


def expects():
    tasks = yaml.safe_load(open(ROOT / "poc" / "tasks.yaml"))
    out = {}
    for group in ("repeat", "single", "fail"):
        for t in tasks[group]:
            out[t["id"]] = t.get("expect") or f"실패 인정 기대: {t['expect_fail']}"
    return out


def main():
    exp = expects()
    con = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = con.cursor()
    ddl(cur)
    cur.execute("""SELECT id, question, tool_calls, answer FROM sessions
                   WHERE turn = 1 AND verdict IS NULL ORDER BY id""")
    rows = [(r[0], r[1].read(), r[2].read(), r[3].read()) for r in cur.fetchall()]
    print(f"판정 대상 {len(rows)}세션")
    for n, (sid, q, calls_json, answer) in enumerate(rows, 1):
        calls = json.loads(calls_json or "[]")
        task_id = sid.split("-")[0]
        expect = exp.get(task_id,  # 실사용(UI) 세션은 일반 기준으로 판정
                         "사용자의 질문이 근거(데이터/문서)와 함께 실질적으로 해결되었는가")
        prompt = JUDGE_PROMPT.format(
            question=q, tools=json.dumps(calls, ensure_ascii=False)[:2000],
            answer=answer[:3000], expect=expect)
        resp = llm.chat.completions.create(
            model=CHAT_MODEL, temperature=0,
            messages=[{"role": "user", "content": prompt}])
        text = resp.choices[0].message.content
        m = re.search(r"\{.*\}", text, re.S)
        try:
            j = json.loads(m.group()) if m else {}
        except json.JSONDecodeError:
            j = {}
        verdict = j.get("verdict", "unknown")
        if verdict not in ("success", "fail"):
            verdict = "unknown"
        cur.execute("UPDATE sessions SET verdict = :1 WHERE id = :2 AND turn = 1",
                    [verdict, sid])
        if verdict != "unknown" and j.get("goal") and j.get("approach"):
            tool_names = {c["name"] for c in calls}
            domain = ("데이터 조회" if tool_names & DATAHUB_TOOLS
                      else "사내 노하우")  # 닫힌 1층: 도구 사용으로 결정적 분류
            d = get_or_create(cur, 1, domain, None, sid, use_embedding=False)
            g = get_or_create(cur, 2, j["goal"], d, sid)
            a = get_or_create(cur, 3, j["approach"], g, sid)
            if verdict == "fail":
                cur.execute("""UPDATE nodes SET fail_flag='Y', fail_reason=:1
                               WHERE id=:2""", [(j.get("fail_reason") or "")[:1000], a])
            for tool in sorted(tool_names):
                get_or_create(cur, 4, f"tool:{tool}", a, sid, use_embedding=False)
        # 채택 판정: 이 세션에 노출된 제안 노드를 실제로 사용했는가 (유도 vs 자발 구분의 기초)
        cur.execute("""UPDATE suggestions s SET adopted =
            CASE WHEN EXISTS (SELECT 1 FROM node_evidence ev
                              WHERE ev.session_id = :sid AND ev.node_id = s.node_id)
                 THEN 'Y' ELSE 'N' END
            WHERE s.session_id = :sid AND s.adopted IS NULL""", {"sid": sid})
        con.commit()
        print(f"[{n}/{len(rows)}] {sid} -> {verdict}", flush=True)

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


if __name__ == "__main__":
    main()
