"""계정 관리 — 가입 승인·관리자 권한 부여/해제·삭제 (관리자 전용)."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from web.deps import check_admin
from core import db as orm_db
from core.models import AppUser

router = APIRouter()


@router.get("/admin/users")
def admin_users(request: Request):
    """관리자: 계정 목록 (승인 대기 + 활성, 권한 표시)."""
    check_admin(request)
    with orm_db.session() as s:
        users = s.query(AppUser).order_by(AppUser.approved,
                                          AppUser.created_at.desc()).all()
        rows = [{"user_id": u.user_id, "approved": u.approved == "Y",
                 "is_admin": (u.is_admin or "N") == "Y",
                 "created_at": u.created_at.isoformat() if u.created_at else None}
                for u in users]
    return {"users": rows}


class UserActIn(BaseModel):
    user_id: str
    action: str  # approve(승인) | admin_on | admin_off | delete(거절/삭제)


@router.post("/admin/users/act")
def admin_user_act(inp: UserActIn, request: Request):
    """관리자: 계정 승인 / 관리자 권한 부여·해제 / 삭제. 권한 변경은 재로그인 시 반영."""
    check_admin(request)
    uid = inp.user_id.strip()
    if inp.action not in ("approve", "admin_on", "admin_off", "delete"):
        raise HTTPException(400, "action은 approve/admin_on/admin_off/delete 중 하나")
    from sqlalchemy import func
    with orm_db.session() as s:
        u = s.get(AppUser, uid)
        if not u:
            raise HTTPException(404, f"계정이 없습니다: {uid}")
        if inp.action == "approve":
            # SYSTIMESTAMP는 func로 쓰면 빈 괄호가 붙어 ORA-30088 — ANSI 함수 사용
            u.approved, u.approved_at = "Y", func.current_timestamp()
        elif inp.action == "admin_on":
            u.is_admin = "Y"
        elif inp.action == "admin_off":
            u.is_admin = "N"
        else:
            s.delete(u)
    return {"ok": True}

