"""문서 LLM 판정 — DB를 만지지 않는 순수 판정 계층 (스레드 병렬 안전).

- judge_doc: 문서 1건 판정 (서버 드라이런도 사용)
- judge_pack: 문서 묶음을 요청 1건으로 판정 — 누락분은 단건 폴백 (유실 없음)
"""
import json
import re

from core import config
from graph.graph_pipeline import CHAT_MODEL, llm

# ── 추출 스키마 — 관리자가 역할·키·표시명·설명을 정의, 프롬프트는 여기서 생성 ──
# 역할→저장층 매핑: entry(진입점)→그래프 2층, solution(추천단위)→3층, attr(속성)→5층.
# 관리자는 이름·키·설명을 자유로 바꾸고(버전 관리), 출력 형식은 코드가 생성해 잠근다.
DEFAULT_SCHEMA = {
    "entry":    {"key": "goal", "label": "목표", "desc": "문서가 다루는 문제/목표"},
    "solution": {"key": "approach", "label": "접근법", "desc": "핵심 해법/접근법"},
    "attrs": [],
}


def norm_schema(etypes) -> dict:
    """etypes 행([{key,label,desc,role}]) → {entry, solution, attrs[]} 정규화.
    role 없는 행=attr(하위호환), entry/solution 미정의=기본 goal/approach."""
    out = {"entry": dict(DEFAULT_SCHEMA["entry"]),
           "solution": dict(DEFAULT_SCHEMA["solution"]), "attrs": []}
    for t in (etypes or []):
        if not (isinstance(t, dict) and str(t.get("key", "")).strip()):
            continue
        row = {"key": str(t["key"]).strip(),
               "label": str(t.get("label", "")).strip() or str(t["key"]).strip(),
               "desc": str(t.get("desc", "")).strip()}
        role = str(t.get("role", "")).strip()
        if role in ("entry", "solution"):
            out[role] = row
        else:
            out["attrs"].append(row)
    return out


def _out_fields(schema: dict, brief: bool = False) -> str:
    """출력 형식 JSON의 스키마 필드 부분 — desc가 추출 기준으로 들어간다."""
    e, s = schema["entry"], schema["solution"]
    suffix = "" if brief else " (한 문장, fits=true일 때만)"
    lines = [f' "{e["key"]}": "{e["desc"] or e["label"]}{suffix}"',
             f' "{s["key"]}": "{s["desc"] or s["label"]}{suffix}"']
    for a in schema["attrs"]:
        lines.append(f' "{a["key"]}": "{a["desc"] or a["label"]}'
                     f' (문서에서 확인될 때만 — 없으면 이 키를 생략)"')
    return ",\n".join(lines)


def _crit_block(criteria: str) -> str:
    criteria = (criteria or "").strip()
    return f"\n엔티티 판정·추출 지침:\n{criteria}" if criteria else ""


def build_doc_prompt(schema: dict | None = None, criteria: str = "") -> str:
    """단건 판정 프롬프트를 스키마에서 생성 — 출력 형식·placeholder 배선은 코드 잠금."""
    sc = schema or DEFAULT_SCHEMA
    e, s = sc["entry"], sc["solution"]
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
- {e["label"]}도 {s["label"]}도 찾을 수 없는 글, 결말·결론 없이 끝나는 글
- 내용이 너무 빈약해 지식으로 일반화할 수 없는 글"""


def build_pack_prompt(schema: dict | None = None, criteria: str = "") -> str:
    """묶음 판정 프롬프트 — 단건과 같은 스키마, 배열 포장만 다름."""
    sc = schema or DEFAULT_SCHEMA
    e, s = sc["entry"], sc["solution"]
    return f"""여러 문서 각각이 도메인 기준에 맞는지 판정하고, 맞으면 지식을 추출하라. JSON 배열만 출력.

도메인: {{domain}}
도메인 기준·추출 지침: {{hint}}{_crit_block(criteria)}

문서 목록 — 각 문서는 ===[문서id]=== 로 시작한다:
{{docs}}

출력 형식: 문서마다 1개씩, 입력 순서대로 JSON 배열.
[{{"id": "문서id", "fits": true|false, "reason": "판정 근거 한 문장",
{_out_fields(sc, brief=True)}}}, ...]

fits=false로 판정할 것: 도메인과 무관 / {e["label"]}도 {s["label"]}도 없음 / 결말·결론 없음 / 내용이 빈약함.
각 문서는 독립적으로 판정하라 — 다른 문서의 내용이 판정에 영향을 주면 안 된다."""


def build_prompts(schema: dict | None = None, criteria: str = "") -> tuple:
    return build_doc_prompt(schema, criteria), build_pack_prompt(schema, criteria)


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
            **_gen_kwargs(no_think, 400))
        m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
        d = _loads_lenient(m.group()) if m else {}
        d["_usage"] = _usage_of(resp)   # (입력토큰, 출력토큰) — 처리량 가시화
        return d
    except Exception as e:  # 판정 1건 실패가 배치를 죽이지 않게
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
