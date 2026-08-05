"""PoC 채팅 UI 서버 — 에이전트를 웹에서 테스트하고 세션을 Oracle 증거 테이블에 기록.

- 멀티턴 기억: Oracle 체크포인터(thread_id=세션id) — 복제본 공유·재시작 생존
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
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from tools.oracle_checkpointer import OracleSaver
from pydantic import BaseModel

from agent.agent import build_agent
from app import auth
from tools import config, model_registry
from tools.blog_search import DSN, PASSWORD, USER, load_matrix
from tools.session_ctx import current_session

app = FastAPI()
app.include_router(auth.router)  # SSO: /oidc/login·callback·logout, /me
_agents = {}          # model_name -> agent (모델별 캐시)
_saver = None         # 공유 checkpointer (같은 세션이 모델 바꿔도 기억 유지)
ADMIN_TOKEN = config.ADMIN_TOKEN


def check_admin(request: Request, token: str):
    """관리자 = SSO 관리자 역할(gsc-admin) 또는 X-Admin-Token(스크립트·비상용)."""
    if token != ADMIN_TOKEN and not auth.is_admin(request):
        raise HTTPException(403, "관리자 권한이 필요합니다 "
                                 "(SSO 관리자 역할 또는 X-Admin-Token)")


async def get_agent(model_name: str | None):
    name = model_name or model_registry.get_default("llm", None) or None
    if name not in _agents:
        _agents[name] = await build_agent(checkpointer=_saver, model_name=name)
    return _agents[name]


# 서버 전용 커넥션 풀 — log_turn(대화 턴마다)·stats(readiness probe 15초마다) 등이
# 매번 새로 접속하던 것을 재사용으로 전환. con.close()는 풀 반납으로 동작(실제 종료 아님).
_db_pool = oracledb.create_pool(user=USER, password=PASSWORD, dsn=DSN,
                                min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                                increment=config.ORACLE_POOL_INCREMENT)


def db():
    return _db_pool.acquire()


@app.on_event("startup")
async def startup():
    global _saver
    load_matrix()  # 임베딩 행렬 메모리 적재 (하이브리드 검색)
    _saver = OracleSaver()  # 멀티턴 기억 — Oracle 외부화 (복제본 공유·재시작 생존)
    await get_agent(None)   # 기본 LLM 예열
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
              user_id    VARCHAR2(64),   -- SSO 로그인 사용자 (재발 판정을 사용자 단위로)
              PRIMARY KEY (id, turn)
            )
        """)
        con.commit()
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SESSIONS' AND column_name = 'USER_ID'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE sessions ADD (user_id VARCHAR2(64))")
        con.commit()
    con.close()


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str
    model: str | None = None  # 사용자가 선택한 LLM (미지정 시 레지스트리 기본값)


def log_turn(sid: str, question: str, calls: list, answer: str,
             user: str | None = None):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT NVL(MAX(turn),0)+1 FROM sessions WHERE id = :1", [sid])
    turn = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sessions (id, turn, question, tool_calls, answer, user_id) "
        "VALUES (:1, :2, :3, :4, :5, :6)",
        [sid, turn, question, json.dumps(calls, ensure_ascii=False), answer, user])
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
async def chat_stream(inp: ChatIn, request: Request):
    """SSE: 툴 호출을 실시간으로 내보내고 마지막에 답변 전송."""
    u = auth.require_user(request)
    uid = (u or {}).get("user")
    sid = inp.session_id or str(uuid.uuid4())

    async def gen():
        current_session.set(sid)  # 도구(노출 기록)까지 세션 id 전파
        agent = await get_agent(inp.model)
        yield sse({"type": "session", "session_id": sid})
        calls, answer, t0 = [], "", time.time()
        config = {"configurable": {"thread_id": sid}}
        try:
            async for mode, chunk in agent.astream(
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
        log_turn(sid, inp.message, calls, answer, user=uid)
        yield sse({"type": "answer", "text": answer,
                   "elapsed_sec": round(time.time() - t0, 1)})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/chat")
async def chat(inp: ChatIn, request: Request):
    """비스트리밍 API (스크립트용). 멀티턴 기억 동일 적용."""
    u = auth.require_user(request)
    sid = inp.session_id or str(uuid.uuid4())
    t0 = time.time()
    current_session.set(sid)
    agent = await get_agent(inp.model)
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": inp.message}]},
            {"configurable": {"thread_id": sid}})
    except Exception as e:
        return {"session_id": sid, "answer": f"[모델 오류] {e}", "tool_calls": [],
                "elapsed_sec": round(time.time() - t0, 1), "error": True}
    calls = [{"name": c["name"], "args": c["args"]}
             for m in result["messages"]
             for c in (getattr(m, "tool_calls", None) or [])]
    answer = result["messages"][-1].content
    log_turn(sid, inp.message, calls, answer, user=(u or {}).get("user"))
    return {"session_id": sid, "answer": answer, "tool_calls": calls,
            "elapsed_sec": round(time.time() - t0, 1)}


