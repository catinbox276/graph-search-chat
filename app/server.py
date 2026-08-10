"""PoC 채팅 UI 서버 — 에이전트를 웹에서 테스트하고 세션을 Oracle 증거 테이블에 기록.

- 멀티턴 기억: Oracle 체크포인터(thread_id=세션id) — 복제본 공유·재시작 생존
- 실시간 진행 표시: /chat/stream SSE — 툴 호출 이벤트를 즉시 전송

usage: .venv/bin/uvicorn app.server:app --port 8500
접속: http://localhost:8500
"""
import json
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from tools.oracle_checkpointer import OracleSaver
from pydantic import BaseModel

from agent.agent import build_agent
from app import auth
from tools import config, model_registry, source_registry
from tools.blog_search import DSN, PASSWORD, USER, load_matrix
from tools.session_ctx import current_session

app = FastAPI()
app.include_router(auth.router)  # 자체 계정: /auth/login·signup·logout, /me
_agents = {}          # model_name -> agent (모델별 캐시)
_saver = None         # 공유 checkpointer (같은 세션이 모델 바꿔도 기억 유지)
def check_admin(request: Request):
    """관리자 = env 계정 또는 is_admin 부여 계정."""
    if not auth.is_admin(request):
        raise HTTPException(403, "관리자 권한이 필요합니다")


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
    con = db()
    cur = con.cursor()
    # 코퍼스 DDL을 배치보다 먼저 보장 — 새 DB에서 야간 적재 전에
    # 검색·드라이런이 먼저 와도 ORA-00942가 나지 않도록 (멱등)
    source_registry.ensure(cur)
    source_registry.ensure_corpus(cur)
    source_registry.ensure_corpus_chunks(cur)
    con.commit()
    load_matrix()  # 임베딩 행렬 메모리 적재 (하이브리드 검색)
    _saver = OracleSaver()  # 멀티턴 기억 — Oracle 외부화 (복제본 공유·재시작 생존)
    await get_agent(None)   # 기본 LLM 예열
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


class TopicCheckIn(BaseModel):
    session_id: str
    question: str


@app.post("/session/topic-check")
def topic_check(inp: TopicCheckIn, request: Request):
    """전송 직전 화제 단절 확인 — 직전 질문과 임베딩 비교 (야간 분할과 같은 임계값).

    shifted=true면 UI가 "새 대화로 시작할까요?" 확인 바를 띄운다. 판정 실패는
    조용히 false — 확인 바는 보조 장치라 검색·답변을 막으면 안 된다 (fail-open)."""
    u = auth.require_user(request)
    con = db()
    cur = con.cursor()
    cur.execute("""SELECT question FROM sessions
                   WHERE id = :sid
                     AND ((:u IS NULL AND user_id IS NULL) OR user_id = :u)
                   ORDER BY turn DESC FETCH FIRST 1 ROWS ONLY""",
                {"sid": inp.session_id, "u": u.get("user")})
    row = cur.fetchone()
    # CLOB은 커넥션이 살아 있을 때 읽어야 한다 (닫은 뒤 .read()는 오류)
    prev = (row[0].read() if hasattr(row[0], "read") else (row[0] or "")) if row else ""
    con.close()
    if not row:
        return {"shifted": False}
    try:
        cli, emb_name = model_registry.embedding_client()
        d = cli.embeddings.create(model=emb_name,
                                  input=[prev[:500], inp.question[:500]]).data
        a, b = d[0].embedding, d[1].embedding
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        sim = dot / (na * nb) if na and nb else 1.0
        return {"shifted": sim < config.SEG_SPLIT_SIM, "sim": round(sim, 3)}
    except Exception:
        return {"shifted": False}


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


def _source_items(refs: dict) -> list:
    """이 턴에 실제 검색·열람된 문서 id들의 제목·링크 조회 (footer용 — LLM 미개입).
    refs: {pid: '열람'|'검색'}. 열람 우선, 최대 8건."""
    if not refs:
        return []
    order = sorted(refs, key=lambda p: refs[p] != "열람")[:8]
    items = []
    try:
        con = db()
        cur = con.cursor()
        for pid in order:
            src, sep, sid_ = pid.partition(":")
            if not sep:  # 문서 id는 '소스명:원천id' 단일 형식
                continue
            cur.execute("""SELECT d.title,
                                  CASE WHEN NVL(r.url_enabled, 'Y') = 'Y' THEN d.url END
                           FROM corpus_docs d
                           JOIN source_registry r ON r.source_name = d.source_name
                           WHERE d.source_name = :1 AND d.src_id = :2""", [src, sid_])
            row = cur.fetchone()
            if row:
                # 원천 테이블 값이라 신뢰 불가 — http(s) 외 스킴은 링크로 내보내지 않음 (XSS)
                url = (row[1] or "").strip()
                if not url.lower().startswith(("http://", "https://")):
                    url = ""
                items.append({"id": pid, "title": row[0], "url": url,
                              "kind": refs[pid]})
        con.close()
    except Exception:
        pass  # footer는 부가 정보 — 실패해도 답변을 막지 않는다
    return items


