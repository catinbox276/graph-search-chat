"""사내 도구 서버(REST) 전용 어댑터 — GET /tools + POST /call.

사내 서버는 MCP 도구 정의·결과 구조를 그대로 REST 두 엔드포인트로 노출한다
(2026-08-06 DataHub 공식 MCP와 필드 단위 비교 실측 — 차이는 전송 방식,
arguments→args, isError→ok 래퍼뿐. 무손실 1:1 매핑):

  GET  {주소}/tools → [{name, description, inputSchema, annotations?}, ...]
  POST {주소}/call  {"name": 도구명, "args": {...}}
                    → {"ok": true, "result": {"content": ..., "structuredContent": ...}}

- inputSchema(JSON Schema dict)는 langchain-core가 그대로 args_schema로 받는다.
- 도구 실행 오류는 예외가 아니라 문자열로 반환 — 에이전트가 우회를 판단하게
  (예외를 올리면 턴 전체가 죽는다. DataHub 실패→코퍼스 폴백과 같은 원리).
- 등록: mcp_registry에 transport='rest'로 (관리 페이지 /admin > MCP 서버).
"""
import json

import httpx
from langchain_core.tools import StructuredTool

from tools import config


def load_rest_tools(server_name: str, base_url: str) -> list:
    """서버의 /tools를 읽어 도구 목록을 StructuredTool로 변환."""
    base = base_url.rstrip("/")
    r = httpx.get(f"{base}/tools", timeout=config.REST_TOOL_TIMEOUT)
    r.raise_for_status()
    items = r.json()
    if isinstance(items, dict):  # {"tools": [...]} 래핑 형태도 수용
        items = items.get("tools") or []
    return [_make_tool(base, t) for t in items if isinstance(t, dict) and t.get("name")]


def _stringify(result) -> str:
    """MCP 결과 의미론: structuredContent(정형) 우선, 없으면 content(표시용)."""
    if not isinstance(result, dict):
        result = {"content": result}
    out = result.get("structuredContent") or result.get("content") or result
    if isinstance(out, str):
        return out[:8000]
    return json.dumps(out, ensure_ascii=False)[:8000]


def _make_tool(base: str, spec: dict) -> StructuredTool:
    tool_name = spec["name"]

    def call(**kwargs) -> str:
        try:
            r = httpx.post(f"{base}/call",
                           json={"name": tool_name, "args": kwargs},
                           timeout=config.REST_TOOL_TIMEOUT)
            if r.status_code != 200:
                return f"[도구 오류] HTTP {r.status_code}: {r.text[:300]}"
            j = r.json()
            if not j.get("ok", True):
                return f"[도구 오류] {json.dumps(j, ensure_ascii=False)[:500]}"
            return _stringify(j.get("result") or {})
        except Exception as e:
            return f"[도구 오류] {type(e).__name__}: {e}"

    return StructuredTool.from_function(
        func=call, name=tool_name,
        description=(spec.get("description") or tool_name)[:1000],
        args_schema=spec.get("inputSchema") or {"type": "object", "properties": {}})