@app.get("/sessions")
def list_sessions(request: Request):
    """내 대화 목록 — 사용자별 독립 (다른 사람 세션은 안 보임).

    AUTH_MODE=none(로컬)에선 user_id 없는 세션만 보인다 — 같은 규칙의 자연스러운 귀결.
    """
    u = auth.require_user(request)
    uid = (u or {}).get("user")
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT s1.id, s1.question, x.turns, x.last_ts
        FROM sessions s1
        JOIN (SELECT id, COUNT(*) AS turns, MAX(ts) AS last_ts FROM sessions
              WHERE (:u IS NULL AND user_id IS NULL) OR user_id = :u
              GROUP BY id) x ON x.id = s1.id
        WHERE s1.turn = 1
        ORDER BY x.last_ts DESC
        FETCH FIRST 30 ROWS ONLY""", {"u": uid})
    out = [{"id": r[0], "title": (r[1].read() if r[1] is not None else "")[:80],
            "turns": r[2], "last_ts": r[3].isoformat() if r[3] else None}
           for r in cur.fetchall()]
    con.close()
    return {"sessions": out}


@app.get("/sessions/{sid}")
def get_session(sid: str, request: Request):
    """세션 대화 복원 — 본인 것만 (이어하기는 같은 session_id로 /chat 호출하면
    Oracle 체크포인터가 기억을 이어준다)."""
    u = auth.require_user(request)
    uid = (u or {}).get("user")
    con = db()
    cur = con.cursor()
    cur.execute("""SELECT turn, question, answer, user_id FROM sessions
                   WHERE id = :1 ORDER BY turn""", [sid])
    rows = [(t, q.read() if q else "", a.read() if a else "", owner)
            for t, q, a, owner in cur.fetchall()]
    con.close()
    if not rows:
        raise HTTPException(404, "세션이 없습니다")
    if rows[0][3] != uid:
        raise HTTPException(403, "본인 세션만 볼 수 있습니다")
    return {"session_id": sid,
            "turns": [{"turn": t, "question": q, "answer": a} for t, q, a, _ in rows]}


@app.get("/static/shell.css")
def shell_css():
    return FileResponse(ROOT / "app" / "shell.css",
                        headers={"Cache-Control": "no-store"})


@app.get("/")
def index(request: Request):
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/graph/data")
def graph_data():
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT n.id, n.layer, n.name, n.fail_reason,
               (SELECT COUNT(DISTINCT ev.session_id) FROM node_evidence ev
                WHERE ev.node_id = n.id) AS ev_cnt,
               (SELECT COUNT(DISTINCT ev.session_id) FROM node_evidence ev
                JOIN sessions s ON s.id = ev.session_id AND s.turn = 1
                WHERE ev.node_id = n.id AND s.verdict = 'success') AS sc,
               (SELECT COUNT(DISTINCT ev.session_id) FROM node_evidence ev
                JOIN sessions s ON s.id = ev.session_id AND s.turn = 1
                WHERE ev.node_id = n.id AND s.verdict = 'fail') AS fc
        FROM nodes n""")
    nodes = [{"id": r[0], "layer": r[1], "name": r[2], "fail_reason": r[3],
              "uses": r[4], "success": r[5], "fail_cnt": r[6],
              "fail": r[6] > r[5]}  # 실패 우세만 빨강 (카운트 기준)
             for r in cur.fetchall()]
    cur.execute("SELECT src, dst, raw_count FROM edges")
    edges = [{"src": r[0], "dst": r[1], "count": r[2]} for r in cur.fetchall()]
    con.close()
    return {"nodes": nodes, "edges": edges}


