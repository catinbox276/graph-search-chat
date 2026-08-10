"""앱 조립·기동 — 엔드포인트는 전부 app/routers/에 (여기 추가 금지).

- 멀티턴 기억: Oracle 체크포인터(thread_id=세션id) — 복제본 공유·재시작 생존
- 기동 순서: 스키마 보장(init_schema) → 임베딩 행렬 적재 → 에이전트 예열

usage: .venv/bin/uvicorn app.server:app --port 8500
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb
from fastapi import FastAPI

from app import auth, deps
from app.routers import (accounts, admin_models, admin_sources, chat, contrib,
                         graph, pages)
from tools import db as orm_db, source_registry
from tools.corpus_search import load_matrix
from tools.oracle_checkpointer import OracleSaver

app = FastAPI()
app.include_router(auth.router)           # 로그인·가입·로그아웃·/me
app.include_router(chat.router)           # 채팅·세션·문서 뷰
app.include_router(pages.router)          # 페이지 서빙
app.include_router(graph.router)          # 그래프 데이터·증거
app.include_router(contrib.router)        # 내 기여
app.include_router(accounts.router)       # 계정 관리
app.include_router(admin_models.router)   # 모델·MCP·에이전트 설정
app.include_router(admin_sources.router)  # 도메인·소스·전처리 운영

# 하위 호환: 일부 모듈이 server.db / server.log_turn을 참조
db = deps.db
log_turn = deps.log_turn


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
    load_matrix()                # 임베딩 행렬 메모리 적재 (하이브리드 검색)
    deps.set_saver(OracleSaver())  # 멀티턴 기억 — Oracle 외부화
    await deps.get_agent(None)   # 기본 LLM 예열


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
