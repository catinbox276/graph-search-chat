"""문서 LLM 판정 — DB를 만지지 않는 순수 판정 계층 (스레드 병렬 안전).

- judge_doc: 문서 1건 판정 (서버 드라이런도 사용)
- judge_pack: 문서 묶음을 요청 1건으로 판정 — 누락분은 단건 폴백 (유실 없음)
"""
import json
import re

from core import config
from graph.graph_pipeline import CHAT_MODEL, llm

# ── 추출 스키마 — 관리자가 계층 체인·태그·속성·관계를 정의, 프롬프트는 여기서 생성 ──
# v2 계층 체인: chain 행(배열 순서=체인 순서)이 그래프 2층부터 순서대로 저장층이 된다.
# 역할 태그: entry(검색진입)=체인 첫 칸 고정, solution(검증귀속)=tags로 지정(기본 마지막 칸).
# attr(속성)→그래프 9층, 도구→8층. v1(role entry/solution 각 1행)은 체인 2칸으로 정규화.
_DEF_ENTRY = {"key": "goal", "label": "목표", "desc": "문서가 다루는 문제/목표"}
_DEF_SOLUTION = {"key": "approach", "label": "접근법", "desc": "핵심 해법/접근법"}


def norm_schema(etypes, rtypes=None) -> dict:
    """etypes 행 + rtypes 행 → {chain, entry, solution, solution_pos, attrs, relations}.
    entry/solution 접근자는 항상 채워짐(체인의 태그 칸 참조) — 2슬롯 리더 하위호환.
    관계의 source/target은 스키마 타입 키(체인 전 키+속성 키)에 실존해야 하며 아니면 버린다."""
    chain, attrs, legacy_e, legacy_s = [], [], None, None
    for t in (etypes or []):
        if not (isinstance(t, dict) and str(t.get("key", "")).strip()):
            continue
        row = {"key": str(t["key"]).strip(),
               "label": str(t.get("label", "")).strip() or str(t["key"]).strip(),
               "desc": str(t.get("desc", "")).strip(),
               "tags": [s for s in (t.get("tags") or []) if s in ("entry", "solution")]}
        role = str(t.get("role", "")).strip()
        if role == "chain":
            chain.append(row)
        elif role == "entry":       # v1 하위호환 — 체인 선두
            legacy_e = row
        elif role == "solution":    # v1 하위호환 — 체인 말미 + 검증귀속 태그
            row["tags"] = ["solution"]
            legacy_s = row
        else:
            attrs.append({k: row[k] for k in ("key", "label", "desc")})
    if legacy_e or legacy_s:   # v1 행 발견 — 기본값 보충 포함해 체인 2칸+로 조립
        chain = [legacy_e or dict(_DEF_ENTRY, tags=[])] + chain \
            + [legacy_s or dict(_DEF_SOLUTION, tags=["solution"])]
    if not chain:
        chain = [dict(_DEF_ENTRY, tags=[]), dict(_DEF_SOLUTION, tags=["solution"])]
    elif len(chain) == 1:      # 체인은 최소 2칸 (진입과 귀속이 같으면 추천이 무의미)
        chain.append(dict(_DEF_SOLUTION, tags=["solution"]))
    # 불변식: 첫 칸 = entry(검색진입, 그래프 2층), solution 태그는 정확히 1개(없으면 마지막)
    spos = next((i for i, c in enumerate(chain) if "solution" in c["tags"]), len(chain) - 1)
    if spos == 0:
        spos = len(chain) - 1
    for i, c in enumerate(chain):
        c["tags"] = (["entry"] if i == 0 else []) + (["solution"] if i == spos else [])
    keys = {c["key"] for c in chain} | {a["key"] for a in attrs}
    relations = []
    for r in (rtypes or []):
        if not (isinstance(r, dict) and str(r.get("key", "")).strip()):
            continue
        src, tgt = str(r.get("source", "")).strip(), str(r.get("target", "")).strip()
        if src not in keys or tgt not in keys:
            continue   # 시그니처가 스키마 밖 — 무효 행 폐기
        relations.append({"key": str(r["key"]).strip(),
                          "label": str(r.get("label", "")).strip() or str(r["key"]).strip(),
                          "desc": str(r.get("desc", "")).strip(),
                          "source": src, "target": tgt})
    return {"chain": chain, "entry": chain[0], "solution": chain[spos],
            "solution_pos": spos, "attrs": attrs, "relations": relations}


DEFAULT_SCHEMA = norm_schema([])   # 리터럴과 정규화 결과가 절대 어긋나지 않게