@app.get("/graph")
def graph_page(request: Request):
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "graph.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/reload")
def reload_embeddings():
    """임베딩 백필 진행 중 행렬 갱신용 (서버 재시작 불필요)."""
    return {"loaded": load_matrix()}


@app.get("/models")
def models():
    """사용자용: 선택 가능한 LLM 목록 + 현재 임베딩(정보만)."""
    llms = [m for m in model_registry.list_models("llm") if m["enabled"]]
    return {"llm": llms, "embedding_in_use": config.EMBED_MODEL}  # 검색 경로 실사용값(.env)


@app.post("/admin/models/sync")
def admin_sync(request: Request, x_admin_token: str = Header(default="")):
    """관리자: 모델 서빙에서 목록 동기화(등록)."""
    check_admin(request, x_admin_token)
    return model_registry.sync_from_serving()


class SelectIn(BaseModel):
    kind: str   # llm | embedding | reranker
    name: str


@app.post("/admin/models/select")
def admin_select(inp: SelectIn, request: Request,
                 x_admin_token: str = Header(default="")):
    """관리자: 종류별 기본 모델 지정. 임베딩 교체는 전체 재백필 필요."""
    check_admin(request, x_admin_token)
    model_registry.set_default(inp.kind, inp.name)
    warn = None
    if inp.kind == "embedding":
        warn = ("임베딩 모델 변경됨 — 기존 벡터와 호환되지 않습니다. "
                "UPDATE blog_posts SET embedding=NULL 후 embed_corpus.py 재실행, "
                "그래프 dedup 임계값 재캘리브레이션 필요")
    return {"ok": True, "kind": inp.kind, "default": inp.name, "warning": warn}


class DomainIn(BaseModel):
    name: str
    tools: str          # 쉼표구분 도구명 — 이 도구를 쓴 세션이 이 도메인으로 분류됨
    priority: int = 100  # 낮을수록 먼저 대조. 최하순위가 폴백 도메인
    extract_hint: str = ""  # 도메인별 추출 지침 — 이 도메인 세션의 목표·접근법 추출 프롬프트에 주입


@app.get("/admin/domains")
def admin_domains(request: Request, x_admin_token: str = Header(default="")):
    """관리자: 1층 도메인 닫힌 목록 조회 (시드 테이블 domain_registry)."""
    check_admin(request, x_admin_token)
    from poc.graph_pipeline import ensure_domain_registry
    con = db()
    cur = con.cursor()
    ensure_domain_registry(cur)
    con.commit()
    cur.execute("""SELECT name, tools, priority, extract_hint
                   FROM domain_registry ORDER BY priority, name""")
    rows = [{"name": r[0], "tools": r[1], "priority": r[2], "extract_hint": r[3] or ""}
            for r in cur.fetchall()]
    con.close()
    return {"domains": rows}


@app.post("/admin/domains")
def admin_domain_add(inp: DomainIn, request: Request,
                     x_admin_token: str = Header(default="")):
    """관리자: 도메인 추가/수정 — 닫힌 1층 목록의 유일한 확장 통로 (사람 전용).

    다음 파이프라인 실행(야간 03:00 또는 수동)부터 신규 세션 분류에 반영된다.
    기존 세션 소급 재분류는 하지 않는다(안전 기본값). 삭제 API는 일부러 없음 —
    도메인 삭제·병합은 기존 노드 재배치가 필요한 신중한 작업이라 SQL로만.
    """
    check_admin(request, x_admin_token)
    if not inp.name.strip() or not inp.tools.strip():
        raise HTTPException(400, "name과 tools(쉼표구분)는 필수입니다")
    from poc.graph_pipeline import ensure_domain_registry
    con = db()
    cur = con.cursor()
    ensure_domain_registry(cur)
    cur.execute("""MERGE INTO domain_registry d USING dual ON (d.name = :n)
                   WHEN MATCHED THEN UPDATE SET tools = :t, priority = :p, extract_hint = :h
                   WHEN NOT MATCHED THEN INSERT (name, tools, priority, extract_hint)
                   VALUES (:n, :t, :p, :h)""",
                {"n": inp.name.strip(), "t": inp.tools.strip(), "p": inp.priority,
                 "h": inp.extract_hint.strip() or None})
    con.commit()
    con.close()
    return {"ok": True, "name": inp.name.strip(),
            "note": "다음 파이프라인 실행부터 신규 세션 분류에 반영 (소급 재분류 없음)"}


