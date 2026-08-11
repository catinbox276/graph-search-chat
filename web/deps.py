"""공용 의존물 — 라우터들이 함께 쓰는 것만 (DB 풀·권한 검사·에이전트 캐시·헬퍼).

규약: 새 엔드포인트는 server.py가 아니라 app/routers/의 해당 기능 라우터에.
DB를 만지는 로직은 tools/ 모듈로 — 라우터는 HTTP 입출력·권한 검사·호출만.
"""
import json
from pathlib import Path

import oracledb
from fastapi import HTTPException, Request

from agent.agent import build_agent
from web import auth
from core import config, db as orm_db

ROOT = Path(__file__).parent.parent

# 서버 전용 raw 커넥션 풀 — 복잡한 조회(검색·그래프·CLOB)용. 단순 CRUD는 orm_db.session().
_db_pool = oracledb.create_pool(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN,
                                min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                                increment=config.ORACLE_POOL_INCREMENT)


def db():
    return _db_pool.acquire()


def check_admin(request: Request):
    """관리자 = env 계정 또는 is_admin 부여 계정."""
    if not auth.is_admin(request):
        raise HTTPException(403, "관리자 권한이 필요합니다")


# ── 에이전트 캐시 (모델별) — 설정 변경 시 clear_agents()로 무효화 ──
_agents = {}
_saver = None


def set_saver(s):
    global _saver
    _saver = s


async def get_agent(model_name: str | None):
    from core import model_registry
    name = model_name or model_registry.get_default("llm", None) or None
    if name not in _agents:
        _agents[name] = await build_agent(checkpointer=_saver, model_name=name)
    return _agents[name]


def clear_agents():
    _agents.clear()


def log_turn(sid: str, question: str, calls: list, answer: str,
             user: str | None = None):
    from sqlalchemy import func
    from core.models import Session_
    with orm_db.session() as s:
        turn = s.query(func.coalesce(func.max(Session_.turn), 0) + 1) \
                .filter(Session_.id == sid).scalar()
        s.add(Session_(id=sid, turn=turn, question=question,
                       tool_calls=json.dumps(calls, ensure_ascii=False),
                       answer=answer, user_id=user))


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