@app.get("/doc/{pid}")
def doc_view(pid: str, request: Request):
    """참고 문서 사내 뷰 — 에이전트가 실제로 읽은 corpus 본문을 그대로 보여준다.
    원문 URL로 바로 보내지 않고 근거를 먼저 확인할 수 있게 (footer·각주 클릭)."""
    auth.require_user(request)
    con = db()
    cur = con.cursor()
    try:
        src, sep, sid_ = pid.partition(":")
        if not sep:  # 문서 id는 '소스명:원천id' 단일 형식
            raise HTTPException(404, f"문서를 찾을 수 없습니다: {pid}")
        cur.execute("""SELECT d.title, d.body, d.kind,
                              CASE WHEN NVL(r.url_enabled, 'Y') = 'Y' THEN d.url END
                       FROM corpus_docs d
                       JOIN source_registry r ON r.source_name = d.source_name
                       WHERE d.source_name = :1 AND d.src_id = :2""", [src, sid_])
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"문서를 찾을 수 없습니다: {pid}")
        source, kind = src, row[2] or ""
        body = row[1].read() if hasattr(row[1], "read") else (row[1] or "")
        url = (row[3] or "").strip()
        if not url.lower().startswith(("http://", "https://")):  # XSS — http(s)만
            url = ""
        return {"id": pid, "title": row[0], "body": body[:20000],
                "kind": kind, "source": source, "url": url}
    finally:
        con.close()


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
        refs = {}  # 이 턴의 참고 문서: pid -> '열람'|'검색' (footer용, 도구 기록 기반)
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
                            if c["name"] in ("read_doc", "read_blog_post") and c["args"].get("post_id"):
                                refs[str(c["args"]["post_id"])] = "열람"
                            yield sse({"type": "tool", "name": c["name"],
                                       "args": c["args"]})
                        if getattr(m, "type", "") == "tool":
                            result = m.content if isinstance(m.content, str) \
                                else json.dumps(m.content, ensure_ascii=False)
                            tname = getattr(m, "name", "") or ""
                            if tname.startswith("search_"):  # search_docs·search_{소스명} 공통
                                for pid in re.findall(r"(?m)^\[([^\]\n]+)\]", result):
                                    refs.setdefault(pid, "검색")
                            elif tname == "suggest_paths":  # 경로 제안의 근거 문서
                                for pid in re.findall(r"\[([^\[\]\s]+:[^\[\]\s]+)\]", result):
                                    refs.setdefault(pid, "경로 근거")
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
        if (items := _source_items(refs)):
            yield sse({"type": "sources", "items": items})
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
    """내 대화 목록 — 사용자별 독립 (다른 사람 세션은 안 보임)."""
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
    cur.execute("""SELECT turn, question, answer, user_id, tool_calls FROM sessions
                   WHERE id = :1 ORDER BY turn""", [sid])
    rows = [(t, q.read() if q else "", a.read() if a else "", owner,
             c.read() if c else "")
            for t, q, a, owner, c in cur.fetchall()]
    con.close()
    if not rows:
        raise HTTPException(404, "세션이 없습니다")
    if rows[0][3] != uid:
        raise HTTPException(403, "본인 세션만 볼 수 있습니다")

    def calls(raw):
        try:
            return json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []
    return {"session_id": sid,
            "turns": [{"turn": t, "question": q, "answer": a,
                       "tool_calls": calls(c)} for t, q, a, _, c in rows]}


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
               (SELECT COUNT(*) FROM node_evidence ev
                WHERE ev.node_id = n.id) AS ev_cnt,
               (SELECT COUNT(*) FROM node_evidence ev
                JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                WHERE ev.node_id = n.id AND ev.kind = 'session'
                  AND s.verdict = 'success') AS sc,
               (SELECT COUNT(*) FROM node_evidence ev
                JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                WHERE ev.node_id = n.id AND ev.kind = 'session'
                  AND s.verdict = 'fail') AS fc,
               (SELECT COUNT(*) FROM node_evidence ev
                WHERE ev.node_id = n.id AND ev.kind = 'doc') AS dc
        FROM nodes n""")
    nodes = [{"id": r[0], "layer": r[1], "name": r[2], "fail_reason": r[3],
              "uses": r[4], "success": r[5], "fail_cnt": r[6], "docs": r[7],
              "fail": r[6] > r[5]}  # 실패 우세만 빨강 (카운트 기준)
             for r in cur.fetchall()]
    cur.execute("SELECT src, dst, raw_count FROM edges")
    edges = [{"src": r[0], "dst": r[1], "count": r[2]} for r in cur.fetchall()]
    con.close()
    return {"nodes": nodes, "edges": edges}


@app.get("/graph/node/{nid}/evidence")
def graph_node_evidence(nid: str, request: Request):
    """노드의 출처 증거 — 어느 세션/문서에서 왔는지 (provenance 가시화)."""
    auth.require_user(request)
    con = db()
    cur = con.cursor()
    cur.execute("""SELECT ev.kind, ev.ref, s.verdict
                   FROM node_evidence ev
                   LEFT JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                   WHERE ev.node_id = :1
                   ORDER BY ev.kind, ev.ref
                   FETCH FIRST 30 ROWS ONLY""", [nid])
    rows = cur.fetchall()
    out = []
    for kind, ref, verdict in rows:
        item = {"kind": kind, "ref": ref, "verdict": verdict}
        if kind == "doc":  # 문서 증거는 제목까지 (ref = 소스명:원천id)
            src, _, sid_ = ref.partition(":")
            cur.execute("""SELECT title FROM corpus_docs
                           WHERE source_name = :1 AND src_id = :2""", [src, sid_])
            r = cur.fetchone()
            item["title"] = r[0] if r else None
        out.append(item)
    cur.execute("SELECT COUNT(*) FROM node_evidence WHERE node_id = :1", [nid])
    total = cur.fetchone()[0]
    con.close()
    return {"evidence": out, "total": total}


@app.get("/me/contributions")
def my_contributions(request: Request):
    """내 세션이 그래프에 만든 지식(2·3층 노드) — 사용자가 확인·수정·철회하는 재료.

    editable(표현 수정 가능) = 증거가 내 것 하나뿐인 노드 — 여럿이 기여한 공유
    노드의 문구를 한 사람이 바꾸면 남의 기여까지 바뀌므로 단독 기여만 허용."""
    u = auth.require_user(request)
    uid = u.get("user")
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT ev.node_id, n.layer, n.name, NVL(n.fail_flag,'N'), n.fail_reason,
               ev.ref, s.verdict, TO_CHAR(s.ts,'YYYY-MM-DD HH24:MI'), s.question,
               (SELECT COUNT(*) FROM suggestions g WHERE g.node_id = n.id) AS exposures,
               (SELECT COUNT(*) FROM node_evidence e2 WHERE e2.node_id = n.id) AS ev_total,
               (SELECT MAX(p.name) FROM edges e JOIN nodes p
                 ON p.id = e.src AND p.layer = 2
                 WHERE e.dst = n.id) AS parent_goal,
               (SELECT COUNT(*) FROM edges e4 JOIN nodes t4
                 ON t4.id = e4.dst AND t4.layer = 4
                 WHERE e4.src = n.id) AS tool_cnt
        FROM node_evidence ev
        JOIN nodes n ON n.id = ev.node_id AND n.layer IN (2, 3)
        JOIN sessions s ON s.id = ev.ref AND s.turn = 1
        WHERE ev.kind = 'session' AND s.user_id = :u
        ORDER BY s.ts DESC, n.layer
        FETCH FIRST 200 ROWS ONLY""", {"u": uid})
    items = []
    for r in cur.fetchall():
        q_ = r[8].read() if hasattr(r[8], "read") else (r[8] or "")
        items.append({"node_id": r[0], "layer": r[1], "name": r[2],
                      "fail": r[3] == "Y", "fail_reason": r[4],
                      "session_id": r[5], "verdict": r[6], "ts": r[7],
                      "question": q_[:200], "exposures": r[9],
                      "editable": r[10] == 1,
                      "parent_goal": r[11], "tool_cnt": r[12]})
    con.close()
    return {"items": items}


