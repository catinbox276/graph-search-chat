"""채팅 — SSE 스트리밍·세션 기록·화제 분기 확인·문서 뷰·모델 목록."""
import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from web import auth
from web.deps import db, get_agent, log_turn, sse
from core import config, model_registry
from core.session_ctx import current_session

router = APIRouter()


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str
    model: str | None = None  # 사용자가 선택한 LLM (미지정 시 레지스트리 기본값)



class TopicCheckIn(BaseModel):
    session_id: str
    question: str


@router.post("/session/topic-check")
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



@router.get("/doc/{pid}")
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



def _check_session_owner(sid: str | None, uid: str | None):
    """이어하기 세션의 소유 검사 — 남의 session_id로 기억·기록에 올라타는 것(IDOR) 차단."""
    if not sid:
        return
    con = db()
    cur = con.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE id = :1 AND turn = 1", [sid])
    row = cur.fetchone()
    con.close()
    if row and row[0] != uid:
        raise HTTPException(403, "본인 세션만 이어할 수 있습니다")


@router.post("/chat/stream")
async def chat_stream(inp: ChatIn, request: Request):
    """SSE: 툴 호출을 실시간으로 내보내고 마지막에 답변 전송."""
    u = auth.require_user(request)
    uid = (u or {}).get("user")
    _check_session_owner(inp.session_id, uid)
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
                            from core import events
                            args_json = json.dumps(c["args"], ensure_ascii=False)
                            events.log("tool", source=c["name"], actor=uid, ref=sid,
                                       status="call", summary=args_json[:300],
                                       detail=args_json)
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
                            from core import events
                            events.log("tool", source=tname, actor=uid, ref=sid,
                                       status="result", summary=result[:300],
                                       detail=result)
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


@router.post("/chat")
async def chat(inp: ChatIn, request: Request):
    """비스트리밍 API (스크립트용). 멀티턴 기억 동일 적용."""
    u = auth.require_user(request)
    _check_session_owner(inp.session_id, (u or {}).get("user"))
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



@router.get("/sessions")
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


@router.get("/sessions/{sid}")
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



@router.get("/models")
def models():
    """사용자용: 선택 가능한 LLM 목록 + 현재 임베딩(정보만)."""
    llms = [m for m in model_registry.list_models("llm") if m["enabled"]]
    return {"llm": llms,
            "embedding_in_use": model_registry.embedding_endpoint()[1]}

