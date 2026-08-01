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

llm = OpenAI(base_url="http://127.0.0.1:1234/v1", api_key="lm-studio")
CHAT_MODEL = "qwen/qwen3.6-35b-a3b"
EMB_MODEL = "text-embedding-qwen3-embedding-0.6b"
SIM_THRESHOLD = 0.85
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
- success: 답변이 판정 기준을 충족
- fail: 근거를 못 찾았고 답변이 그 사실을 인정함 (기준이 '실패 인정'이면 인정했을 때 fail)
- unknown: 판단 불가하거나 근거 없이 지어낸 답변"""


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


def get_or_create(cur, layer, name, parent_id, sid, use_embedding=True):
    """같은 부모 밑 형제와 임베딩 비교 -> 병합 또는 신규. 엣지 raw_count 증가."""
    vec = embed(name) if use_embedding else None
    node_id = None
    if parent_id:
        cur.execute("""SELECT n.id, n.name, n.embedding FROM nodes n
                       JOIN edges e ON e.dst = n.id
                       WHERE e.src = :1 AND n.layer = :2""", [parent_id, layer])
        for nid, nname, nemb in cur.fetchall():
            if nname == name:
                node_id = nid; break
            if vec is not None and nemb:
                if cosine(vec, json.loads(nemb.read())) >= SIM_THRESHOLD:
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
                   WHERE turn = 1 AND verdict IS NULL
                   AND REGEXP_LIKE(id, '^[RSF][0-9]+-[0-9]+$') ORDER BY id""")
    rows = [(r[0], r[1].read(), r[2].read(), r[3].read()) for r in cur.fetchall()]
    print(f"판정 대상 {len(rows)}세션")
    for n, (sid, q, calls_json, answer) in enumerate(rows, 1):
        calls = json.loads(calls_json or "[]")
        task_id = sid.split("-")[0]
        prompt = JUDGE_PROMPT.format(
            question=q, tools=json.dumps(calls, ensure_ascii=False)[:2000],
            answer=answer[:3000], expect=exp[task_id])
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
        con.commit()
        print(f"[{n}/{len(rows)}] {sid} -> {verdict}", flush=True)

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
