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
from search.corpus_search import read_doc, search_docs
from search.path_suggest import suggest_paths

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
- 사내 노하우/문제해결 질문(설치 오류, 설정, 사용법)은 search_docs로 기존 해결 글을
  찾고, 필요하면 read_doc로 전문을 읽은 뒤 답한다. suggest_paths가 근거 문서
  id를 제시하면 그 문서를 read_doc로 열람해 실제 내용 기반으로 답한다 —
  경로 요약만 보고 세부 절차를 지어내지 말 것.
- 검색·열람·경로 제안 결과의 문서를 근거로 쓴 문장 끝에는 그 문서 id를 대괄호
  그대로 표기한다. 예: "...메뉴에서 확인할 수 있습니다 [blog_posts:kin-1507]".
  화면에서 자동으로 [1] 번호 각주와 하단 참고 문서 목록으로 변환된다.
  실제로 도구 결과에 나온 문서 id만 인용 — 보지 않은 문서를 지어내지 말 것.
  데이터 질문은 데이터셋 urn 제시.
- 근거를 못 찾으면 그 사실을 밝히고 일반 지식으로 답한다.
- 한국어로 답한다."""

BUILTIN_TOOLS = (suggest_paths, search_docs, read_doc)


def load_agent_settings() -> dict:
    """전역 에이전트 설정 (app_settings — 관리 페이지 /admin에서 변경).
    DB를 못 읽으면 코드 기본값으로 동작 (CLI 단독 실행 등)."""
    out = {"system_prompt": "", "disabled_tools": set(), "mcp_enabled": True}
    try:
        from core import settings
        st = settings.get_all()  # ORM — 접속·반납은 db.session()이 관리
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


def _mcp_config(row: dict) -> dict:
    """레지스트리 행 → langchain-mcp-adapters 커넥션 설정."""
    if row["transport"] == "stdio":
        cmd = row["command"] or "mcp-server-datahub"
        return {"command": shutil.which(cmd) or str(ROOT.parent / f".venv/bin/{cmd}"),
                "args": [], "transport": "stdio",
                "env": {"DATAHUB_GMS_URL": DATAHUB_GMS}}
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
    out += [{"name": _tool_name(t),
             "description": (t.__doc__ or "").strip().split("\n")[0],
             "source": "source"} for t in _source_tools()]
    for s in _mcp_servers():
        try:
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