class ContribActIn(BaseModel):
    node_id: str
    action: str          # rename | retract | clear_fail
    name: str | None = None


@app.post("/me/contributions/act")
def contribution_act(inp: ContribActIn, request: Request):
    """내 기여 제어 — 사용자 제어=증폭기 원칙의 실행 지점.

    rename: 단독 기여 노드만 문구 교정 (+임베딩 재계산)
    retract: 이 노드에 대한 내 세션 증거 회수 (카운트는 조인으로 자동 감소,
             증거 0이 된 노드는 야간 유지보수가 흡수)
    clear_fail: 실패 표식 해제 — 기여자만 가능"""
    u = auth.require_user(request)
    uid = u.get("user")
    con = db()
    try:
        cur = con.cursor()
        # 소유 확인: 이 노드에 내 세션 증거가 있어야 함
        cur.execute("""SELECT COUNT(*) FROM node_evidence ev
                       JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                       WHERE ev.node_id = :n AND ev.kind = 'session'
                         AND s.user_id = :u""", {"n": inp.node_id, "u": uid})
        mine = cur.fetchone()[0]
        if not mine:
            raise HTTPException(403, "이 노드에 대한 본인 기여가 없습니다")
        if inp.action == "rename":
            name = (inp.name or "").strip()
            if not (2 <= len(name) <= 400):
                raise HTTPException(400, "문구는 2~400자여야 합니다")
            cur.execute("SELECT COUNT(*) FROM node_evidence WHERE node_id = :1",
                        [inp.node_id])
            if cur.fetchone()[0] != 1:
                raise HTTPException(409, "여럿이 기여한 노드는 문구를 바꿀 수 없습니다")
            emb = None
            try:
                cli, emb_name = model_registry.embedding_client()
                v = cli.embeddings.create(model=emb_name, input=name).data[0].embedding
                emb = json.dumps(v).encode()
            except Exception:
                pass  # 임베딩 실패해도 문구는 교정 — 벡터는 다음 병합 때 재계산 여지
            cur.execute("UPDATE nodes SET name = :1, embedding = :2 WHERE id = :3",
                        [name, emb, inp.node_id])
        elif inp.action == "retract":
            cur.execute("""DELETE FROM node_evidence
                           WHERE node_id = :n AND kind = 'session'
                             AND ref IN (SELECT id FROM sessions
                                         WHERE turn = 1 AND user_id = :u)""",
                        {"n": inp.node_id, "u": uid})
        elif inp.action == "clear_fail":
            cur.execute("""UPDATE nodes SET fail_flag = 'N', fail_reason = NULL
                           WHERE id = :1""", [inp.node_id])
        else:
            raise HTTPException(400, f"알 수 없는 액션: {inp.action}")
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@app.get("/contrib")
def contrib_page(request: Request):
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "contrib.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/graph")
def graph_page(request: Request):
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "graph.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/login")
def login_page():
    """로그인/회원가입 페이지 — 유일하게 가드 없는 화면."""
    return FileResponse(ROOT / "app" / "login.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/admin/users")
