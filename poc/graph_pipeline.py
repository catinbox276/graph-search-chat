"""그래프 파이프라인 — sessions -> 게이트 판정 -> 4계층 추출 -> nodes/edges 적재.

design.md §2~§5 구현:
- 세션 게이트 (2갈래):
  · 태스크 세션(selfplay) — LLM이 tasks.yaml의 expect 기준으로 판정
  · 실사용(UI) 세션 — expect가 없으므로 행동 신호를 코드로 세서 판정 (design §3 보강.
    판단은 코드, LLM은 목표/접근법 표현 추출만). 감정·말투 분석 금지.
- 재발 소급 취소: success 세션과 같은 증상이 RECUR_DAYS 안에 다시 오면
  그 성공 판정을 'retracted'로 바꾸고 기여 가중치 회수 (design §4 역방향 supersession)
- 4계층: 도메인(닫힌 목록, 툴 사용으로 결정) -> 목표 -> 접근법 (LLM 추출)
          -> 행동(tool_calls에서 결정적으로 생성)
- dedup: 같은 부모 밑 형제와 임베딩 비교 — >=0.92 & 문자 가드(짧은 이름 제외 +
  difflib ratio) 통과 시 즉시 병합, 아니면 후보들을 LLM 선택 프롬프트로 1회 판정
  (쌍별 이지선다보다 정확 — ComEM COLING'25. 가드 근거: Graphiti/Neo4j는 임베딩
  단독 자동 병합을 안 함)
- 실패 세션: 접근법 노드에 fail_flag + 이유
- 출처: node_evidence(node_id, kind, ref) — kind=session/doc, PK+FK(캐스케이드)로 무결성 강제

usage: .venv/bin/python poc/graph_pipeline.py
"""
import difflib
import json
import re
import statistics
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb
import yaml
from openai import OpenAI

from tools import config

llm = OpenAI(base_url=config.CHAT_URL, api_key=config.MODEL_API_KEY)
# 임베딩 클라이언트는 model_registry.embedding_client()가 해석 (레지스트리 우선)
CHAT_MODEL = config.CHAT_MODEL
SIM_HIGH = config.DEDUP_SIM_HIGH       # 이 이상은 명백히 동일 — LLM 확인 생략하고 병합
SIM_THRESHOLD = config.DEDUP_SIM_THRESHOLD  # 후보 하한 — 이 구간은 LLM이 동일 의도 여부 확인
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

# UI 세션용: 판정은 행동 신호(코드)가 이미 끝냈고, LLM은 적합성 판정 + 지식 표현 추출.
# fits: 문서 파이프라인과 대칭인 도메인 게이트 — 잡담·일반 상식(요리법 등)이
#       도구 매칭만으로 사내 그래프에 유입되는 것을 입구에서 차단.
# grounded: 공로 귀속 — 도구가 기여 없이 모델 일반 지식으로 답한 세션은
#       "검색으로 해결"이라는 거짓 경로를 만들지 않도록 기여 보류.
EXTRACT_PROMPT = """대화가 도메인 범위의 업무 지식인지 판정하고, 맞으면 지식을 추출하라. JSON만 출력.

도메인: {domain}

[첫 질문] {question}
[사용한 도구] {tools}
[최종 답변] {answer}

출력 형식:
{{"fits": true|false,
  "grounded": true|false,
  "goal": "사용자 목표 (10단어 이내, 일반화된 표현, fits=true일 때만)",
  "approach": "해결 접근법 (15단어 이내, 도구+방법. 예: 'DataHub 검색으로 테이블 탐색 후 스키마 조인 키 확인')"}}

fits=false로 판정할 것: 도메인·업무와 무관한 잡담, 일반 상식 질문(요리·생활·시사 등) —
조직 지식으로 축적할 가치가 없는 대화.
grounded=false로 판정할 것: 최종 답변이 도구 결과(검색된 문서·조회된 데이터)에 근거하지 않고
모델의 일반 지식만으로 작성된 경우 (예: 검색이 0건이거나 무관한 결과뿐인데 답변함)."""

# 정정 언어 — 사용자 턴 앞머리의 부정·정정 표현 (design §3: 사용자 턴만 본다)
CORRECTION_RE = re.compile(
    r"^\s*(아니|아뇨|아니요|아닌데)\b|그게 아니라|그거 말고|내가 말한 건|틀렸|잘못 (알|이해|찾)")
