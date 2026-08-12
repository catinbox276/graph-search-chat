"""자체 계정 인증 — 외부 SSO 없이 앱이 사용자를 직접 관리한다.

- 관리자: 환경 설정 계정 1개 (ADMIN_ID/ADMIN_PASSWORD — DB 아님, env 수정으로 복구 가능)
- 일반 계정: /login에서 가입(id+pw만) → app_users에 미승인(approved='N')으로 저장
  → 관리자가 관리 페이지에서 승인해야 로그인 가능
- 세션: 서명 쿠키(itsdangerous) — 서버 저장소 없음 → 복제본 공유·재시작 생존 자동
- 비밀번호: PBKDF2-HMAC-SHA256 (stdlib — 의존성 추가 없음)
"""
import hashlib
import re
import secrets
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from core import config, db, events
from core.models import AppUser

COOKIE = "gsc_auth"
_signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="gsc-local-auth")
router = APIRouter()

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,32}$")
_PBKDF2_ITERS = 100_000


# ── 비밀번호 해시 (stdlib PBKDF2) ─────────────────────────────
def _hash_pw(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt}${dk.hex()}"


def _verify_pw(password: str, stored: str) -> bool:
    try:
        _, iters, salt, hexhash = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


# ── 세션 (서명 토큰 — 쿠키 또는 Authorization Bearer 이중 전달) ──
def current_user(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:  # API/스크립트용: 같은 토큰을 Bearer 헤더로도 수용
        h = (request.headers.get("Authorization") or "").strip()
        raw = h[7:].strip() if h.lower().startswith("bearer ") else ""
    if not raw:
        return None
    try:
        return _signer.loads(raw, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def issue_token(user: dict) -> str:
    """서명 토큰 발급 — 쿠키·Bearer 공용 (무상태, SESSION_MAX_AGE 만료)."""
    return _signer.dumps(user)


def _set_login_cookie(resp, token: str):
    resp.set_cookie(COOKIE, token, max_age=config.SESSION_MAX_AGE,
                    httponly=True, samesite="lax")


def refresh_cookie(request: Request, resp) -> None:
    """슬라이딩 세션 갱신 — 수명 절반을 넘긴 유효 쿠키는 재발급해 만료를 밀어낸다.
    계속 사용 중인 사용자는 로그아웃되지 않고, 방치 시에만 MAX_AGE 후 만료.
    모든 요청 미들웨어(server.py)에서 호출 — 실패는 조용히 무시(다음 요청에 재시도)."""
    raw = request.cookies.get(COOKIE)
    if not raw:
        return
    # 핸들러가 이미 세션 쿠키를 설정·삭제한 응답(로그인·로그아웃)은 건드리지 않는다
    # — 로그아웃의 delete_cookie를 갱신이 덮어쓰면 로그아웃이 무효화된다
    if any(COOKIE + "=" in v for v in resp.headers.getlist("set-cookie")):
        return
    try:
        user, ts = _signer.loads(raw, max_age=config.SESSION_MAX_AGE,
                                 return_timestamp=True)
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > config.SESSION_MAX_AGE / 2:
            _set_login_cookie(resp, issue_token(user))
    except Exception:
        pass  # 만료·위조 쿠키는 갱신 대상 아님 — 본 요청 처리에 영향 없음


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "로그인이 필요합니다 — /login")
    return u


def is_admin(request: Request) -> bool:
    u = current_user(request)
    return bool(u and u.get("admin"))


def page_guard(request: Request) -> RedirectResponse | None:
    """페이지 가드 — 미로그인은 로그인 페이지로."""
    if current_user(request):
        return None
    nxt = urllib.parse.quote(request.url.path or "/")
    return RedirectResponse(f"/login?next={nxt}")


# ── 로그인 / 가입 / 로그아웃 ─────────────────────────────────
class CredIn(BaseModel):
    user_id: str
    password: str


@router.post("/auth/login")
def login(inp: CredIn):
    from fastapi.responses import JSONResponse
    uid, pw = inp.user_id.strip(), inp.password
    # 1) 관리자 (환경 설정 계정 — DB 조회 없음)
    if secrets.compare_digest(uid, config.ADMIN_ID):
        if not secrets.compare_digest(pw, config.ADMIN_PASSWORD):
            events.log("auth", source="/auth/login", actor=uid, level="warn",
                       summary="로그인 실패 (비밀번호 불일치)")
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
        events.log("auth", source="/auth/login", actor=uid, summary="로그인 성공 (관리자)")
        token = issue_token({"user": uid, "admin": True})
        resp = JSONResponse({"ok": True, "user": uid, "admin": True, "token": token})
        _set_login_cookie(resp, token)
        return resp
    # 2) 일반 계정 (가입 + 승인 필요)
    with db.session() as s:
        u = s.get(AppUser, uid)
        row = (u.pw_hash, u.approved, u.is_admin or "N") if u else None
    if not row or not _verify_pw(pw, row[0]):
        events.log("auth", source="/auth/login", actor=uid, level="warn",
                   summary="로그인 실패 (아이디 또는 비밀번호 불일치)")
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
    if row[1] != "Y":
        events.log("auth", source="/auth/login", actor=uid, level="warn",
                   summary="로그인 거부 (가입 승인 대기)")
        raise HTTPException(403, "가입 승인 대기 중입니다 — 관리자에게 승인을 요청하세요")
    admin = row[2] == "Y"  # 관리자가 부여한 권한 (재로그인 시 반영)
    events.log("auth", source="/auth/login", actor=uid,
               summary=f"로그인 성공{' (관리자 권한)' if admin else ''}")
    token = issue_token({"user": uid, "admin": admin})
    resp = JSONResponse({"ok": True, "user": uid, "admin": admin, "token": token})
    _set_login_cookie(resp, token)
    return resp


@router.post("/auth/signup")
def signup(inp: CredIn):
    uid, pw = inp.user_id.strip(), inp.password
    if not _ID_RE.fullmatch(uid):
        raise HTTPException(400, "아이디는 영문/숫자/._- 2~32자여야 합니다")
    if uid == config.ADMIN_ID:
        raise HTTPException(409, "사용할 수 없는 아이디입니다")
    if len(pw) < 4:
        raise HTTPException(400, "비밀번호는 4자 이상이어야 합니다")
    with db.session() as s:
        if s.get(AppUser, uid):
            raise HTTPException(409, "이미 존재하는 아이디입니다")
        s.add(AppUser(user_id=uid, pw_hash=_hash_pw(pw)))
    return {"ok": True, "note": "가입 완료 — 관리자 승인 후 로그인할 수 있습니다"}


@router.get("/auth/logout")
def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/me")
def me(request: Request):
    u = current_user(request)
    return {"user": (u or {}).get("user"), "admin": bool((u or {}).get("admin"))}