def admin_users(request: Request):
    """관리자: 계정 목록 (승인 대기 + 활성, 권한 표시)."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    auth.ensure_users(cur)
    cur.execute("""SELECT user_id, approved, NVL(is_admin, 'N'), created_at
                   FROM app_users ORDER BY approved, created_at DESC""")
    rows = [{"user_id": r[0], "approved": r[1] == "Y", "is_admin": r[2] == "Y",
             "created_at": r[3].isoformat() if r[3] else None}
            for r in cur.fetchall()]
    con.close()
    return {"users": rows}


class UserActIn(BaseModel):
    user_id: str
    action: str  # approve(승인) | admin_on | admin_off | delete(거절/삭제)


@app.post("/admin/users/act")
def admin_user_act(inp: UserActIn, request: Request):
    """관리자: 계정 승인 / 관리자 권한 부여·해제 / 삭제. 권한 변경은 재로그인 시 반영."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    auth.ensure_users(cur)
    uid = inp.user_id.strip()
    if inp.action == "approve":
        cur.execute("""UPDATE app_users SET approved = 'Y', approved_at = SYSTIMESTAMP
                       WHERE user_id = :1""", [uid])
    elif inp.action == "admin_on":
        cur.execute("UPDATE app_users SET is_admin = 'Y' WHERE user_id = :1", [uid])
    elif inp.action == "admin_off":
        cur.execute("UPDATE app_users SET is_admin = 'N' WHERE user_id = :1", [uid])
    elif inp.action == "delete":
        cur.execute("DELETE FROM app_users WHERE user_id = :1", [uid])
    else:
        con.close()
        raise HTTPException(400, "action은 approve/admin_on/admin_off/delete 중 하나")
    n = cur.rowcount
    con.commit()
    con.close()
    if not n:
        raise HTTPException(404, f"계정이 없습니다: {uid}")
    return {"ok": True}


@app.get("/admin")
def admin_page(request: Request):
    """관리 콘솔 페이지 — 도메인·소스·처리 현황·전처리 설정 (모달에서 분리).
    페이지는 로그인만 요구, 내용 API가 각자 관리자 권한을 검사한다."""
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "admin.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/reload")
def reload_embeddings():
    """임베딩 백필 진행 중 행렬 갱신용 (서버 재시작 불필요)."""
    return {"loaded": load_matrix()}


@app.get("/models")
def models():
    """사용자용: 선택 가능한 LLM 목록 + 현재 임베딩(정보만)."""
    llms = [m for m in model_registry.list_models("llm") if m["enabled"]]
    return {"llm": llms,
            "embedding_in_use": model_registry.embedding_endpoint()[1]}


@app.get("/admin/models/all")
def admin_models_all(request: Request):
    """관리자: 전체 모델 목록 (종류·주소·기본값·활성)."""
    check_admin(request)
    return {"models": model_registry.list_models(),
            "embedding_in_use": model_registry.embedding_endpoint()[1]}


class ModelAddIn(BaseModel):
    kind: str            # llm | embedding | reranker
    name: str            # served-model-name (호스트 /v1/models 값 그대로)
    base_url: str = ""   # 이 모델의 서빙 주소 — 빈값이면 역할별 .env(CHAT/EMBED/RERANK_URL)
    enabled: bool = True