def chain_view(sc: dict) -> tuple:
    """(체인 행 목록, solution 인덱스) — v1 스냅샷({entry,solution,...}, chain 없음)도 수용.
    모든 리더(병합 사다리·드라이런·표시)의 단일 하위호환 관용구."""
    chain = sc.get("chain") or [sc["entry"], sc["solution"]]
    spos = sc.get("solution_pos")
    if spos is None:
        spos = next((i for i, c in enumerate(chain)
                     if "solution" in (c.get("tags") or [])), len(chain) - 1)
    return chain, spos


def _out_fields(schema: dict, brief: bool = False, src: str = "문서") -> str:
    """출력 형식 JSON의 스키마 필드 부분 — desc가 추출 기준으로 들어간다.
    체인 칸은 순서대로 전부 필수 필드 (chain=2면 v1과 바이트 동일).
    src: 추출 원천 표기 ("문서" 또는 "대화" — 세션 스캐폴드 공용)."""
    chain, _ = chain_view(schema)
    suffix = "" if brief else " (한 문장, fits=true일 때만)"
    lines = [f' "{c["key"]}": "{c["desc"] or c["label"]}{suffix}"' for c in chain]
    for a in schema["attrs"]:
        lines.append(f' "{a["key"]}": "{a["desc"] or a["label"]}'
                     f' ({src}에서 확인될 때만 — 없으면 이 키를 생략)"')
    if schema.get("relations"):
        lines.append(' "relations": [{"type": "<관계 타입 키>", '
                     '"source": "<출발 엔티티 값>", "target": "<도착 엔티티 값>"}]')
    return ",\n".join(lines)


def _rel_block(schema: dict, src: str = "문서") -> str:
    """관계 타입 카탈로그 + 추출 규칙 — Graphiti extract_edges의 FACT_TYPES/RULES 포팅.
    관계가 정의된 스키마에서만 프롬프트에 붙는다. src: "문서" 또는 "대화"."""
    rels = schema.get("relations") or []
    if not rels:
        return ""
    tl = {t["key"]: (t.get("label") or t["key"])
          for t in [*chain_view(schema)[0], *schema["attrs"]]}
    lines = "\n".join(
        f'- {r["key"]} ({r["label"]}): {r["desc"] or "(설명 없음)"}'
        f' [시그니처: {tl.get(r["source"], r["source"])} → {tl.get(r["target"], r["target"])}]'
        for r in rels)
    return f"""

관계 추출 규칙 — 추출했다면 아래 관계 타입에 해당하는 사실만 "relations" 배열로 추출하라:
{lines}
- source/target에는 반드시 위 필드들로 이 {src}에서 추출한 엔티티 값을 그대로 쓴다
  (목록에 없는 이름을 쓰면 그 관계는 폐기된다). source와 target은 서로 달라야 한다.
- {src}에 명시되었거나 명백히 함의된 관계만 — 추측 금지. 해당 없으면 빈 배열 []."""


def _crit_block(criteria: str) -> str:
    criteria = (criteria or "").strip()
    return f"\n엔티티 판정·추출 지침:\n{criteria}" if criteria else ""


def build_doc_prompt(schema: dict | None = None, criteria: str = "") -> str:
    """단건 판정 프롬프트를 스키마에서 생성 — 출력 형식·placeholder 배선은 코드 잠금."""
    sc = schema or DEFAULT_SCHEMA
    labels = "도 ".join(c["label"] for c in chain_view(sc)[0])
    return f"""문서가 도메인 기준에 맞는지 판정하고, 맞으면 지식을 추출하라. JSON만 출력.

도메인: {{domain}}
도메인 기준·추출 지침: {{hint}}{_crit_block(criteria)}

문서 (유형: {{kind}}):
제목: {{title}}
{{body}}

출력 형식: {{"fits": true|false, "reason": "판정 근거 한 문장",
{_out_fields(sc)}}}

fits=false로 판정할 것:
- 도메인과 무관한 내용
- {labels}도 찾을 수 없는 글, 결말·결론 없이 끝나는 글
- 내용이 너무 빈약해 지식으로 일반화할 수 없는 글{_rel_block(sc)}"""


