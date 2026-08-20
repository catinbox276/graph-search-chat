"""PoC 에이전트 (A) — LangChain DeepAgents + OpenAI 호환 모델 서빙.

- 모델: .env의 CHAT_URL/MODEL_NAME로 지정 (core/config.py). 로컬은 LM Studio, 사내는 vLLM
- 툴 1: 블로그 검색 함수 2개 (Oracle 조회, 함수 직접 등록)
- 툴 2: DataHub 공식 MCP 서버 (langchain-mcp-adapters로 연결)

usage: .venv/bin/python agent/agent.py "financial DB에서 계좌 테이블 뭐 있어?"
"""
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from deepagents import create_deep_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from core import config
from agent.identity import identity
from search.corpus_search import read_doc, search_docs, search_multi
from search.graph_explore import explore_entity
from search.path_suggest import suggest_paths

MODEL_URL = config.CHAT_URL
MODEL_NAME = config.CHAT_MODEL

# 한자(중국어) 차단용 guided_regex — "한자 블록이 아닌 모든 문자"의 반복(전체 매칭).
# 한글·영문·코드·숫자·기호·이모지는 통과, CJK 한자(통합+확장A+호환)만 생성 단계에서 제외.
# vLLM guided_regex(xgrammar)로 매 토큰 마스킹 → 한자 자리에 대체어가 생성됨(후처리 삭제 아님).
HANZI_BLOCK_RE = r"[^一-鿿㐀-䶿豈-﫿]*"

def default_system_prompt(st: dict | None = None) -> str:
    """코드 기본 시스템 프롬프트 — 정체성(이름·소개·지원범위)을 런타임에 주입한다.
    관리에서 agent_system_prompt를 지정하면 build_agent가 그걸 우선 쓴다."""
    name, intro, scope = identity(st)
    return f"""당신은 사내 데이터 분석가를 돕는 어시스턴트 "{name}"다.
사내 지식(블로그·VoC·FAQ) 검색과 검증된 해결 경로, 데이터 메타데이터 도구를 사용해
정확하게, 되도록 직접 답한다. 질문이 영어·혼용이든 상관없이 답변은 반드시 한국어로만
쓴다(고유명사·코드·식별자·urn·표 안의 값은 원문 유지).

[첫 판단 — 무엇을 다루는 질문인가]
1. 인사·감사·잡담(안녕, 고마워, 오늘 날씨 같은 것)이면 도구 없이 짧고 친근하게 화답한다.
2. 나 자신에 대한 질문(넌 뭐야, 누구야, 이름, 정체)이면 "{intro}"로 소개하고 마친다.
3. 지원 범위({scope}) 밖이면 도구를 호출하지도 되묻지도 말고, 즉시
   "죄송하지만 그 주제는 지원하지 않습니다"라고 명시한 뒤 사유와 지원 가능한 주제를
   안내한다. 이 판단은 아래 8·9의 되묻기·유사 제안보다 우선한다.
4. "아까", "전에", "직전 질문" 등 대화 회고·요약 요청이면 새로 검색하지 말고
   이 대화의 앞선 내용을 근거로 답한다.

[일반 질문 — 답을 구하는 흐름]
5. 새 목표/문제를 받으면 먼저 suggest_paths를 호출해 과거 검증 경로를 확인한다.
   검증된 경로가 있으면 그 방법을 우선 쓰고 "이전에 N회 검증된 방법"임을 언급한다.
   실패 이력이 경고되면 그 접근을 쓰기 전에 사실과 이유를 먼저 알린다.
6. 지식·문제해결 질문은 search_docs로 찾고, 필요하면 read_doc으로 전문을 읽어 실제
   내용으로 답한다. 여러 키워드로 넓게 찾아야 하면 search_docs를 여러 번 부르지 말고
   search_multi에 키워드들을 모아 "한 번에" 검색한다(중복 키워드·같은 검색 반복 금지 —
   병렬로 빠르게 융합된다). 경로 요약만 보고 절차를 지어내지 않는다. 근거로 쓴 문장 끝에는
   문서 id를 대괄호 그대로 붙인다 (예: "...메뉴에서 확인할 수 있습니다 [blog_posts:kin-1507]"
   — 화면에서 자동으로 [1] 각주와 하단 참고 문서 목록으로 변환된다). suggest_paths가
   근거 문서 id를 제시하면 read_doc으로 열람해 실제 내용 기반으로 답한다.
   데이터 질문(테이블·스키마·조인·리니지)은 DataHub 도구로 조회하고 데이터셋 urn을 제시한다.
7. 절차/방법/설정/가이드형 질문(어떻게, 방법, 절차, 설정)은 단계를 빠짐없이 순서대로
   직접 안내한다. "문서를 열람해 보세요" 식으로 우회하지 않고 본문 내용을 제공한다.
7-1. 특정 기술·제품·도구·개념의 연관 항목을 묻는 질문("X는 무엇과 함께 쓰이나",
   "X 관련 사례/작업")이나, 검색 결과에 나온 핵심 엔티티의 주변을 넓혀야 할 때는
   explore_entity로 관계망을 조회한다. 결과의 관계(A —타입→ B)와 근거 문서 id를
   활용하고, 필요하면 read_doc으로 근거 문서를 열람해 실제 내용으로 답한다.

[근거를 못 찾았을 때 — 미루지 말고, 순서대로]
8. 지원 범위 안의 질문인데 근거를 못 찾으면 답을 지어내지 말고 두 단계로 처리한다.
   ① 먼저 "관련 근거를 찾지 못했습니다"라고 분명히 밝힌다.
   ② 그 다음에만, 비슷한 후보 문서가 있으면 제목을 제안하거나, 없으면 "오류 코드·사용
      메뉴·문서 제목 등을 알려주시면 더 정확히 찾아드리겠습니다"로 되묻는다.
   ①을 건너뛰고 곧장 되묻거나 유사 제안만 내지 않는다.
9. 어떤 경우에도 빈 응답을 내지 않는다. 도저히 답할 수 없으면 "답변할 수 없습니다"와
   그 사유(근거 없음·데이터 미적재·지원 범위 밖)를 명시하고 가능한 다음 행동을 제안한다.
10. 근거가 분명하면 즉시 직접 답한다. "설명드리겠습니다" 같은 빈말·사족·일반 상식 폴백을
    붙이지 않는다. 실제 도구 결과에 나온 문서만 인용하고, 보지 않은 문서는 인용하지 않는다.

[금지]
- 근거 없이 답하기, 지원 범위 밖 질문에 아는 척 답하거나 되묻기, 빈 응답, 한국어 외
  언어로 답하기, 답 대신 "참고하세요"로 미루기."""