# 구체화 신호용 식별자 — 영문 식별자(테이블·컴포넌트명)나 3자리 이상 숫자(로그ID 등)
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}|\d{3,}")
# 명시적 실패 인정 — 에이전트 최종 답변의 "접근이 막혔음" 표지 (design §3 NG 분기).
# 사용자 감정 분석이 아니라 결정적 텍스트 표지 — 조용한 종료의 오탐(포기=성공)을 막는다.
FAIL_ADMIT_RE = re.compile(
    r"찾지 못했|찾을 수 없|존재하지 않|조회(가|할 수) 불가능|접근 권한이 없|"
    r"데이터가 없|해당하는 (글|문서|데이터셋?)[이가] 없")


def ddl(cur):
    for stmt in (
        """CREATE TABLE nodes (
             id VARCHAR2(36) PRIMARY KEY, layer NUMBER(1) NOT NULL,
             name VARCHAR2(400), embedding BLOB,
             fail_flag CHAR(1) DEFAULT 'N', fail_reason VARCHAR2(1000),
             valid_from TIMESTAMP DEFAULT SYSTIMESTAMP, valid_to TIMESTAMP)""",
        """CREATE TABLE edges (
             src VARCHAR2(36) NOT NULL, dst VARCHAR2(36) NOT NULL,
             weight NUMBER DEFAULT 0, raw_count NUMBER DEFAULT 0,
             PRIMARY KEY (src, dst),
             CONSTRAINT edges_src_fk FOREIGN KEY (src)
               REFERENCES nodes(id) ON DELETE CASCADE,
             CONSTRAINT edges_dst_fk FOREIGN KEY (dst)
               REFERENCES nodes(id) ON DELETE CASCADE)""",
        """CREATE TABLE node_evidence (
             node_id VARCHAR2(36) NOT NULL,
             kind VARCHAR2(10) NOT NULL CHECK (kind IN ('session','doc')),
             ref VARCHAR2(400) NOT NULL,
             CONSTRAINT node_evidence_pk PRIMARY KEY (node_id, kind, ref),
             CONSTRAINT node_evidence_node_fk FOREIGN KEY (node_id)
               REFERENCES nodes(id) ON DELETE CASCADE)""",
    ):
        table = stmt.split()[2]
        cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :1",
                    [table.upper()])
        if not cur.fetchone()[0]:
            cur.execute(stmt)
    # FK 캐스케이드 삭제 성능용 (dst는 PK 선두가 아님)
    cur.execute("SELECT COUNT(*) FROM user_indexes WHERE index_name = 'EDGES_DST_IX'")
    if not cur.fetchone()[0]:
        cur.execute("CREATE INDEX edges_dst_ix ON edges (dst)")
    # 구버전 sessions 테이블에 ts가 없으면 추가 (신호 계산·재발 판정에 필요)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SESSIONS' AND column_name = 'TS'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE sessions ADD (ts TIMESTAMP DEFAULT SYSTIMESTAMP)")
    # user_id(SSO 로그인)가 없으면 추가 — 재발 판정을 사용자 단위로 매칭하는 데 쓴다
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SESSIONS' AND column_name = 'USER_ID'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE sessions ADD (user_id VARCHAR2(64))")
    ensure_domain_registry(cur)


# 기본 도메인 시드: (이름, 도구 csv, 우선순위, 추출 지침)
# 추출 지침은 이 도메인으로 분류된 세션의 목표·접근법 추출 프롬프트에 그대로 주입된다.
SEED_DOMAINS = (
    ("데이터 조회", None, 1,  # tools=None → DATAHUB_TOOLS에서 채움
     "목표는 데이터 탐색 의도(무엇을 찾고/조인하고/추적하려 했나)로, "
     "접근법은 도구+방법(테이블 탐색, 스키마 확인, 조인 키, 리니지)으로 일반화하라"),
    ("사내 노하우", "search_docs,read_doc", 2,
     "목표는 해결하려던 문제 증상으로, 접근법은 검색으로 찾은 해법의 핵심 조치로 일반화하라"),
)


