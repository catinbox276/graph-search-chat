"""앱 조립·기동 — 엔드포인트는 전부 web/routers/에 (여기 추가 금지).

- 멀티턴 기억: Oracle 체크포인터(thread_id=세션id) — 복제본 공유·재시작 생존
- 기동 순서: 스키마 보장(init_schema) → 임베딩 행렬 적재 → 에이전트 예열

usage: .venv/bin/uvicorn web.server:app --port 8500
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import time
import traceback

import oracledb
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from web import auth, deps
from web.routers import (accounts, admin_events, admin_models, admin_sources,
                         chat, contrib, graph, pages)
from core import db as orm_db, events
from ingestion import source_registry
from search.corpus_search import reload_index
from core.oracle_checkpointer import OracleSaver

app = FastAPI()
app.include_router(auth.router)           # 로그인·가입·로그아웃·/me
app.include_router(chat.router)           # 채팅·세션·문서 뷰
app.include_router(pages.router)          # 페이지 서빙
app.include_router(graph.router)          # 그래프 데이터·증거
app.include_router(contrib.router)        # 내 기여
app.include_router(accounts.router)       # 계정 관리
app.include_router(admin_models.router)   # 모델·MCP·에이전트 설정
app.include_router(admin_sources.router)  # 도메인·소스·전처리 운영
app.include_router(admin_events.router)   # 활동 로그 조회

# 하위 호환: 일부 모듈이 server.db / server.log_turn을 참조
db = deps.db
log_turn = deps.log_turn

import json as _json
import re as _re

# 활동 로그에서 제외 — 정적 파일·readiness 프로브(15초마다 노이즈)
_LOG_SKIP = ("/static", "/favicon.ico", "/stats")
# 요청 본문(detail)을 통째로 남기지 않을 경로 — 자격증명 계열 (대소문자 무관)
_BODY_SKIP = ("/auth", "/login", "/register", "/password", "/token", "/oauth", "/apikey")
# SSE 스트리밍 경로: 본문 캡처(_receive 오버라이드) 금지.
# BaseHTTPMiddleware가 스트리밍 응답의 disconnect를 감시할 때 오버라이드된 _receive가
# http.request를 반복 반환하면 "Unexpected message received: http.request"로 터진다.
# (nginx는 요청버퍼링으로 가려졌으나 traefik(버퍼링 없음)에서 표면화 — 프록시 무관 근본수정)
_STREAM_SKIP = ("/chat/stream",)
# 이 이름의 JSON 키는 값을 가린다 — 진짜 비밀값만 (message/content 등 사용자 데이터는 유지)
_SECRET_KEY = _re.compile(
    r"(?i)(pass|pw|secret|token|api[_-]?key|authorization|bearer|pin|otp|ssn|"
    r"credit|card|private[_-]?key)")


def _redact(text: str) -> str:
    """요청 본문에서 비밀 키의 값만 [REDACTED]로 — 절단 없이 전문 저장(2026-08-12 정책).
    detail은 CLOB이라 길이 제한 불필요. 절대 예외를 던지지 않는다(로깅이 앱을 못 죽이게)."""
    try:
        def walk(v):
            if isinstance(v, dict):
                return {k: ("[REDACTED]" if _SECRET_KEY.search(k) else walk(x))
                        for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v
        return _json.dumps(walk(_json.loads(text)), ensure_ascii=False)
    except Exception:
        return text


@app.middleware("http")
async def activity_log(request: Request, call_next):
    """모든 요청(정상·비정상)을 app_events에 기록 + JSON 요청 본문을 detail에.
    SSE는 스트리밍 시작만 잡힌다. 민감 경로(_BODY_SKIP)는 본문 제외."""
    t0 = time.time()
    p = request.url.path
    body_text = ""
    pl = p.lower()
    want_body = (request.method in ("POST", "PUT", "PATCH")
                 and "application/json" in request.headers.get("content-type", "")
                 and not any(x in pl for x in _BODY_SKIP)
                 and p not in _STREAM_SKIP           # SSE는 본문 캡처 금지(스트리밍 충돌)
                 and not any(p.startswith(x) for x in _LOG_SKIP))
    if want_body:
        raw = await request.body()  # 읽은 뒤 재주입 — 다운스트림 핸들러가 다시 읽게

        async def _receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        request._receive = _receive
        body_text = _redact(raw.decode("utf-8", "replace"))

    resp = await call_next(request)
    auth.refresh_cookie(request, resp)  # 슬라이딩 세션 — 사용 중이면 만료를 밀어냄
    if not any(p.startswith(x) for x in _LOG_SKIP):
        # 응답 본문도 전문 저장 (2026-08-12 정책) — JSON 응답만, SSE·자격증명 경로·
        # 활동 로그 조회 자체(로그가 로그를 낳는 자기증폭)는 제외
        resp_text = ""
        if (p not in _STREAM_SKIP and not p.startswith("/admin/events")
                and not any(x in pl for x in _BODY_SKIP)
                and "application/json" in resp.headers.get("content-type", "")):
            chunks = [c async for c in resp.body_iterator]

            async def _replay(cs=chunks):
                for c in cs:
                    yield c
            resp.body_iterator = _replay()
            resp_text = _redact(b"".join(chunks).decode("utf-8", "replace"))
        try:
            actor = (auth.current_user(request) or {}).get("user")
        except Exception:
            actor = None
        detail = ""
        if body_text:
            detail = "▶ 요청\n" + body_text
        if resp_text:
            detail += ("\n\n" if detail else "") + "◀ 응답\n" + resp_text
        events.log("request", source=p, actor=actor, status=resp.status_code,
                   level=("error" if resp.status_code >= 500 else
                          "warn" if resp.status_code >= 400 else "info"),
                   duration_ms=int((time.time() - t0) * 1000),
                   summary=f"{request.method} {p}", detail=(detail or None))
    return resp


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Pydantic 검증 실패 → 사람이 읽는 detail 문자열로 (UI가 detail을 그대로 표시)."""
    msgs = "; ".join(e.get("msg", "").removeprefix("Value error, ")
                     for e in exc.errors()) or "요청 형식이 올바르지 않습니다"
    return JSONResponse({"detail": msgs}, status_code=422)


