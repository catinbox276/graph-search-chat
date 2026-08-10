"""활동 로그 조회 — app_events (관리자 전용). 정상·비정상 전부, 최신순 + 필터·검색·페이지."""
from fastapi import APIRouter, Request

from app.deps import check_admin, db

router = APIRouter()

_KINDS = ("request", "tool", "batch", "admin", "model", "error")
_LEVELS = ("info", "warn", "error")


@router.get("/admin/events")
def admin_events(request: Request, kind: str = "", level: str = "",
                 q: str = "", page: int = 1, page_size: int = 50):
    """활동 로그 페이지 — kind/level 필터, source·summary 검색, 최신순."""
    check_admin(request)
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    where, binds = ["1=1"], {}
    if kind in _KINDS:
        where.append("kind = :kind"); binds["kind"] = kind
    if level in _LEVELS:
        where.append("lvl = :lvl"); binds["lvl"] = level
    if q.strip():
        where.append("(LOWER(source) LIKE :kw OR LOWER(summary) LIKE :kw)")
        binds["kw"] = f"%{q.strip().lower()}%"
    wsql = " AND ".join(where)
    con = db()
    cur = con.cursor()
    cur.execute(f"""SELECT id, TO_CHAR(ts,'MM-DD HH24:MI:SS'), kind, lvl, source,
                           actor, ref, status, duration_ms, summary
                    FROM app_events WHERE {wsql}
                    ORDER BY id DESC
                    OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY""",
                {**binds, "off": (page - 1) * page_size, "lim": page_size})
    rows = [{"id": int(r[0]), "ts": r[1], "kind": r[2], "level": r[3],
             "source": r[4], "actor": r[5], "ref": r[6], "status": r[7],
             "duration_ms": int(r[8]) if r[8] is not None else None,
             "summary": r[9]} for r in cur.fetchall()]
    cur.execute(f"SELECT COUNT(*) FROM app_events WHERE {wsql}", binds)
    total = cur.fetchone()[0]
    con.close()
    return {"events": rows, "total": total, "page": page,
            "pages": (total + page_size - 1) // page_size}


@router.get("/admin/events/{eid}")
def admin_event_detail(eid: int, request: Request):
    """이벤트 1건 상세 — detail(스택트레이스·인자·컨텍스트) 포함."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    cur.execute("""SELECT TO_CHAR(ts,'YYYY-MM-DD HH24:MI:SS'), kind, lvl, source,
                          actor, ref, status, duration_ms, summary, detail
                   FROM app_events WHERE id = :1""", [eid])
    r = cur.fetchone()
    con.close()
    if not r:
        from fastapi import HTTPException
        raise HTTPException(404, "이벤트가 없습니다")
    detail = r[9].read() if hasattr(r[9], "read") else (r[9] or "")
    return {"ts": r[0], "kind": r[1], "level": r[2], "source": r[3], "actor": r[4],
            "ref": r[5], "status": r[6], "duration_ms": r[7], "summary": r[8],
            "detail": detail}