class SourceIn(BaseModel):
    source_name: str
    table_name: str      # 원천 테이블 (읽기 전용 — 우리는 SELECT만)
    id_column: str       # 고유 id 필드
    ts_column: str = ""  # 생성시간 필드 — 증분 워터마크 (빈값 = 전량 1회 소스)
    field_map: dict      # {역할: 컬럼} 역할=title|body|question|answer|meta
    content_kind: str = ""  # 문제해결/가이드 등 — 프롬프트 힌트
    enabled: bool = True


@app.get("/admin/sources")
def admin_sources(request: Request, x_admin_token: str = Header(default="")):
    """관리자: 구조화 원천 테이블 목록 (source_registry)."""
    check_admin(request, x_admin_token)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    rows = source_registry.list_sources(cur)
    con.commit()
    con.close()
    return {"sources": rows}


@app.post("/admin/sources")
def admin_source_add(inp: SourceIn, request: Request,
                     x_admin_token: str = Header(default="")):
    """관리자: 원천 테이블 등록/수정 — 테이블·컬럼 실존을 검증하고 저장.

    다음 적재 배치부터 반영. 원천 테이블은 읽기 전용(우리는 SELECT만)이고,
    삭제 API는 domain_registry와 같은 이유로 없음(enabled='N'으로 끄는 것까지만).
    """
    check_admin(request, x_admin_token)
    from tools import source_registry
    if not inp.source_name.strip() or not inp.table_name.strip() or not inp.id_column.strip():
        raise HTTPException(400, "source_name·table_name·id_column은 필수입니다")
    con = db()
    cur = con.cursor()
    source_registry.ensure(cur)
    err = source_registry.validate(cur, inp.table_name.strip(), inp.id_column.strip(),
                                   inp.ts_column.strip(), inp.field_map)
    if err:
        con.close()
        raise HTTPException(400, err)
    source_registry.upsert(cur, inp.source_name.strip(), inp.table_name.strip(),
                           inp.id_column.strip(), inp.ts_column.strip(),
                           inp.field_map, inp.content_kind.strip(), inp.enabled)
    con.commit()
    con.close()
    return {"ok": True, "source_name": inp.source_name.strip(),
            "note": "다음 적재 배치부터 반영 (원천 테이블은 읽기 전용)"}


@app.get("/admin/sources/tables")
def admin_source_tables(request: Request, x_admin_token: str = Header(default="")):
    """관리자: 접속 DB의 등록 후보 테이블 목록 (Oracle 내부·우리 테이블 제외)."""
    check_admin(request, x_admin_token)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    tables = source_registry.browse_tables(cur)
    con.close()
    return {"tables": tables}


@app.get("/admin/sources/tables/{tname}")
def admin_source_columns(tname: str, request: Request,
                         x_admin_token: str = Header(default="")):
    """관리자: 테이블의 컬럼 목록 — 등록 폼의 컬럼 선택용."""
    check_admin(request, x_admin_token)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    cols = source_registry.table_columns(cur, tname)
    con.close()
    if not cols:
        raise HTTPException(404, f"테이블이 없습니다: {tname}")
    return {"columns": [{"name": k, "type": v} for k, v in cols.items()]}


@app.get("/stats")
def stats():
    """헤더 상태칩용 현황."""
    con = db()
    cur = con.cursor()
    out = {}
    # posts = 통합 코퍼스 수 (전환 전이면 구 blog_posts로 폴백)
    for k, q in [("posts", "SELECT COUNT(*) FROM corpus_docs"),
                 ("nodes", "SELECT COUNT(*) FROM nodes"),
                 ("edges", "SELECT COUNT(*) FROM edges"),
                 ("sessions", "SELECT COUNT(DISTINCT id) FROM sessions")]:
        try:
            cur.execute(q)
            out[k] = cur.fetchone()[0]
        except oracledb.DatabaseError:
            out[k] = 0
    if not out["posts"]:
        try:
            cur.execute("SELECT COUNT(*) FROM blog_posts")
            out["posts"] = cur.fetchone()[0]
        except oracledb.DatabaseError:
            pass
    con.close()
    return out
