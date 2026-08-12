"""모델 호출 계층 — 챗 LLM 클라이언트·프롬프트·임베딩. 이 패키지에서 모델을 부르는 곳은 여기뿐.

- llm/_llm_json: 챗 호출 + JSON 파싱 (판정·추출 공용)
- llm_same/llm_select: dedup 보조 판정 (경계 구간에서만 호출 — merge.py가 사용)
- embed/cosine: 임베딩 벡터와 유사도 (게이트·병합·재발 판정 공용)
"""
import json
import re

from openai import OpenAI

from core import config

llm = OpenAI(base_url=config.CHAT_URL, api_key=config.MODEL_API_KEY,
             timeout=config.LLM_TIMEOUT)   # 멈춘 요청이 파이프라인을 무한 대기시키지 않게
# 임베딩 클라이언트는 model_registry.embedding_client()가 해석 (레지스트리 우선)
CHAT_MODEL = config.CHAT_MODEL

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


def embed(text: str) -> list:
    from core import model_registry
    cli, emb_name = model_registry.embedding_client()
    return cli.embeddings.create(model=emb_name, input=text).data[0].embedding


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
