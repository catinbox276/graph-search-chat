"""PoC 에이전트 (A) — LangChain DeepAgents + OpenAI 호환 모델 서빙.

- 모델: .env의 CHAT_URL/MODEL_NAME로 지정 (tools/config.py). 로컬은 LM Studio, 사내는 vLLM
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

from tools import config
from tools.blog_search import read_blog_post, search_blog
from tools.path_suggest import suggest_paths

MODEL_URL = config.CHAT_URL
MODEL_NAME = config.CHAT_MODEL
DATAHUB_GMS = config.DATAHUB_GMS_URL

SYSTEM_PROMPT = """당신은 사내 데이터 분석가를 돕는 어시스턴트다.

- 새로운 문제/목표를 받으면 **가장 먼저 suggest_paths를 호출**해 과거 조직의
  해결 경로를 확인한다. 검증된 경로가 있으면 그 도구·방법을 우선 시도하고,
  답변에 "이전에 N회 검증된 방법"임을 언급한다.
- suggest_paths가 실패 이력을 경고하면, 그 접근을 쓰기 전에 사용자에게
  과거 실패 사실과 이유를 먼저 알린다 (차단하지는 말 것).
- 데이터 질문(테이블 찾기, 스키마, 조인, 리니지)은 DataHub 도구로 조회한다.
- 사내 노하우/문제해결 질문(설치 오류, 설정, 사용법)은 search_blog로 기존 해결 글을
  찾고, 필요하면 read_blog_post로 전문을 읽은 뒤 답한다. suggest_paths가 근거 문서
  id를 제시하면 그 문서를 read_blog_post로 열람해 실제 내용 기반으로 답한다 —
  경로 요약만 보고 세부 절차를 지어내지 말 것.
- 검색·열람·경로 제안 결과의 문서를 근거로 쓴 문장 끝에는 그 문서 id를 대괄호
  그대로 표기한다. 예: "...메뉴에서 확인할 수 있습니다 [blog_posts:kin-1507]".
  화면에서 자동으로 [1] 번호 각주와 하단 참고 문서 목록으로 변환된다.
  실제로 도구 결과에 나온 문서 id만 인용 — 보지 않은 문서를 지어내지 말 것.
  데이터 질문은 데이터셋 urn 제시.
- 근거를 못 찾으면 그 사실을 밝히고 일반 지식으로 답한다.
- 한국어로 답한다."""

BUILTIN_TOOLS = (suggest_paths, search_blog, read_blog_post)


def load_agent_settings() -> dict:
    """전역 에이전트 설정 (app_settings — 관리 페이지 /admin에서 변경).
    DB를 못 읽으면 코드 기본값으로 동작 (CLI 단독 실행 등)."""
    out = {"system_prompt": "", "disabled_tools": set(), "mcp_enabled": True}
    try:
        import oracledb

        from tools import settings
        con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                               dsn=config.ORACLE_DSN)
        st = settings.get_all(con.cursor())
        con.close()
        out["system_prompt"] = (st.get("agent_system_prompt") or "").strip()
        out["disabled_tools"] = {t.strip() for t in
                                 (st.get("agent_disabled_tools") or "").split(",")
                                 if t.strip()}
        out["mcp_enabled"] = st.get("agent_mcp_enabled", "1") != "0"
    except Exception as e:
        print(f"[경고] 에이전트 설정 조회 실패 — 기본값 사용: {e}", file=sys.stderr)
    return out


def _tool_name(t) -> str:
    return getattr(t, "name", None) or getattr(t, "__name__", "")


async def _mcp_tools():
    client = MultiServerMCPClient({
        "datahub": {
            "command": shutil.which("mcp-server-datahub")
                       or str(ROOT / ".venv/bin/mcp-server-datahub"),
            "args": [],
            "transport": "stdio",
            "env": {"DATAHUB_GMS_URL": DATAHUB_GMS},
        }
    })
    return await client.get_tools()


async def discover_tools() -> list:
    """관리 페이지용 — 사용 가능한 도구 전체 (비활성 포함). {name, description, source}."""
    out = [{"name": _tool_name(t), "description": (t.__doc__ or "").strip().split("\n")[0],
            "source": "builtin"} for t in BUILTIN_TOOLS]
    try:
        for t in await _mcp_tools():
            out.append({"name": _tool_name(t),
                        "description": (getattr(t, "description", "") or "").split("\n")[0],
                        "source": "mcp"})
    except Exception as e:
        print(f"[경고] MCP 도구 목록 조회 실패: {e}", file=sys.stderr)
    return out


async def build_agent(checkpointer=None, model_name=None):
    ag = load_agent_settings()
    tools = list(BUILTIN_TOOLS)
    if ag["mcp_enabled"]:
        try:
            tools += await _mcp_tools()
        except Exception as e:  # DataHub 미기동 시 블로그 검색만으로 동작
            print(f"[경고] DataHub MCP 연결 실패, 블로그 검색만 사용: {e}", file=sys.stderr)
    if ag["disabled_tools"]:
        tools = [t for t in tools if _tool_name(t) not in ag["disabled_tools"]]
    model = ChatOpenAI(
        base_url=MODEL_URL,
        api_key=config.MODEL_API_KEY,
        model=model_name or MODEL_NAME,
        temperature=config.LLM_TEMPERATURE,
    )
    return create_deep_agent(model=model, tools=tools,
                             system_prompt=ag["system_prompt"] or SYSTEM_PROMPT,
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