def build_pack_prompt(schema: dict | None = None, criteria: str = "") -> str:
    """묶음 판정 프롬프트 — 단건과 같은 스키마, 배열 포장만 다름."""
    sc = schema or DEFAULT_SCHEMA
    labels = "도 ".join(c["label"] for c in chain_view(sc)[0])
    return f"""여러 문서 각각이 도메인 기준에 맞는지 판정하고, 맞으면 지식을 추출하라. JSON 배열만 출력.

도메인: {{domain}}
도메인 기준·추출 지침: {{hint}}{_crit_block(criteria)}

문서 목록 — 각 문서는 ===[문서id]=== 로 시작한다:
{{docs}}

출력 형식: 문서마다 1개씩, 입력 순서대로 JSON 배열.
[{{"id": "문서id", "fits": true|false, "reason": "판정 근거 한 문장",
{_out_fields(sc, brief=True)}}}, ...]

fits=false로 판정할 것: 도메인과 무관 / {labels}도 없음 / 결말·결론 없음 / 내용이 빈약함.
각 문서는 독립적으로 판정하라 — 다른 문서의 내용이 판정에 영향을 주면 안 된다.{_rel_block(sc)}"""


def build_prompts(schema: dict | None = None, criteria: str = "") -> tuple:
    return build_doc_prompt(schema, criteria), build_pack_prompt(schema, criteria)


# ── 세션(대화) 스캐폴드 — 같은 스키마 라인이 문서+대화 양쪽에 적용된다 ──
# 자리표시자 치환은 graph_pipeline.llm.fill_prompt ({domain}{hint}{question}{tools}{answer}{expect}).
# graph_pipeline.run이 lazy import로 사용 (모듈 상단 import는 순환 — judge가 graph_pipeline을 import).

def _brevity_line(sc: dict) -> str:
    """값 간결성 가이드 — 체인 첫 칸(검색 진입 매칭 대상)은 10단어, 나머지 단계는 15단어.
    chain=2면 v1 문구와 바이트 동일."""
    chain, _ = chain_view(sc)
    rest = ", ".join(c["label"] for c in chain[1:])
    return (f"값은 간결하게 — {chain[0]['label']}은(는) 10단어 이내(일반화된 표현), "
            f"{rest}은(는) 15단어 이내(도구+방법")


def build_session_extract_prompt(schema: dict | None = None, criteria: str = "") -> str:
    """UI 세션 추출 프롬프트 — fits/grounded 게이트 문구는 원문 유지, 추출 키만 스키마에서.
    fits: 도메인 게이트(잡담·일반 상식 차단), grounded: 공로 귀속(도구 기여 없는 답변 보류)."""
    sc = schema or DEFAULT_SCHEMA
    return f"""대화가 도메인 범위의 업무 지식인지 판정하고, 맞으면 지식을 추출하라. JSON만 출력.

도메인: {{domain}}
도메인 추출 지침: {{hint}}{_crit_block(criteria)}

[첫 질문] {{question}}
[사용한 도구] {{tools}}
[최종 답변] {{answer}}

출력 형식:
{{"fits": true|false,
  "grounded": true|false,
{_out_fields(sc, src="대화")}}}

{_brevity_line(sc)}.
예: 'DataHub 검색으로 테이블 탐색 후 스키마 조인 키 확인').

fits=false로 판정할 것: 도메인·업무와 무관한 잡담, 일반 상식 질문(요리·생활·시사 등) —
조직 지식으로 축적할 가치가 없는 대화.
grounded=false로 판정할 것: 최종 답변이 도구 결과(검색된 문서·조회된 데이터)에 근거하지 않고
모델의 일반 지식만으로 작성된 경우 (예: 검색이 0건이거나 무관한 결과뿐인데 답변함).{_rel_block(sc, src="대화")}"""


def build_session_judge_prompt(schema: dict | None = None, criteria: str = "") -> str:
    """태스크 세션(selfplay) 판정+추출 프롬프트 — verdict 규칙 원문 유지, 추출 키만 스키마에서."""
    sc = schema or DEFAULT_SCHEMA
    return f"""세션을 판정하고 지식을 추출하라. JSON만 출력.

도메인: {{domain}}
도메인 추출 지침: {{hint}}{_crit_block(criteria)}

[질문] {{question}}
[사용한 도구] {{tools}}
[답변] {{answer}}
[판정 기준] {{expect}}

출력 형식:
{{"verdict": "success|fail|unknown",
{_out_fields(sc, brief=True, src="대화")},
  "fail_reason": "실패 시 이유 한 줄, 성공이면 null"}}

{_brevity_line(sc)}).

판정 규칙:
- success: 답변이 판정 기준의 핵심(문제 해결)을 달성함. 인용 형식이 미흡해도 해결책이 맞으면 success
- fail: 접근 자체가 막힌 경우만 — 데이터/글이 존재하지 않아 목표 달성이 불가능했고 답변이 이를 인정함
  (기준이 '실패 인정'이면 인정했을 때 fail)
- unknown: 판단 불가, 근거 없이 지어냄, 또는 답변 품질이 미달이지만 접근이 막힌 건 아닌 경우{_rel_block(sc, src="대화")}"""


