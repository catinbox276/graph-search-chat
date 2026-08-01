"""PoC 에이전트 (A) — LangChain DeepAgents + 로컬 모델 서빙(LM Studio).

- 모델: http://127.0.0.1:1234 (OpenAI 호환, 기본 qwen/qwen3.6-35b-a3b)
- 툴: 블로그 검색 함수 2개 (Oracle 조회). DataHub 공식 MCP는 이후 추가.

usage: .venv/bin/python agent/agent.py "pip install이 프록시 뒤에서 안 돼"
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from tools.blog_search import read_blog_post, search_blog

MODEL_URL = os.environ.get("MODEL_URL", "http://127.0.0.1:1234/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen3.6-27b-mtp")

SYSTEM_PROMPT = """당신은 사내 데이터 분석가를 돕는 어시스턴트다.

- 사내 노하우/문제해결 질문(설치 오류, 설정, 사용법 등)은 반드시 search_blog로
  기존 해결 글을 먼저 찾고, 필요하면 read_blog_post로 전문을 읽은 뒤 답한다.
- 답변에는 근거로 삼은 글의 id와 url을 함께 제시한다.
- 검색 결과가 없으면 없다고 말하고 일반 지식으로 답하되 그 사실을 밝힌다.
- 한국어로 답한다."""


def build_agent():
    model = ChatOpenAI(
        base_url=MODEL_URL,
        api_key=os.environ.get("MODEL_API_KEY", "lm-studio"),
        model=MODEL_NAME,
        temperature=0,
    )
    return create_deep_agent(
        model=model,
        tools=[search_blog, read_blog_post],
        system_prompt=SYSTEM_PROMPT,
    )


def main():
    question = sys.argv[1] if len(sys.argv) > 1 else "pip install이 프록시 뒤에서 안 되는데 어떻게 해?"
    agent = build_agent()
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    for m in result["messages"]:
        calls = getattr(m, "tool_calls", None)
        if calls:
            for c in calls:
                print(f"[툴 호출] {c['name']}({c['args']})")
    print("\n=== 답변 ===")
    print(result["messages"][-1].content)


if __name__ == "__main__":
    main()
