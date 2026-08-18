"""입력 라우터(triage) — 에이전트 호출 직전에 질문 1개를 분류하는 앞단 단계.

매 턴, 값싼 LLM 1콜로 {intent, normalized_query, reply}를 뽑는다.
- 지원밖·잡담·자기소개·되묻기 → 에이전트·검색을 돌리지 않고 즉시 응답(하드 숏컷).
- 정상 질문 → 오타를 1회 교정한 normalized_query로 에이전트에 넘긴다(query rewrite).

설계 근거: docs/chatbot-response-quality-exploration.md (Tier 2).
원칙은 fail-open — 분류가 안 되거나 애매하면 무조건 normal로 흘려 에이전트가 처리한다.
라우터가 검색·답변을 막는 오탐이 가장 큰 리스크라, 확실할 때만 숏컷한다.
"""
import asyncio
import json
import re
import sys

from openai import OpenAI

from core import config, model_registry

_INTENTS = ("smalltalk", "self", "out_of_scope", "clarify", "normal")

_INTRO = config.AGENT_INTRO or \
    f"사내 데이터와 지식 검색을 돕는 어시스턴트 '{config.AGENT_NAME}'입니다"

_SYS = f"""너는 사내 어시스턴트 "{config.AGENT_NAME}"의 입력 분류기다.
사용자의 마지막 질문 하나를 읽고 아래 JSON 객체 "하나만" 출력한다(설명·코드펜스 금지).

{{"intent": "<값>", "normalized_query": "<문자열>", "reply": "<문자열>"}}

intent 값과 규칙:
- "smalltalk": 인사·감사·잡담(안녕, 고마워, 수고 등). reply = 짧고 친근한 한국어 화답.
- "self": 너의 정체·이름·역할을 묻는 질문. reply = "{_INTRO}".
- "out_of_scope": 지원 범위({config.AGENT_SCOPE}) 밖(일반 상식, 코딩 대행, 시세·날씨
  등 사내 데이터·지식과 무관). reply = "죄송하지만 그 주제는 지원하지 않습니다."로
  시작해 사유와 지원 가능한 주제를 1~2문장으로 안내.
- "clarify": 지원 범위 안이지만 검색할 대상이 하나도 없을 만큼 막연할 때만("에러 났어"
  처럼 무엇에 대한 건지 전혀 없음). reply = 무엇을 알려주면 되는지 1문장 되묻기.
- "normal": 위에 확실히 해당하지 않으면 전부 이것. 이전 대화 참조("아까","그거","더
  자세히")나 조금이라도 애매하면 반드시 normal.

normalized_query: intent가 normal일 때만 채운다. 명백한 오타·띄어쓰기·문법 오류가 있으면
1회 교정한 문장을, 없으면 원문 그대로 둔다. 의미는 절대 바꾸지 않는다. 그 외 intent면 "".
reply: normal이면 "". 그 외에는 한국어로 채운다.
확신이 없으면 normal로 둔다. 분류기는 검색을 막아선 안 된다."""

_client = None


def _chat():
    global _client
    if _client is None:  # ROUTER_URL 지정 시 그 엔드포인트, 비면 CHAT_URL 재사용
        _client = OpenAI(base_url=config.ROUTER_URL or config.CHAT_URL,
                         api_key=config.MODEL_API_KEY, timeout=config.LLM_TIMEOUT)
    return _client


def _model() -> str:
    """라우터 모델 — ROUTER_MODEL 지정 시 그것, 비면 기본 LLM 재사용."""
    return config.ROUTER_MODEL or model_registry.get_default("llm", config.CHAT_MODEL)


def _parse(text: str) -> dict:
    """모델 출력에서 첫 JSON 객체만 뽑아 파싱 — 코드펜스·잡설이 붙어도 견딘다."""
    m = re.search(r"\{.*\}", (text or "").strip(), re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _call(msg: str) -> dict:
    r = _chat().chat.completions.create(
        model=_model(),
        temperature=0, max_tokens=400,
        messages=[{"role": "system", "content": _SYS},
                  {"role": "user", "content": msg}])
    return _parse(r.choices[0].message.content or "")


async def triage(message: str) -> dict:
    """질문 1개 분류. 반환: {intent, normalized_query, reply}.
    intent!=normal이면 reply로 즉시 응답, normal이면 normalized_query로 에이전트 호출."""
    msg = (message or "").strip()
    if not msg:
        return {"intent": "normal", "normalized_query": msg, "reply": ""}
    try:  # 동기 호출은 스레드로 — 이벤트 루프(스트리밍) 블로킹 방지
        out = await asyncio.to_thread(_call, msg)
    except Exception as e:
        print(f"[경고] triage 실패 — normal 폴백: {e}", file=sys.stderr)
        return {"intent": "normal", "normalized_query": msg, "reply": ""}

    intent = out.get("intent") if out.get("intent") in _INTENTS else "normal"
    reply = (out.get("reply") or "").strip()
    if intent == "normal" or not reply:  # 숏컷인데 문구가 없으면 안전하게 normal
        nq = (out.get("normalized_query") or "").strip() or msg
        return {"intent": "normal", "normalized_query": nq, "reply": ""}
    return {"intent": intent, "normalized_query": msg, "reply": reply}


if __name__ == "__main__":  # 파서 자체검증(LLM 불필요): python -m agent.triage
    assert _parse('{"intent":"normal","reply":""}')["intent"] == "normal"
    assert _parse('```json\n{"intent":"self","reply":"x"}\n```')["intent"] == "self"
    assert _parse('설명\n{"intent": "out_of_scope"}\n끝') == {"intent": "out_of_scope"}
    assert _parse("no json") == {} and _parse("{bad json}") == {}
    print("triage._parse self-check OK")