BUILTIN_TOOLS = (suggest_paths, search_docs, search_multi, read_doc, explore_entity)

# DeepAgents가 create_deep_agent에서 미들웨어로 자동 부착하는 내장 도구 — 우리가 등록한
# 게 아니라 프레임워크 스캐폴딩이라 agent_disabled_tools 필터가 닿지 않는다(항상 켜짐).
# 관리 페이지에 "이런 게 켜져 있다"를 보여주기 위한 가시화용 목록(fixed=끌 수 없음).
DEEPAGENTS_BUILTIN = (
    ("write_todos", "할 일 목록 관리(플래너)"),
    ("task", "서브에이전트에 하위 작업 위임"),
    ("ls", "작업공간 파일 목록"),
    ("read_file", "파일 읽기"),
    ("write_file", "파일 쓰기"),
    ("edit_file", "파일 부분 수정"),
    ("glob", "이름 패턴으로 파일 찾기"),
    ("grep", "내용으로 파일 검색"),
    ("execute", "셸 실행(샌드박스 백엔드 없으면 비활성)"),
)


def load_agent_settings() -> dict:
    """전역 에이전트 설정 (app_settings — 관리 페이지 /admin에서 변경).
    DB를 못 읽으면 코드 기본값으로 동작 (CLI 단독 실행 등)."""
    out = {"system_prompt": "", "disabled_tools": set(), "mcp_enabled": True,
           "no_think": False, "block_hanzi": False}
    try:
        from core import settings
        st = settings.get_all()  # ORM — 접속·반납은 db.session()이 관리
        out["system_prompt"] = (st.get("agent_system_prompt") or "").strip()
        out["disabled_tools"] = {t.strip() for t in
                                 (st.get("agent_disabled_tools") or "").split(",")
                                 if t.strip()}
        out["mcp_enabled"] = st.get("agent_mcp_enabled", "1") != "0"
        out["no_think"] = st.get("agent_no_think", "") == "1"
        out["block_hanzi"] = st.get("agent_block_hanzi", "") == "1"
    except Exception as e:
        print(f"[경고] 에이전트 설정 조회 실패 — 기본값 사용: {e}", file=sys.stderr)
    return out


def _tool_name(t) -> str:
    return getattr(t, "name", None) or getattr(t, "__name__", "")


def _mcp_config(row: dict) -> dict:
    """레지스트리 행 → langchain-mcp-adapters 커넥션 설정."""
    if row["transport"] == "stdio":  # 범용 stdio — command는 등록 행에서
        cmd = row["command"] or ""
        return {"command": shutil.which(cmd) or str(ROOT.parent / f".venv/bin/{cmd}"),
                "args": [], "transport": "stdio"}
    return {"transport": row["transport"], "url": row["url"]}


def _mcp_servers() -> list:
    """등록된 도구 서버 목록 (mcp_registry) — DB 미기동 시 없음."""
    try:
        from core import mcp_registry
        return mcp_registry.list_servers(enabled_only=True)
    except Exception as e:
        print(f"[경고] MCP 레지스트리 조회 실패 — 도구 서버 없이 동작: {e}",
              file=sys.stderr)
        return []