@app.exception_handler(Exception)
async def log_unhandled(request: Request, exc: Exception):
    """미처리 예외 → error 이벤트(스택트레이스 포함) 기록 후 500 반환."""
    try:
        actor = (auth.current_user(request) or {}).get("user")
    except Exception:
        actor = None
    events.log("error", source=request.url.path, actor=actor, level="error",
               status=500, summary=f"{type(exc).__name__}: {str(exc)[:200]}",
               detail=traceback.format_exc())
    return JSONResponse({"detail": "서버 오류가 발생했습니다"}, status_code=500)


@app.on_event("startup")
async def startup():
    # 전 테이블 생성(models.py 선언, 멱등) + Oracle Text 인덱스 + 시드
    orm_db.init_schema()
    con = deps.db()
    cur = con.cursor()
    # 구버전 DB 컬럼 추가(ALTER) 마이그레이션은 기존 ensure가 계속 담당
    source_registry.ensure(cur)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'SESSIONS' AND column_name = 'USER_ID'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE sessions ADD (user_id VARCHAR2(64))")
    con.commit()
    con.close()
    reload_index()               # SQLite 인메모리 검색 인덱스 빌드 (Oracle→:memory:)
    deps.set_saver(OracleSaver())  # 멀티턴 기억 — Oracle 외부화
    await deps.get_agent(None)   # 기본 LLM 예열
    _probe_models_bg()           # 설정된 모델 서빙 연결 점검 (비블로킹 — 서버 기동은 안 막음)


def _probe_models_bg():
    """기동 시 .env/레지스트리에 설정된 모델 서빙을 점검하고 결과를 활동 로그·콘솔에 남긴다.
    별도 스레드 — 서빙이 느리거나 죽어도 서버 기동을 지연/차단하지 않는다."""
    import threading

    def _run():
        from core import model_registry, events
        for rec in model_registry.probe_serving():
            good = rec["ok"] and rec["found"]
            lvl = "info" if good else ("warn" if rec["ok"] else "error")
            msg = f"{rec['role']} 서빙 {rec['url']} ({rec['model'] or '모델명 미설정'}) — {rec['detail']}"
            print(f"[모델점검] {'OK' if good else '주의'}: {msg}", flush=True)
            events.log("model", source="startup-probe", level=lvl, actor=rec["role"],
                       status=("ok" if rec["ok"] else "fail"), summary=msg)
        # 레지스트리가 비어 있으면 서빙에서 자동 등록 (이름 휴리스틱·빠름) — 부팅만 해도
        # 표가 차 있게. 이미 등록분이 있으면(사람이 큐레이션) 건드리지 않는다.
        try:
            if not model_registry.list_models():
                r = model_registry.sync_from_serving()  # 능력 테스트로 종류 판정
                m = (f"레지스트리 비어 자동 동기화 — 신규 {len(r.get('registered', []))} / "
                     f"전체 {r.get('total', 0)}건, 오류 {r.get('errors') or '없음'} "
                     f"(종류는 실제 호출로 판정, 접근 불가는 이름 폴백)")
                print(f"[모델자동등록] {m}", flush=True)
                events.log("model", source="startup-autosync", level="info", status="ok",
                           summary=m)
        except Exception as e:
            events.log("model", source="startup-autosync", level="error", status="fail",
                       summary=f"{type(e).__name__}: {str(e)[:200]}")

    threading.Thread(target=_run, daemon=True).start()


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