def ensure_domain_registry(cur):
    """1층 도메인의 닫힌 목록 저장소 — 없으면 만들고 기본 2종을 시드.

    확장은 사람만 한다(관리자 API /admin/domains 또는 SQL). LLM에게 쓰기 경로 없음.
    design §2 결정 1(위는 닫고 아래는 연다)의 '닫힌 목록'이 코드 하드코딩에서
    이 테이블로 옮겨진 것 — 도메인 추가에 재배포가 필요 없어진다.
    extract_hint = 도메인별 추출 지침. 분류(도구 대조)는 코드가, 표현(목표·접근법을
    어떻게 일반화할지)은 이 지침이 프롬프트에 실려 LLM에 전달된다.
    """
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DOMAIN_REGISTRY'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE domain_registry (
            name         VARCHAR2(100) PRIMARY KEY,
            tools        VARCHAR2(2000),           -- 쉼표구분 도구명: 이 도구를 쓰면 이 도메인
            priority     NUMBER DEFAULT 100,       -- 낮을수록 먼저 대조. 최하순위가 폴백
            extract_hint VARCHAR2(2000),           -- 도메인별 추출 지침 (프롬프트 주입)
            scope        VARCHAR2(10) DEFAULT 'both',  -- 사용 목적: both|chat(대화 전용)|doc(문서 전용)
            created      TIMESTAMP DEFAULT SYSTIMESTAMP)""")
        for name, tools, prio, hint in SEED_DOMAINS:
            cur.execute("INSERT INTO domain_registry (name, tools, priority, extract_hint) "
                        "VALUES (:1, :2, :3, :4)",
                        [name, tools or ",".join(sorted(DATAHUB_TOOLS)), prio, hint])
        return
    # 기존 테이블에 extract_hint가 없으면 추가하고 기본 시드 지침을 백필
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'DOMAIN_REGISTRY' AND column_name = 'EXTRACT_HINT'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE domain_registry ADD (extract_hint VARCHAR2(2000))")
        for name, _tools, _prio, hint in SEED_DOMAINS:
            cur.execute("UPDATE domain_registry SET extract_hint = :1 "
                        "WHERE name = :2 AND extract_hint IS NULL", [hint, name])
    # 사용 목적(scope) 컬럼 — 등록 때 대화/문서/둘 다를 명시 선택 (기존 행은 both)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'DOMAIN_REGISTRY' AND column_name = 'SCOPE'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE domain_registry ADD (scope VARCHAR2(10) DEFAULT 'both')")
        cur.execute("UPDATE domain_registry SET scope = 'both' WHERE scope IS NULL")


def classify_domain(cur, tool_names):
    """닫힌 1층 분류 — LLM이 아니라 도구 사용으로 결정적으로. priority 순 첫 매칭,
    매칭 없으면 최하순위 도메인(범용 폴백). 반환: (도메인명, 추출 지침).

    사용 목적(scope)이 doc(문서 전용)인 도메인은 대화 분류·폴백에서 제외 —
    소스 구조화용 도메인이 최하순위 폴백이 되어 대화를 먹는 사고 방지.
    """
    cur.execute("""SELECT name, tools, extract_hint FROM domain_registry
                   WHERE NVL(scope, 'both') != 'doc' ORDER BY priority, name""")
    rows = [(n, t, h) for n, t, h in cur.fetchall() if (t or "").strip()]
    for name, tools, hint in rows:
        if tool_names & {t.strip() for t in tools.split(",") if t.strip()}:
            return name, (hint or "")
    return (rows[-1][0], rows[-1][2] or "") if rows else ("사내 노하우", "")


def embed(text: str) -> list:
    from tools import model_registry
    cli, emb_name = model_registry.embedding_client()
    return cli.embeddings.create(model=emb_name, input=text).data[0].embedding


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


LAYER_KIND = {2: "목표(사용자가 이루려는 것)", 3: "접근법(문제를 푸는 방법)"}


def llm_same(kind: str, a: str, b: str) -> bool:
    """2단계 판정: 임베딩 후보를 LLM이 최종 확인 (인접 주제 과병합 차단)."""
    kw = ({"extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
           "max_tokens": 80}
          if config.LLM_AUX_NO_THINK else {})  # 이지선다 — 생각 출력 불필요 (config 참조)
    resp = llm.chat.completions.create(
        model=CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
        messages=[{"role": "user", "content":
                   f'두 문구가 같은 {kind}를 가리키면 true. '
                   f'주제·도구가 비슷해도 의도가 다르면 false. JSON만 출력: {{"same": true|false}}\n'
                   f'A: {a}\nB: {b}'}], **kw)
    m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
    try:
        return bool(json.loads(m.group()).get("same")) if m else False
    except json.JSONDecodeError:
        return False


def _auto_merge_ok(a: str, b: str) -> bool:
    """임베딩 ≥HIGH 자동 병합 가드 — 짧은 이름 제외 + 문자 유사도 AND 조건.
    임베딩 코사인 단독 즉시 병합은 업계 관행에 없음 (Graphiti 3-gram Jaccard,
    Neo4j 편집거리 AND). 가드에 걸리면 병합을 버리는 게 아니라 LLM 판정으로 넘어간다."""
    if min(len(a), len(b)) < config.DEDUP_SHORT_NAME_CHARS:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= config.DEDUP_CHAR_RATIO


def llm_select(kind: str, name: str, cands: list) -> str | None:
    """후보 형제 여러 개 중 같은 의도 하나를 LLM이 선택 (없으면 없음).
    쌍별 이지선다 반복보다 정확하고 호출도 1회 (ComEM, COLING 2025).
    cands: [(sim, node_id, name)] 유사도 내림차순. 반환: 병합 대상 node_id 또는 None."""
    cands = cands[:config.DEDUP_SELECT_MAX]
    kw = ({"extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
           "max_tokens": 80}
          if config.LLM_AUX_NO_THINK else {})
    lines = "\n".join(f"{i + 1}. {n}" for i, (_s, _id, n) in enumerate(cands))
    resp = llm.chat.completions.create(
        model=CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
        messages=[{"role": "user", "content":
                   f'기준 문구와 같은 {kind}를 가리키는 후보가 있으면 그 번호를, 없으면 0을 답하라. '
                   f'주제·도구가 비슷해도 의도가 다르면 같은 것이 아니다. JSON만 출력: {{"pick": 번호}}\n'
                   f'기준: {name}\n후보:\n{lines}'}], **kw)
    m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
    try:
        pick = int(json.loads(m.group()).get("pick", 0)) if m else 0
    except (json.JSONDecodeError, TypeError, ValueError):
        pick = 0
    return cands[pick - 1][1] if 1 <= pick <= len(cands) else None


def _llm_json(prompt: str) -> dict:
    """LLM 호출 후 응답에서 JSON 오브젝트 1개를 파싱 (실패 시 빈 dict)."""
    resp = llm.chat.completions.create(
        model=CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
        messages=[{"role": "user", "content": prompt}])
    m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
    try:
        return json.loads(m.group()) if m else {}
    except json.JSONDecodeError:
        return {}


def get_or_create(cur, layer, name, parent_id, ev_kind, ev_ref, use_embedding=True):
    """같은 부모 밑 형제와 2단계(임베딩→LLM) 비교 -> 병합 또는 신규. 엣지 raw_count 증가.

    ev_kind/ev_ref: 출처 증거 — 'session'+세션id 또는 'doc'+'소스명:원천id'."""
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
        if node_id is None and cands:
            cands.sort(reverse=True)
            top_sim, top_id, top_name = cands[0]
            if top_sim >= SIM_HIGH and _auto_merge_ok(name, top_name):
                node_id = top_id  # 고신뢰 + 문자 가드 통과 — LLM 없이 즉시 병합
            else:
                node_id = llm_select(LAYER_KIND.get(layer, "개념"), name, cands)
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
    # 같은 출처가 같은 노드에 두 번 기여해도 안전 (PK 중복 방지)
    cur.execute("""MERGE INTO node_evidence e USING dual
                   ON (e.node_id = :n AND e.kind = :k AND e.ref = :r)
                   WHEN NOT MATCHED THEN INSERT (node_id, kind, ref)
                   VALUES (:n, :k, :r)""",
                {"n": node_id, "k": ev_kind, "r": ev_ref})
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


def _read(v):
    return v.read() if hasattr(v, "read") else (v or "")


def session_turns(cur, sid):
    """세션의 전 턴을 시간순으로 — 신호 계산과 다턴 집계용."""
    cur.execute("""SELECT turn, ts, question, tool_calls, answer FROM sessions
                   WHERE id = :1 ORDER BY turn""", [sid])
    return [{"turn": t, "ts": ts, "q": _read(q),
             "calls": json.loads(_read(c) or "[]"), "a": _read(a)}
            for t, ts, q, c, a in cur.fetchall()]


def split_segments(turns):
    """세션을 태스크 단위 세그먼트로 분할 — 인접 질문 임베딩이 SEG_SPLIT_SIM보다
    멀면 화제가 꺾인 것으로 보고 자른다. 게이트·추출은 세그먼트마다 독립 적용.

    "세션 1개 = 문제 1개" 가정의 보강: 한 세션에서 A를 풀고 B로 넘어가면
    A·B가 따로 판정·추출된다 (첫 질문/마지막 답변 짝짝이 방지 + 자산 회수).
    1턴이거나 경계가 없으면 세그먼트 1개(기존 동작과 동일)."""
    if len(turns) < 2:
        return [turns]
    vecs = [embed(t["q"][:500]) for t in turns]
    segs, cur_seg = [], [turns[0]]
    for prev, nxt, va, vb in zip(turns, turns[1:], vecs, vecs[1:]):
        if cosine(va, vb) < config.SEG_SPLIT_SIM:
            segs.append(cur_seg)
            cur_seg = []
        cur_seg.append(nxt)
    segs.append(cur_seg)
    return segs


def judge_by_signals(turns):
    """실사용(UI) 세션 판정 — 감정·말투가 아니라 행동 신호를 코드로 센다 (design §3 보강).

    후퇴 2개 이상 -> fail / 전진 있고 후퇴 없음 -> success / 나머지 -> unknown(미판정 유지).
    '이탈'(답변 후 무응답 종료)은 배치 시점엔 모든 세션이 그렇게 보여 신호로 쓰지 않는다.
    재발(N일 내 같은 증상 재방문)은 즉시 신호가 아니라 retract_recurrences()의
    소급 취소로 처리한다 — "조용한 종료"를 지금 success로 주고 재발이 나중에 교정.
    """
    qs = [t["q"] for t in turns]
    retreat, forward = [], []
    # 명시적 실패 인정 — 에이전트가 최종 답변에서 접근 불가를 인정하면 즉시 fail
    # (design §3 NG 분기: 데이터/글이 존재하지 않아 목표 달성 불가 + 답변이 인정)
    if turns and FAIL_ADMIT_RE.search(turns[-1]["a"]):
        return "fail", "명시적 실패 인정"
    # 후퇴: 정정 언어 (2턴째부터 — 첫 질문의 "아니"는 정정이 아님)
    if any(CORRECTION_RE.search(q) for q in qs[1:]):
        retreat.append("정정 언어")
    # 후퇴: 문서 재방문 — 같은 글을 다른 턴에서 다시 읽음
    seen = {}
    for t in turns:
        for c in t["calls"]:
            if c.get("name") in ("read_doc", "read_blog_post"):  # 구명 세션 호환
                pid = json.dumps(c.get("args", {}), sort_keys=True, ensure_ascii=False)
                seen.setdefault(pid, set()).add(t["turn"])
    if any(len(v) > 1 for v in seen.values()):
        retreat.append("문서 재방문")
    # 후퇴: 조급함 — 턴 간격이 중앙값 대비 급감
    ts = [t["ts"] for t in turns if t["ts"]]
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    if len(gaps) >= 2 and gaps[-1] < statistics.median(gaps) * config.SIG_HASTY_RATIO:
        retreat.append("조급함")
    # 재질문(후퇴) vs 화제 전진(전진) — 질문 임베딩, 2턴 이상일 때만 계산
    if len(qs) >= 2:
        vecs = [embed(q[:500]) for q in qs]
        adj = [cosine(a, b) for a, b in zip(vecs, vecs[1:])]
        if max(adj) >= config.SIG_REPEAT_SIM:
            retreat.append("재질문")
        elif cosine(vecs[0], vecs[-1]) < config.SIG_TOPIC_MOVE_SIM:
            forward.append("화제 전진")  # 멀어지고 되돌아오지 않음 (재질문 없음이 전제)
        # 전진: 구체화 — 증상 서술에서 컴포넌트명·식별자로 좁혀 들어감
        if len(IDENT_RE.findall(qs[-1])) > len(IDENT_RE.findall(qs[0])):
            forward.append("구체화")
    # 전진: 조용한 종료 — 정정 없이, 도구 근거가 있는 답으로 끝남
    if not retreat and any(t["calls"] for t in turns) \
            and not turns[-1]["a"].startswith("[에이전트 오류]"):
        forward.append("조용한 종료")
    if len(retreat) >= 2:
        return "fail", ", ".join(retreat)
    if forward and not retreat:
        return "success", ", ".join(forward)
    return "unknown", ", ".join(retreat + forward)


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


if __name__ == "__main__":
    import time as _t
    from tools import events as _ev
    _t0 = _t.time()
    try:
        main()
        _ev.log("batch", source="graph-pipeline", level="info", status="ok",
                duration_ms=int((_t.time() - _t0) * 1000), summary="graph-pipeline 완료")
    except Exception as _e:
        import traceback as _tb
        _ev.log("batch", source="graph-pipeline", level="error", status="fail",
                duration_ms=int((_t.time() - _t0) * 1000),
                summary=f"{type(_e).__name__}: {str(_e)[:200]}",
                detail=_tb.format_exc())
        raise