def build_session_prompts(schema: dict | None = None, criteria: str = "") -> tuple:
    """(추출, 판정) 세션 프롬프트 쌍 — 관리 미리보기·run 오케스트레이션 공용."""
    return (build_session_extract_prompt(schema, criteria),
            build_session_judge_prompt(schema, criteria))


# 기본 스키마의 렌더 결과 — 앱설정 override 미지정·세션 무관 경로의 코드 기본값
DOC_PROMPT = build_doc_prompt()
PACK_PROMPT = build_pack_prompt()

PACK_MAX_DOCS = 8  # 묶음당 문서 상한 — 출력 길이·판정 품질 보호


def _gen_kwargs(no_think: bool, max_tokens: int) -> dict:
    """생성 옵션 — no_think면 추론(생각) 출력을 끄고 출력 상한을 건다.

    출력(디코드)이 배치의 병목이라 생각 토큰 제거가 최대 지렛대 (A/B 실측 7~8배,
    판정 품질 동일). Qwen3 계열 chat_template_kwargs — 다른 서빙이면 무시될 수 있음.
    """
    if not no_think:
        return {}
    return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            "max_tokens": max_tokens}


def _clip(s: str, n: int) -> str:
    """본문 자르기 — n<=0이면 전체(제한 없음)."""
    s = s or ""
    return s if n <= 0 else s[:n]


_PLACEHOLDERS = ("domain", "hint", "kind", "title", "body", "docs")


def _fill(tmpl: str, **kw) -> str:
    """지정 플레이스홀더({domain}·{hint}·{kind}·{title}·{body}·{docs})만 치환한다.
    그 외 중괄호(JSON 예시 등)는 그대로 둬 관리자가 편집한 프롬프트도 안전
    (.format의 중괄호 이스케이프 footgun 없음)."""
    out = tmpl
    for k in _PLACEHOLDERS:
        if k in kw:
            out = out.replace("{" + k + "}", str(kw[k]))
    return out


def judge_pack(domain: str, hint: str, pack: list, model: str = "",
               body_chars: int = 3000, no_think: bool = True,
               doc_prompt: str = "", pack_prompt: str = "") -> list:
    """문서 묶음을 요청 1건으로 판정 — [(doc, verdict)] 반환.

    묶음 응답에서 누락·파싱 실패한 문서는 단건 판정으로 자동 폴백 (유실 없음).
    pack 원소: (src_id, title, kind, body). DB를 만지지 않아 스레드 병렬 안전.
    doc_prompt/pack_prompt: 관리 override(빈값=코드 기본) — 호출자가 1회 로드해 전달.
    """
    if len(pack) == 1:
        d = pack[0]
        return [(d, judge_doc(domain, hint, d[2], d[1], d[3],
                              model=model, body_chars=body_chars,
                              no_think=no_think, doc_prompt=doc_prompt))]
    blocks = [f"===[{d[0]}]===\n제목: {(d[1] or '').strip()[:300]}\n{_clip(d[3], body_chars)}"
              for d in pack]
    prompt = _fill((pack_prompt or "").strip() or PACK_PROMPT,
        domain=domain, hint=(hint or "").strip() or "(지침 없음 — 도메인명 기준으로 판정)",
        docs="\n\n".join(blocks))
    by_id, pack_usage = {}, (0, 0)
    try:
        resp = llm.chat.completions.create(
            model=model or CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
            **_gen_kwargs(no_think, 1600))
        pack_usage = _usage_of(resp)   # 묶음 1요청 전체 토큰
        m = re.search(r"\[.*\]", resp.choices[0].message.content, re.S)
        for item in (json.loads(m.group()) if m else []):
            if isinstance(item, dict) and item.get("id") is not None:
                by_id[str(item["id"])] = item
    except Exception:
        pass  # 아래 단건 폴백이 받는다
    out, usage_attached = [], False
    for d in pack:
        j = by_id.get(str(d[0]))
        if j is None:  # 묶음 응답 누락 → 단건 재판정(자체 _usage 있음)
            j = judge_doc(domain, hint, d[2], d[1], d[3], model=model,
                          body_chars=body_chars, no_think=no_think, doc_prompt=doc_prompt)
        elif not usage_attached:  # 묶음 총 토큰을 첫 문서에 한 번만 귀속(합계 정확)
            j = {**j, "_usage": pack_usage}
            usage_attached = True
        out.append((d, j))
    return out