@app.post("/admin/models/add")
def admin_model_add(inp: ModelAddIn, request: Request):
    """관리자: 모델 수동 등록/수정 (사내 vLLM처럼 sync가 못 닿는 호스트용)."""
    check_admin(request)
    if inp.base_url and not inp.base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "base_url은 http(s):// 주소여야 합니다")
    try:
        model_registry.add_model(inp.kind, inp.name.strip(), inp.base_url.strip(),
                                 inp.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _agents.clear()  # LLM 목록이 바뀌었을 수 있음
    return {"ok": True}


@app.post("/admin/models/sync")
def admin_sync(request: Request):
    """관리자: 모델 서빙에서 목록 동기화(등록)."""
    check_admin(request)
    return model_registry.sync_from_serving()


class SelectIn(BaseModel):
    kind: str   # llm | embedding | reranker
    name: str


@app.post("/admin/models/select")
def admin_select(inp: SelectIn, request: Request):
    """관리자: 종류별 기본 모델 지정. 임베딩 교체는 전체 재백필 필요."""
    check_admin(request)
    try:
        model_registry.set_default(inp.kind, inp.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    warn = None
    if inp.kind == "embedding":
        n = load_matrix()  # 새 모델 벡터만 로드 — 백필 전이면 0건 (lexical이 받침)
        warn = (f"임베딩 기본값 변경됨 — 검색·dedup·경로 진입점이 즉시 이 모델을 씁니다. "
                f"현재 이 모델의 청크 벡터 {n}건 로드 (백필 배치가 불일치분을 자동 재임베딩, "
                "재백필 중엔 lexical이 받침). nodes.embedding 재백필과 dedup 임계값 "
                "재캘리브레이션은 별도 필요")
    return {"ok": True, "kind": inp.kind, "default": inp.name, "warning": warn}


class DomainIn(BaseModel):
    name: str
    tools: str = ""     # 쉼표구분 도구명 — 이 도구를 쓴 세션이 이 도메인으로 분류됨 (scope=doc이면 불필요)
    priority: int = 100  # 낮을수록 먼저 대조. 최하순위가 폴백 도메인
    extract_hint: str = ""  # 도메인별 추출 지침 — 목표·접근법 추출 프롬프트에 주입 (대화·문서 공통)
    scope: str = "both"  # 사용 목적: both(대화+문서) | chat(대화 전용) | doc(문서 전용)


@app.get("/admin/domains")
def admin_domains(request: Request):
    """관리자: 1층 도메인 닫힌 목록 조회 (시드 테이블 domain_registry)."""
    check_admin(request)
    from poc.graph_pipeline import ensure_domain_registry
    con = db()
    cur = con.cursor()
    ensure_domain_registry(cur)
    con.commit()
    cur.execute("""SELECT name, tools, priority, extract_hint, NVL(scope, 'both')
                   FROM domain_registry ORDER BY priority, name""")
    rows = [{"name": r[0], "tools": r[1] or "", "priority": r[2],
             "extract_hint": r[3] or "", "scope": r[4]}
            for r in cur.fetchall()]
    con.close()
    return {"domains": rows}


@app.post("/admin/domains")
def admin_domain_add(inp: DomainIn, request: Request):
    """관리자: 도메인 추가/수정 — 닫힌 1층 목록의 유일한 확장 통로 (사람 전용).

    등록 때 사용 목적(scope)을 명시 선택한다: both(대화+문서)/chat(대화 전용)/
    doc(문서 전용). doc 도메인은 대화 분류·폴백에 안 끼고 소스(📚) 지정으로만 쓴다.
    다음 파이프라인 실행(야간)부터 반영되고, 기존 세션 소급 재분류는 하지 않는다
    (안전 기본값). 삭제 API는 일부러 없음 — 도메인 삭제·병합은 기존 노드 재배치가
    필요한 신중한 작업이라 SQL로만.
    """
    check_admin(request)
    if not inp.name.strip():
        raise HTTPException(400, "name은 필수입니다")
    scope = inp.scope.strip().lower() or "both"
    if scope not in ("both", "chat", "doc"):
        raise HTTPException(400, "scope는 both/chat/doc 중 하나입니다")
    if scope != "doc" and not inp.tools.strip():
        raise HTTPException(400, "대화 분류에 쓰는 도메인(both/chat)은 tools(쉼표구분)가 필요합니다")
    from poc.graph_pipeline import ensure_domain_registry
    con = db()
    cur = con.cursor()
    ensure_domain_registry(cur)
    cur.execute("""MERGE INTO domain_registry d USING dual ON (d.name = :n)
                   WHEN MATCHED THEN UPDATE SET tools = :t, priority = :p,
                        extract_hint = :h, scope = :s
                   WHEN NOT MATCHED THEN INSERT (name, tools, priority, extract_hint, scope)
                   VALUES (:n, :t, :p, :h, :s)""",
                {"n": inp.name.strip(), "t": inp.tools.strip() or None, "p": inp.priority,
                 "h": inp.extract_hint.strip() or None, "s": scope})
    con.commit()
    con.close()
    note = {"doc": "문서 전용 — 소스(📚)에 지정하면 문서 구조화에 사용 (대화 분류엔 안 낌)",
            "chat": "대화 전용 — 다음 파이프라인 실행부터 신규 세션 분류에 반영",
            "both": "대화+문서 — 세션 분류와 소스 문서 구조화 양쪽에 사용"}[scope]
    return {"ok": True, "name": inp.name.strip(), "scope": scope, "note": note}


class SourceIn(BaseModel):
    source_name: str
    table_name: str      # 원천 테이블 (읽기 전용 — 우리는 SELECT만)
    id_column: str       # 고유 id 필드
    ts_column: str = ""  # 생성시간 필드 — 증분 워터마크 (빈값 = 전량 1회 소스)
    field_map: dict      # {역할: 컬럼} 역할=title|body|question|answer|meta|url
    content_kind: str = ""  # 문제해결/가이드 등 — 프롬프트 힌트
    domain: str = ""     # 그래프 구조화 도메인 — 지정 시 doc_pipeline이 LLM 판정·구조화 (빈값=검색만)
    enabled: bool = True
    url_enabled: bool = True  # N이면 검색·출처·문서 뷰에서 원본 링크 숨김 (즉시 반영)


@app.get("/admin/sources")
def admin_sources(request: Request):
    """관리자: 구조화 원천 테이블 목록 (source_registry)."""
    check_admin(request)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    rows = source_registry.list_sources(cur)
    con.commit()
    con.close()
    return {"sources": rows}


@app.get("/admin/doc-status")
def admin_doc_status(request: Request):
    """관리자: 문서 구조화 진행 현황 — 도메인 지정 소스별 상태 카운트 (UI 프로그래스용)."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT r.source_name, r.domain,
               COUNT(d.src_id),
               SUM(CASE WHEN d.graph_status = 'done' THEN 1 ELSE 0 END),
               SUM(CASE WHEN d.graph_status = 'excluded' THEN 1 ELSE 0 END),
               SUM(CASE WHEN d.graph_status = 'error' THEN 1 ELSE 0 END),
               SUM(CASE WHEN d.graph_status IS NULL AND d.src_id IS NOT NULL
                        THEN 1 ELSE 0 END)
        FROM source_registry r
        LEFT JOIN corpus_docs d ON d.source_name = r.source_name
        WHERE r.domain IS NOT NULL
        GROUP BY r.source_name, r.domain ORDER BY r.domain, r.source_name""")
    rows = [{"source": r[0], "domain": r[1], "total": r[2] or 0, "done": r[3] or 0,
             "excluded": r[4] or 0, "error": r[5] or 0, "pending": r[6] or 0}
            for r in cur.fetchall()]
    con.close()
    return {"sources": rows}


@app.post("/admin/sources")
def admin_source_add(inp: SourceIn, request: Request):
    """관리자: 원천 테이블 등록/수정 — 테이블·컬럼 실존을 검증하고 저장.

    다음 적재 배치부터 반영. 원천 테이블은 읽기 전용(우리는 SELECT만)이고,
    삭제 API는 domain_registry와 같은 이유로 없음(enabled='N'으로 끄는 것까지만).
    """
    check_admin(request)
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
    domain = inp.domain.strip()
    if domain:  # 지정 시 닫힌 도메인 목록에 실존 + 문서 용도(both/doc)여야 함
        from poc.graph_pipeline import ensure_domain_registry
        ensure_domain_registry(cur)
        cur.execute("SELECT NVL(scope, 'both') FROM domain_registry WHERE name = :1",
                    [domain])
        r = cur.fetchone()
        if not r:
            con.close()
            raise HTTPException(400, f"등록되지 않은 도메인: {domain} (⚙ 관리에서 먼저 추가)")
        if r[0] == "chat":
            con.close()
            raise HTTPException(400, f"도메인 '{domain}'은 대화 전용입니다 — "
                                     "문서 구조화에 쓰려면 용도를 '대화+문서'나 '문서 전용'으로")
    source_registry.upsert(cur, inp.source_name.strip(), inp.table_name.strip(),
                           inp.id_column.strip(), inp.ts_column.strip(),
                           inp.field_map, inp.content_kind.strip(), inp.enabled,
                           domain=domain, url_enabled=inp.url_enabled)
    con.commit()
    con.close()
    return {"ok": True, "source_name": inp.source_name.strip(),
            "note": "다음 적재 배치부터 반영 (원천 테이블은 읽기 전용)"}


@app.get("/admin/sources/tables")
def admin_source_tables(request: Request):
    """관리자: 접속 DB의 등록 후보 테이블 목록 (Oracle 내부·우리 테이블 제외)."""
    check_admin(request)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    tables = source_registry.browse_tables(cur)
    con.close()
    return {"tables": tables}


@app.get("/admin/sources/tables/{tname}")
def admin_source_columns(tname: str, request: Request):
    """관리자: 테이블의 컬럼 목록 — 등록 폼의 컬럼 선택용."""
    check_admin(request)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    cols = source_registry.table_columns(cur, tname)
    con.close()
    if not cols:
        raise HTTPException(404, f"테이블이 없습니다: {tname}")
    return {"columns": [{"name": k, "type": v} for k, v in cols.items()]}


@app.get("/admin/pipeline-settings")
def admin_pipeline_settings(request: Request):
    """관리자: 전처리(문서 구조화) 운영 설정 — 효과값 반환 (DB 없으면 .env 기본값)."""
    check_admin(request)
    from tools import settings
    con = db()
    cur = con.cursor()
    st = settings.get_all(cur)
    con.commit()
    con.close()
    return {"doc_extract_limit": settings.get_int(st, "doc_extract_limit",
                                                  config.DOC_EXTRACT_LIMIT),
            "doc_concurrency": settings.get_int(st, "doc_concurrency",
                                                config.DOC_CONCURRENCY),
            "doc_body_chars": settings.get_int(st, "doc_body_chars",
                                               config.DOC_BODY_CHARS),
            "doc_pack_tokens": settings.get_int(st, "doc_pack_tokens",
                                                config.DOC_PACK_TOKENS),
            "doc_no_think": settings.get_int(st, "doc_no_think", config.DOC_NO_THINK),
            "doc_extract_model": st.get("doc_extract_model") or "",
            "chunk_chars": settings.get_int(st, "chunk_chars", config.CHUNK_CHARS),
            "chunk_overlap": settings.get_int(st, "chunk_overlap", config.CHUNK_OVERLAP),
            "overridden": sorted(st.keys())}


class PipelineSettingsIn(BaseModel):
    doc_extract_limit: str = ""   # 빈값 = 기본값 복귀 (문자열로 받아 검증)
    doc_concurrency: str = ""
    doc_body_chars: str = ""
    doc_pack_tokens: str = ""     # 0=1건씩 / N=입력 N토큰 예산으로 묶음 판정
    doc_no_think: str = ""        # 1=추론(생각) 출력 끔 (기본) / 0=켬
    doc_extract_model: str = ""   # 빈값 = 대화 모델 사용
    chunk_chars: str = ""         # 청크 크기(자)
    chunk_overlap: str = ""       # 인접 청크 겹침(자)


@app.post("/admin/pipeline-settings")
def admin_pipeline_settings_set(inp: PipelineSettingsIn, request: Request):
    """관리자: 전처리 설정 저장 — 다음 배치 실행부터 반영 (재배포 불필요)."""
    check_admin(request)
    from tools import settings
    vals = {}
    for key, raw, lo, hi in (("doc_extract_limit", inp.doc_extract_limit, 1, 100000),
                             ("doc_concurrency", inp.doc_concurrency, 1, 256),
                             ("doc_body_chars", inp.doc_body_chars, 200, 20000),
                             ("doc_pack_tokens", inp.doc_pack_tokens, 0, 30000),
                             ("doc_no_think", inp.doc_no_think, 0, 1),
                             ("chunk_chars", inp.chunk_chars, 200, 8000),
                             ("chunk_overlap", inp.chunk_overlap, 0, 2000)):
        raw = raw.strip()
        if raw:
            try:
                v = int(raw)
            except ValueError:
                raise HTTPException(400, f"{key}는 정수여야 합니다")
            if not lo <= v <= hi:
                raise HTTPException(400, f"{key}는 {lo}~{hi} 범위여야 합니다")
        vals[key] = raw
    vals["doc_extract_model"] = inp.doc_extract_model.strip()
    con = db()
    cur = con.cursor()
    settings.set_many(cur, vals)
    con.commit()
    con.close()
    return {"ok": True, "note": "다음 전처리 배치 실행부터 반영 (빈값은 기본값 복귀)"}


@app.get("/admin/mcp")
def admin_mcp_list(request: Request):
    """관리자: 등록된 MCP 서버 목록."""
    check_admin(request)
    from tools import mcp_registry
    return {"servers": mcp_registry.list_servers()}


class McpIn(BaseModel):
    name: str
    transport: str = "streamable_http"  # streamable_http | sse | stdio
    url: str = ""       # http 계열: MCP 엔드포인트 주소
    command: str = ""   # stdio: 실행 파일
    enabled: bool = True


@app.post("/admin/mcp")
def admin_mcp_upsert(inp: McpIn, request: Request):
    """관리자: MCP 서버 등록/수정 — 저장 즉시 다음 질문부터 도구가 조립된다."""
    check_admin(request)
    if not inp.name.strip():
        raise HTTPException(400, "name은 필수입니다")
    from tools import mcp_registry
    try:
        mcp_registry.upsert(inp.name, inp.transport, inp.url, inp.command, inp.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _agents.clear()  # 다음 질문부터 새 MCP 구성으로 조립
    return {"ok": True, "note": "다음 질문부터 반영 (에이전트 재조립)"}


@app.get("/admin/agent-settings")
async def admin_agent_settings_get(request: Request):
    """관리자: 에이전트 전역 설정 조회 — 시스템 프롬프트·MCP 사용·도구별 활성."""
    check_admin(request)
    from agent.agent import SYSTEM_PROMPT, discover_tools
    from tools import settings
    con = db()
    st = settings.get_all(con.cursor())
    con.commit()
    con.close()
    return {"system_prompt": (st.get("agent_system_prompt") or ""),
            "default_prompt": SYSTEM_PROMPT,
            "mcp_enabled": st.get("agent_mcp_enabled", "1") != "0",
            "disabled_tools": [t.strip() for t in
                               (st.get("agent_disabled_tools") or "").split(",")
                               if t.strip()],
            "tools": await discover_tools()}


class AgentSettingsIn(BaseModel):
    system_prompt: str = ""      # 빈값 = 코드 기본 프롬프트 사용
    mcp_enabled: bool = True     # DataHub MCP 전역 on/off
    disabled_tools: list[str] = []  # 비활성 도구 이름 목록 (builtin·MCP 공통)


@app.post("/admin/agent-settings")
def admin_agent_settings_set(inp: AgentSettingsIn, request: Request):
    """관리자: 에이전트 전역 설정 저장 — 캐시를 비워 다음 질문부터 재조립."""
    check_admin(request)
    from tools import settings
    if len(inp.system_prompt) > 8000:
        raise HTTPException(400, "시스템 프롬프트는 8000자 이내여야 합니다")
    con = db()
    cur = con.cursor()
    settings.set_many(cur, {
        "agent_system_prompt": inp.system_prompt.strip(),
        "agent_mcp_enabled": "" if inp.mcp_enabled else "0",
        "agent_disabled_tools": ",".join(
            t.strip() for t in inp.disabled_tools if t.strip()),
    })
    con.commit()
    con.close()
    _agents.clear()  # 모델별 에이전트 캐시 무효화 — 다음 요청이 새 설정으로 조립
    return {"ok": True, "note": "다음 질문부터 반영 (에이전트 재조립)"}


class ReprocessIn(BaseModel):
    mode: str  # errors = 실패만 재시도 | reset = 소스 전체 초기화(그래프 증거 회수 포함)


@app.post("/admin/sources/{sname}/reprocess")
def admin_source_reprocess(sname: str, inp: ReprocessIn, request: Request):
    """관리자: 소스 재처리 준비.

    errors: error 상태만 미처리로 되돌림 (다음 배치가 재시도)
    reset : 소스 전체 초기화 — 이 소스의 문서가 그래프에 올린 기여(엣지 +1, 증거)를
            먼저 회수한 뒤 상태를 리셋한다. 그냥 리셋하면 재처리 때 이중 카운트되기
            때문 (재발 소급 취소와 같은 원리). 지침·모델 변경 후 재구조화용.
    """
    check_admin(request)
    con = db()
    cur = con.cursor()
    if inp.mode == "errors":
        cur.execute("""UPDATE corpus_docs SET graph_status = NULL, graph_note = NULL
                       WHERE source_name = :1 AND graph_status = 'error'""", [sname])
        n = cur.rowcount
        con.commit()
        con.close()
        return {"ok": True, "reset": n, "note": "error 문서를 미처리로 — 다음 배치가 재시도"}
    if inp.mode != "reset":
        con.close()
        raise HTTPException(400, "mode는 errors 또는 reset")
    n, retracted = _reset_source(cur, sname)
    con.commit()
    con.close()
    return {"ok": True, "reset": n, "evidence_retracted": retracted,
            "note": "그래프 기여 회수 완료 — 다음 배치가 처음부터 재구조화 "
                    "(고아 노드는 야간 유지보수가 정리)"}


def _reset_source(cur, sname: str):
    """소스 1개의 그래프 기여(엣지 +1, 증거) 회수 후 문서 상태 리셋. commit은 호출자가."""
    # 증거 회수: 문서 ref마다 그 문서가 만든 노드 집합 내부 엣지에서 기여 -1
    cur.execute("""SELECT DISTINCT ref FROM node_evidence
                   WHERE kind = 'doc' AND ref LIKE :1""", [f"{sname}:%"])
    refs = [r[0] for r in cur.fetchall()]
    for ref in refs:
        cur.execute("""SELECT node_id FROM node_evidence
                       WHERE kind = 'doc' AND ref = :1""", [ref])
        nids = [r[0] for r in cur.fetchall()]
        for j in range(0, len(nids), 100):
            chunk = nids[j:j + 100]
            src_marks = ",".join(f":s{k}" for k in range(len(chunk)))
            dst_marks = ",".join(f":d{k}" for k in range(len(chunk)))
            binds = {f"s{k}": v for k, v in enumerate(chunk)}
            binds.update({f"d{k}": v for k, v in enumerate(chunk)})
            cur.execute(
                f"""UPDATE edges SET raw_count = GREATEST(raw_count - 1, 0),
                                     weight = GREATEST(weight - 1, 0)
                    WHERE src IN ({src_marks}) AND dst IN ({dst_marks})""", binds)
        cur.execute("DELETE FROM node_evidence WHERE kind = 'doc' AND ref = :1", [ref])
    cur.execute("""UPDATE corpus_docs SET graph_status = NULL, graph_note = NULL
                   WHERE source_name = :1 AND graph_status IS NOT NULL""", [sname])
    return cur.rowcount, len(refs)


@app.post("/admin/domains/{dname}/reset")
def admin_domain_reset(dname: str, request: Request):
    """관리자: 도메인 초기화 — 이 도메인에 물린 모든 소스의 문서 구조화를 회수·리셋.
    대화 세션 기여는 건드리지 않는다 (문서 쪽만)."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    cur.execute("SELECT source_name FROM source_registry WHERE domain = :1", [dname])
    names = [r[0] for r in cur.fetchall()]
    if not names:
        con.close()
        raise HTTPException(404, f"도메인 '{dname}'에 지정된 소스가 없습니다")
    per = {s: _reset_source(cur, s) for s in names}
    con.commit()
    con.close()
    return {"ok": True, "sources": {s: {"reset": n, "evidence_retracted": r}
                                    for s, (n, r) in per.items()},
            "note": "다음 배치가 처음부터 재구조화 (고아 노드는 야간 유지보수가 정리)"}


@app.post("/admin/reset-all-docs")
def admin_reset_all_docs(request: Request):
    """관리자: 전체 초기화 — 도메인 지정된 모든 소스의 문서 구조화를 회수·리셋."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    cur.execute("SELECT source_name FROM source_registry WHERE domain IS NOT NULL")
    names = [r[0] for r in cur.fetchall()]
    per = {s: _reset_source(cur, s) for s in names}
    con.commit()
    con.close()
    return {"ok": True, "sources": {s: {"reset": n, "evidence_retracted": r}
                                    for s, (n, r) in per.items()},
            "note": "다음 배치가 처음부터 재구조화 (고아 노드는 야간 유지보수가 정리)"}


class DryrunIn(BaseModel):
    n: int = 3  # 판정해볼 문서 수 (최대 5 — 그래프에 반영하지 않음)


@app.post("/admin/sources/{sname}/dryrun")
def admin_source_dryrun(sname: str, inp: DryrunIn, request: Request):
    """관리자: 드라이런 — 미처리 문서 N건을 판정만 해보고 결과를 보여준다.

    그래프·상태에 아무것도 쓰지 않는다. 새 소스·새 추출 지침을 튜닝할 때
    'excluded가 얼마나 나오나'를 배치 전에 확인하는 용도.
    """
    check_admin(request)
    n = max(1, min(inp.n, 5))
    con = db()
    cur = con.cursor()
    cur.execute("""SELECT s.domain, NVL(d.extract_hint, ' ')
                   FROM source_registry s
                   JOIN domain_registry d ON d.name = s.domain
                   WHERE s.source_name = :1 AND s.domain IS NOT NULL""", [sname])
    r = cur.fetchone()
    if not r:
        con.close()
        raise HTTPException(400, "이 소스에 그래프 도메인이 지정되어 있지 않습니다")
    domain, hint = r[0], r[1]
    cur.execute("""SELECT src_id, NVL(title, ' '), NVL(kind, ' '), body
                   FROM corpus_docs
                   WHERE source_name = :1 AND graph_status IS NULL
                   FETCH FIRST :2 ROWS ONLY""", [sname, n])
    docs = [(row[0], row[1], row[2],
             row[3].read() if hasattr(row[3], "read") else (row[3] or ""))
            for row in cur.fetchall()]
    from tools import settings
    st = settings.get_all(cur)
    con.close()
    if not docs:
        return {"domain": domain, "results": [], "note": "미처리 문서가 없습니다"}
    from poc.doc_pipeline import judge_doc
    body_chars = settings.get_int(st, "doc_body_chars", config.DOC_BODY_CHARS)
    model = (st.get("doc_extract_model") or "").strip()
    out = []
    for src_id, title, kind, body in docs:
        j = judge_doc(domain, hint, kind, title, body,
                      model=model, body_chars=body_chars)
        out.append({"src_id": src_id, "title": title.strip()[:120],
                    "fits": bool(j.get("fits")), "reason": j.get("reason") or
                    j.get("_error") or "파싱 실패",
                    "goal": j.get("goal") or "", "approach": j.get("approach") or ""})
    return {"domain": domain, "results": out,
            "note": "판정만 수행 — 그래프·상태에 반영 안 됨"}


@app.get("/stats")
def stats():
    """헤더 상태칩용 현황."""
    con = db()
    cur = con.cursor()
    out = {}
    for k, q in [("posts", "SELECT COUNT(*) FROM corpus_docs"),
                 ("nodes", "SELECT COUNT(*) FROM nodes"),
                 ("edges", "SELECT COUNT(*) FROM edges"),
                 ("sessions", "SELECT COUNT(DISTINCT id) FROM sessions")]:
        try:
            cur.execute(q)
            out[k] = cur.fetchone()[0]
        except oracledb.DatabaseError:
            out[k] = 0
    con.close()
    return out
