"""PoC 채팅 UI 서버 — 에이전트를 웹에서 테스트하고 세션을 Oracle 증거 테이블에 기록.

- 멀티턴 기억: LangGraph MemorySaver + thread_id=세션id (서버 재시작 시 초기화)
- 실시간 진행 표시: /chat/stream SSE — 툴 호출 이벤트를 즉시 전송

usage: .venv/bin/uvicorn app.server:app --port 8500
접속: http://localhost:8500
"""
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from agent.agent import build_agent
from tools.blog_search import DSN, PASSWORD, USER, load_matrix

app = FastAPI()
_agent = None


def db():
    return oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)


@app.on_event("startup")
async def startup():
    global _agent
    load_matrix()  # 임베딩 행렬 메모리 적재 (하이브리드 검색)
    _agent = await build_agent(checkpointer=MemorySaver())  # 멀티턴 기억
    con = db()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'SESSIONS'")
    if not cur.fetchone()[0]:
        cur.execute("""
            CREATE TABLE sessions (
              id         VARCHAR2(36),
              turn       NUMBER,
              ts         TIMESTAMP DEFAULT SYSTIMESTAMP,
              question   CLOB,
              tool_calls CLOB,
              answer     CLOB,
              verdict    VARCHAR2(20),   -- 세션 게이트 판정 (success/fail/unknown)
              PRIMARY KEY (id, turn)
            )
        """)
        con.commit()
    con.close()


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str


def log_turn(sid: str, question: str, calls: list, answer: str):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT NVL(MAX(turn),0)+1 FROM sessions WHERE id = :1", [sid])
    turn = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sessions (id, turn, question, tool_calls, answer) "
        "VALUES (:1, :2, :3, :4, :5)",
        [sid, turn, question, json.dumps(calls, ensure_ascii=False), answer])
    con.commit()
    con.close()


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def prettify_result(result: str) -> str:
    """MCP 응답의 이스케이프 JSON 덩어리를 사람이 읽게 정리."""
    try:
        data = json.loads(result)
        # MCP content 포맷 [{"type":"text","text":"..."}] 언랩
        if isinstance(data, list) and data and isinstance(data[0], dict) \
                and data[0].get("type") == "text":
            result = "\n".join(d.get("text", "") for d in data
                               if d.get("type") == "text")
            try:
                data = json.loads(result)
            except json.JSONDecodeError:
                return result
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False, indent=1)
    except (json.JSONDecodeError, TypeError):
        pass
    return result


@app.post("/chat/stream")
async def chat_stream(inp: ChatIn):
    """SSE: 툴 호출을 실시간으로 내보내고 마지막에 답변 전송."""
    sid = inp.session_id or str(uuid.uuid4())

    async def gen():
        yield sse({"type": "session", "session_id": sid})
        calls, answer, t0 = [], "", time.time()
        config = {"configurable": {"thread_id": sid}}
        try:
            async for mode, chunk in _agent.astream(
                {"messages": [{"role": "user", "content": inp.message}]},
                config, stream_mode=["updates", "messages"],
            ):
                if mode == "messages":
                    # LLM 생성 중 토큰을 실시간 전송 (이게 체감 스트리밍의 핵심)
                    msg_chunk, _meta = chunk
                    text = getattr(msg_chunk, "content", "")
                    if not (isinstance(text, str) and text):
                        text = (getattr(msg_chunk, "additional_kwargs", {}) or {}).get(
                            "reasoning_content", "")
                    if isinstance(text, str) and text:
                        yield sse({"type": "token", "text": text})
                    continue
                for upd in chunk.values():
                    if not isinstance(upd, dict):
                        continue
                    msgs = upd.get("messages") or []
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    for m in msgs:
                        for c in getattr(m, "tool_calls", None) or []:
                            calls.append({"name": c["name"], "args": c["args"]})
                            yield sse({"type": "tool", "name": c["name"],
                                       "args": c["args"]})
                        if getattr(m, "type", "") == "tool":
                            result = m.content if isinstance(m.content, str) \
                                else json.dumps(m.content, ensure_ascii=False)
                            yield sse({"type": "tool_end",
                                       "name": getattr(m, "name", "") or "",
                                       "result": prettify_result(result)[:3000]})
                        if getattr(m, "type", "") == "ai" and m.content \
                                and not getattr(m, "tool_calls", None):
                            answer = m.content if isinstance(m.content, str) \
                                else json.dumps(m.content, ensure_ascii=False)
        except Exception as e:
            answer = answer or f"[오류] {e}"
            yield sse({"type": "error", "message": str(e)})
        log_turn(sid, inp.message, calls, answer)
        yield sse({"type": "answer", "text": answer,
                   "elapsed_sec": round(time.time() - t0, 1)})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/chat")
async def chat(inp: ChatIn):
    """비스트리밍 API (스크립트용). 멀티턴 기억 동일 적용."""
    sid = inp.session_id or str(uuid.uuid4())
    t0 = time.time()
    result = await _agent.ainvoke(
        {"messages": [{"role": "user", "content": inp.message}]},
        {"configurable": {"thread_id": sid}})
    calls = [{"name": c["name"], "args": c["args"]}
             for m in result["messages"]
             for c in (getattr(m, "tool_calls", None) or [])]
    answer = result["messages"][-1].content
    log_turn(sid, inp.message, calls, answer)
    return {"session_id": sid, "answer": answer, "tool_calls": calls,
            "elapsed_sec": round(time.time() - t0, 1)}


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/graph/data")
def graph_data():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, layer, name, fail_flag, fail_reason FROM nodes")
    nodes = [{"id": r[0], "layer": r[1], "name": r[2], "fail": r[3] == "Y",
              "fail_reason": r[4]} for r in cur.fetchall()]
    cur.execute("SELECT src, dst, raw_count FROM edges")
    edges = [{"src": r[0], "dst": r[1], "count": r[2]} for r in cur.fetchall()]
    con.close()
    return {"nodes": nodes, "edges": edges}


@app.get("/graph")
def graph_page():
    return FileResponse(ROOT / "app" / "graph.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/reload")
def reload_embeddings():
    """임베딩 백필 진행 중 행렬 갱신용 (서버 재시작 불필요)."""
    return {"loaded": load_matrix()}