def judge_doc(domain: str, hint: str, kind: str, title: str, body: str,
              model: str = "", body_chars: int = 3000, no_think: bool = True,
              doc_prompt: str = "") -> dict:
    """문서 1건 LLM 판정 — DB를 만지지 않아 스레드 병렬 안전. 서버 드라이런도 사용.
    doc_prompt: 관리 override(빈값=코드 기본 DOC_PROMPT)."""
    prompt = _fill((doc_prompt or "").strip() or DOC_PROMPT,
        domain=domain, hint=(hint or "").strip() or "(지침 없음 — 도메인명 기준으로 판정)",
        kind=(kind or "").strip(), title=(title or "").strip()[:300],
        body=_clip(body, body_chars))
    try:
        resp = llm.chat.completions.create(
            model=model or CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
            **_gen_kwargs(no_think, 800))  # 속성·관계 붙은 스키마의 출력 절단 방지 (400은 잘림 실측)
        m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
        d = _loads_lenient(m.group()) if m else {}
        d["_usage"] = _usage_of(resp)   # (입력토큰, 출력토큰) — 처리량 가시화
        return d
    except Exception as e:  # 판정 1건 실패가 배치를 죽이지 않게
        return {"_error": str(e)[:300]}


ROUTER_BODY_CHARS = 1500  # 장르 분류는 문서 머리만 읽는다 — 추출 body_chars와 독립


def build_router_prompt(cands: list) -> str:
    """장르 라우터 프롬프트 — 라인 후보(descr + 진입점·추천단위 표시명)로 문서 분류.
    cands: run 스냅샷 st["lines"] 항목들 ({line, descr, schema} 사용)."""
    rows = []
    for c in cands:
        sc = c.get("schema") or {}
        try:
            labels = " → ".join(x.get("label") or x.get("key", "") for x in chain_view(sc)[0])
        except KeyError:   # 스키마 없는 후보 — 설명만으로 분류
            labels = ""
        rows.append(f"- {c['line']}: {(c.get('descr') or '').strip() or '(설명 없음)'}"
                    + (f" [체인: {labels}]" if labels else ""))
    return f"""당신은 문서 분류기다. "{{domain}}" 도메인의 문서를 아래 추출 스키마 유형 중
가장 잘 맞는 하나로 분류하라. 어느 유형에도 맞지 않으면 none (그 문서는 제외된다).

[유형 후보]
{chr(10).join(rows)}

[문서]
유형: {{kind}}
제목: {{title}}
본문(앞부분): {{body}}

JSON 한 줄로만 답하라: {{"line": "<위 후보 이름 중 하나 또는 none>", "reason": "한 문장 근거"}}"""


def classify_doc(domain: str, cands: list, kind: str, title: str, body: str,
                 model: str = "", no_think: bool = True) -> dict:
    """문서 1건 장르 분류 — {"line": 후보명 또는 ""(제외), "reason"} 반환.
    후보명 검증을 여기서 끝낸다(환각·none → ""). DB를 만지지 않아 스레드 병렬 안전.
    # ponytail: 문서당 1요청 — 라우터 토큰비용이 문제되면 judge_pack처럼 묶음 분류"""
    prompt = _fill(build_router_prompt(cands),
        domain=domain, kind=(kind or "").strip(), title=(title or "").strip()[:300],
        body=_clip(body, ROUTER_BODY_CHARS))
    try:
        resp = llm.chat.completions.create(
            model=model or CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
            **_gen_kwargs(no_think, 150))
        m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
        d = _loads_lenient(m.group()) if m else {}
        line = str(d.get("line", "")).strip()
        if line not in {c["line"] for c in cands}:  # none·환각 라인명 → 제외로 정규화
            line = ""
        return {"line": line, "reason": str(d.get("reason", ""))[:300],
                "_usage": _usage_of(resp)}
    except Exception as e:  # 분류 1건 실패가 배치를 죽이지 않게
        return {"_error": str(e)[:300]}


def _usage_of(resp) -> tuple:
    """응답의 토큰 사용량 (prompt, completion) — 없으면 (0,0)."""
    u = getattr(resp, "usage", None)
    if not u:
        return (0, 0)
    return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)


def _loads_lenient(s: str) -> dict:
    """모델이 본문 인용 시 만드는 잘못된 이스케이프(\\한글 등) 복구 폴백."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s))
