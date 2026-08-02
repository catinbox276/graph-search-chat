"""PoC 에이전트 (A) — LangChain DeepAgents + 로컬 모델 서빙(LM Studio).

- 모델: http://127.0.0.1:1234 (OpenAI 호환, 기본 qwen3.6-27b-mtp)
- 툴 1: 블로그 검색 함수 2개 (Oracle 조회, 함수 직접 등록)
- 툴 2: DataHub 공식 MCP 서버 (langchain-mcp-adapters로 연결)

usage: .venv/bin/python agent/agent.py "financial DB에서 계좌 테이블 뭐 있어?"
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from deepagents import create_deep_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from tools.blog_search import read_blog_post, search_blog
from tools.path_suggest import suggest_paths

MODEL_URL = os.environ.get("MODEL_URL", "http://127.0.0.1:1234/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen/qwen3.6-35b-a3b")
DATAHUB_GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")

SYSTEM_PROMPT = """당신은 사내 데이터 분석가를 돕는 어시스턴트다.

- 새로운 문제/목표를 받으면 **가장 먼저 suggest_paths를 호출**해 과거 조직의
  해결 경로를 확인한다. 검증된 경로가 있으면 그 도구·방법을 우선 시도하고,
  답변에 "이전에 N회 검증된 방법"임을 언급한다.
- suggest_paths가 실패 이력을 경고하면, 그 접근을 쓰기 전에 사용자에게
  과거 실패 사실과 이유를 먼저 알린다 (차단하지는 말 것).
- 데이터 질문(테이블 찾기, 스키마, 조인, 리니지)은 DataHub 도구로 조회한다.
- 사내 노하우/문제해결 질문(설치 오류, 설정, 사용법)은 search_blog로 기존 해결 글을
  찾고, 필요하면 read_blog_post로 전문을 읽은 뒤 답한다.
- 답변에는 근거(글 id/url 또는 데이터셋 urn)를 함께 제시한다.
- 근거를 못 찾으면 그 사실을 밝히고 일반 지식으로 답한다.
- 한국어로 답한다."""


async def build_agent(checkpointer=None, model_name=None):
    tools = [suggest_paths, search_blog, read_blog_post]
    try:
        client = MultiServerMCPClient({
            "datahub": {
                "command": str(ROOT / ".venv/bin/mcp-server-datahub"),
                "args": [],
                "transport": "stdio",
                "env": {"DATAHUB_GMS_URL": DATAHUB_GMS},
            }
        })
        tools += await client.get_tools()
    except Exception as e:  # DataHub 미기동 시 블로그 검색만으로 동작
        print(f"[경고] DataHub MCP 연결 실패, 블로그 검색만 사용: {e}", file=sys.stderr)
    model = ChatOpenAI(
        base_url=MODEL_URL,
        api_key=os.environ.get("MODEL_API_KEY", "lm-studio"),
        model=model_name or MODEL_NAME,
        temperature=0,
    )
    return create_deep_agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT,
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
