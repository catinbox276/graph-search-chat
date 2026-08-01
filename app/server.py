"""PoC 채팅 UI 서버 — 에이전트를 웹에서 테스트하고 세션을 Oracle 증거 테이블에 기록.

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
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent.agent import build_agent
from tools.blog_search import DSN, PASSWORD, USER

app = FastAPI()
_agent = None


def db():
    return oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)


@app.on_event("startup")
async def startup():
    global _agent
    _agent = await build_agent()
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM user_tables WHERE table_name = 'SESSIONS'
    """)
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


@app.post("/chat")
async def chat(inp: ChatIn):
    sid = inp.session_id or str(uuid.uuid4())
    t0 = time.time()
    result = await _agent.ainvoke(
        {"messages": [{"role": "user", "content": inp.message}]}
    )
    calls = [
        {"name": c["name"], "args": c["args"]}
        for m in result["messages"]
        for c in (getattr(m, "tool_calls", None) or [])
    ]
    answer = result["messages"][-1].content
    con = db()
    cur = con.cursor()
    cur.execute("SELECT NVL(MAX(turn),0)+1 FROM sessions WHERE id = :1", [sid])
    turn = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sessions (id, turn, question, tool_calls, answer) "
        "VALUES (:1, :2, :3, :4, :5)",
        [sid, turn, inp.message, json.dumps(calls, ensure_ascii=False), answer],
    )
    con.commit()
    con.close()
    return {"session_id": sid, "answer": answer, "tool_calls": calls,
            "elapsed_sec": round(time.time() - t0, 1)}


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "index.html")