def _exc_detail(e, depth=0) -> str:
    """ExceptionGroup('unhandled errors in a TaskGroup') 언랩 — 진짜 원인 노출."""
    subs = getattr(e, "exceptions", None)
    if subs and depth < 3:
        return "; ".join(_exc_detail(x, depth + 1) for x in subs[:3])
    return f"{type(e).__name__}: {str(e)[:200]}"


async def _mcp_tools():
    tools = []
    for s in _mcp_servers():  # 서버별 격리 — 하나가 실패해도 나머지는 산다
        try:
            if s["transport"] == "rest":  # 사내 REST 도구 서버 (전용 어댑터)
                from core.rest_tools import load_rest_tools
                tools += load_rest_tools(s["name"], s["url"])
            else:
                tools += await MultiServerMCPClient(
                    {s["name"]: _mcp_config(s)}).get_tools()
        except Exception as e:
            print(f"[경고] 도구 서버 '{s['name']}' 연결 실패 — 제외하고 계속: "
                  f"{_exc_detail(e)}", file=sys.stderr)
    return tools


def _source_tools() -> list:
    """등록 소스마다 소스 한정 검색 도구 자동 생성 (search_{소스명})."""
    try:
        from search.corpus_search import source_search_tools
        return source_search_tools()
    except Exception as e:
        print(f"[경고] 소스 검색 도구 생성 실패: {e}", file=sys.stderr)
        return []


async def discover_tools() -> list:
    """관리 페이지용 — 사용 가능한 도구 전체 (비활성 포함).
    {name, description, source} — source: builtin / source / mcp:서버명."""
    out = [{"name": _tool_name(t), "description": (t.__doc__ or "").strip().split("\n")[0],
            "source": "builtin"} for t in BUILTIN_TOOLS]
    # DeepAgents 내장 도구 — 가시화만(끌 수 없음). fixed=True로 UI가 읽기전용 처리.
    out += [{"name": n, "description": d, "source": "deepagents", "fixed": True}
            for n, d in DEEPAGENTS_BUILTIN]
    out += [{"name": _tool_name(t),
             "description": (t.__doc__ or "").strip().split("\n")[0],
             "source": "source"} for t in _source_tools()]
    for s in _mcp_servers():
        try:
            if s["transport"] == "rest":
                from core.rest_tools import load_rest_tools
                found = load_rest_tools(s["name"], s["url"])
                tag = f"rest:{s['name']}"
            else:
                found = await MultiServerMCPClient({s["name"]: _mcp_config(s)}).get_tools()
                tag = f"mcp:{s['name']}"
            for t in found:
                out.append({"name": _tool_name(t),
                            "description": (getattr(t, "description", "") or "").split("\n")[0],
                            "source": tag})
        except Exception as e:
            print(f"[경고] 도구 서버 '{s['name']}' 목록 조회 실패: {_exc_detail(e)}",
                  file=sys.stderr)
    return out


async def build_agent(checkpointer=None, model_name=None):
    ag = load_agent_settings()
    tools = list(BUILTIN_TOOLS) + _source_tools()
    if ag["mcp_enabled"]:
        try:
            tools += await _mcp_tools()
        except Exception as e:  # MCP 미기동 시 검색 도구만으로 동작
            print(f"[경고] MCP 연결 실패, 검색 도구만 사용: {e}", file=sys.stderr)
    if ag["disabled_tools"]:
        tools = [t for t in tools if _tool_name(t) not in ag["disabled_tools"]]
    # 서빙 파라미터(extra_body): no_think=생각 출력 끔(Qwen3), block_hanzi=한자 차단(guided_regex)
    eb = {}
    if ag["no_think"]:
        eb["chat_template_kwargs"] = {"enable_thinking": False}
    if ag["block_hanzi"]:
        eb["guided_regex"] = HANZI_BLOCK_RE
    # 모델별 서빙 주소·개발 키 해석 (레지스트리 우선, 없으면 .env 폴백) —
    # 모델마다 호스트·키가 다른 사내 게이트웨이 환경 지원
    from core import model_registry
    url, resolved, key = model_registry.chat_endpoint(model_name or MODEL_NAME)
    model = ChatOpenAI(
        base_url=url,
        api_key=key,
        model=resolved,
        temperature=config.LLM_TEMPERATURE,
        **({"extra_body": eb} if eb else {}),
    )
    return create_deep_agent(model=model, tools=tools,
                             system_prompt=ag["system_prompt"] or default_system_prompt(),
                             checkpointer=checkpointer)


async def run(question: str):
    agent = await build_agent()
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    for m in result["messages"]:
        for c in getattr(m, "tool_calls", None) or []:
            print(f"[툴 호출] {c['name']}({c['args']})")
    print("\n=== 답변 ===")
    print(result["messages"][-1].content)


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "financial DB에서 계좌 관련 테이블은 뭐가 있어?"
    asyncio.run(run(q))
