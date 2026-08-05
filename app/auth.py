"""SSO 인증 — 앱이 소비하는 건 userId·role 2개뿐 (docs/integration.md 접점 1).

AUTH_MODE 두 갈래 (none이면 전부 비활성):

- header (사내 기본): 전단 SSO가 인증을 끝내고 헤더로 식별을 넘겨준다.
  앱은 SSO_USER_HEADER(userId)·SSO_ROLE_HEADER(role 목록)만 읽는다 — 로그인 UI 없음.
  전제: 앱에 전단 우회 직접 접근 불가(헤더 위조 방지는 네트워크/인그레스가 보장).

- keycloak (전단 프록시 없는 환경): 앱이 직접 OIDC 코드 플로우.
  - 로그인 상태는 서명 쿠키(itsdangerous)로만 유지 — 서버 저장소가 없어
    복제본 공유·재시작 생존이 자동 (cluster 모드에서 세션 고정 불필요).
  - 역할은 access_token의 realm_access.roles에서 읽는다.
  - ID 토큰 서명 검증은 생략한다: 코드→토큰 교환이 client_secret으로 인증된
    서버-서버 채널이라 OIDC Core 3.1.3.7이 TLS 채널 검증으로 갈음을 허용.
    대신 iss(발급자)·aud(수신자)·state(브라우저 바인딩 nonce)는 검증한다.

관리자 = OIDC_ADMIN_ROLE(기본 gsc-admin) 역할 보유 — 두 모드 공통.
사내 SSO 전환: .env의 AUTH_MODE=header + 헤더명 2개만 맞추면 끝.
"""
import base64
import json
import re
import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from tools import config

COOKIE = "gsc_auth"
_signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="gsc-oidc")
router = APIRouter()


def active() -> bool:
    """인증이 켜져 있는가 (header 또는 keycloak)."""
    return config.AUTH_MODE in ("header", "keycloak")


def enabled() -> bool:
    """keycloak(직접 OIDC) 모드인가 — /oidc/* 라우트는 이 모드에서만 동작."""
    return config.AUTH_MODE == "keycloak"


def _ep(base: str, name: str) -> str:
    return f"{base}/realms/{config.KEYCLOAK_REALM}/protocol/openid-connect/{name}"


def current_user(request: Request) -> dict | None:
    """로그인 정보 {user, roles} — 모드별 출처에서 읽고, 없으면 None.

    header: 전단 SSO가 주입한 헤더 2개 (userId 필수, role은 선택)
    keycloak: 서명 쿠키 (위조·만료면 None)
    """
    if config.AUTH_MODE == "header":
        uid = (request.headers.get(config.SSO_USER_HEADER) or "").strip()
        if not uid:
            return None
        raw = request.headers.get(config.SSO_ROLE_HEADER) or ""
        return {"user": uid, "roles": [r for r in re.split(r"[,;\s]+", raw) if r]}
    if config.AUTH_MODE == "keycloak":
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            return _signer.loads(raw, max_age=config.SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None
    return None


def _unauthorized() -> HTTPException:
    if enabled():
        return HTTPException(401, "로그인이 필요합니다 — /oidc/login")
    return HTTPException(401, f"SSO 식별 헤더({config.SSO_USER_HEADER})가 없습니다 "
                              "— 전단 SSO를 경유해 접속하세요")


def require_user(request: Request) -> dict | None:
    """API 가드: 미식별 401. AUTH_MODE=none이면 통과(None)."""
    if not active():
        return None
    u = current_user(request)
    if not u:
        raise _unauthorized()
    return u


def is_admin(request: Request) -> bool:
    u = current_user(request)
    return bool(u and config.OIDC_ADMIN_ROLE in u.get("roles", []))


def page_guard(request: Request) -> RedirectResponse | None:
    """페이지 가드 — keycloak: 로그인으로 리다이렉트 / header: 401 (로그인 UI 없음)."""
    if not active() or current_user(request):
        return None
    if enabled():
        nxt = urllib.parse.quote(request.url.path or "/")
        return RedirectResponse(f"/oidc/login?next={nxt}")
    raise _unauthorized()


def _safe_next(n: str) -> str:
    """로그인 후 이동 경로 — 사이트 내부 절대경로만 허용 (오픈 리다이렉트 차단).
    '//host'·'/\\host'는 브라우저가 외부 주소로 해석하므로 함께 거른다."""
    return n if n.startswith("/") and not n.startswith(("//", "/\\")) else "/"


def _jwt_payload(token: str) -> dict:
    """JWT payload 디코드(서명 검증 없음 — 모듈 docstring의 근거 참조)."""
    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except Exception:
        return {}


@router.get("/oidc/login")
def login(next: str = "/"):
    if not enabled():
        return RedirectResponse("/")
    # state를 브라우저에 바인딩(로그인 CSRF 차단): nonce를 쿠키와 서명 state 양쪽에
    # 넣고 콜백에서 일치를 요구 — 공격자가 만든 콜백 URL은 쿠키가 없어 거부된다.
    nonce = secrets.token_urlsafe(16)
    state = _signer.dumps({"next": _safe_next(next), "n": nonce})
    q = urllib.parse.urlencode({
        "client_id": config.OIDC_CLIENT_ID, "response_type": "code",
        "scope": "openid",
        "redirect_uri": f"{config.APP_BASE_URL}/oidc/callback", "state": state})
    resp = RedirectResponse(f"{_ep(config.KEYCLOAK_PUBLIC_URL, 'auth')}?{q}")
    resp.set_cookie("oidc_state", nonce, max_age=600, httponly=True, samesite="lax")
    return resp


@router.get("/oidc/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    if not enabled():
        return RedirectResponse("/")
    try:
        st = _signer.loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(400, "잘못되었거나 만료된 state — 다시 로그인하세요")
    if not st.get("n") or st["n"] != request.cookies.get("oidc_state"):
        raise HTTPException(400, "state가 이 브라우저의 로그인 시도와 일치하지 않습니다 "
                                 "— 다시 로그인하세요")
    nxt = _safe_next(st.get("next", "/"))
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.post(_ep(config.KEYCLOAK_INTERNAL_URL, "token"), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": f"{config.APP_BASE_URL}/oidc/callback",
            "client_id": config.OIDC_CLIENT_ID,
            "client_secret": config.OIDC_CLIENT_SECRET})
    if r.status_code != 200:
        raise HTTPException(502, f"Keycloak 토큰 교환 실패: {r.text[:300]}")
    tok = r.json()
    idt = _jwt_payload(tok.get("id_token", ""))
    acc = _jwt_payload(tok.get("access_token", ""))
    iss = f"{config.KEYCLOAK_PUBLIC_URL}/realms/{config.KEYCLOAK_REALM}"
    aud = idt.get("aud")
    if idt.get("iss") != iss or not (
            aud == config.OIDC_CLIENT_ID
            or (isinstance(aud, list) and config.OIDC_CLIENT_ID in aud)):
        raise HTTPException(502, "ID 토큰 검증 실패 (iss/aud 불일치)")
    user = {"user": idt.get("preferred_username") or idt.get("sub"),
            "roles": (acc.get("realm_access") or {}).get("roles", [])}
    resp = RedirectResponse(nxt)
    resp.set_cookie(COOKIE, _signer.dumps(user), max_age=config.SESSION_MAX_AGE,
                    httponly=True, samesite="lax")
    resp.delete_cookie("oidc_state")
    return resp


@router.get("/oidc/logout")
def logout():
    if enabled():
        q = urllib.parse.urlencode({
            "client_id": config.OIDC_CLIENT_ID,
            "post_logout_redirect_uri": config.APP_BASE_URL})
        resp = RedirectResponse(f"{_ep(config.KEYCLOAK_PUBLIC_URL, 'logout')}?{q}")
    else:
        resp = RedirectResponse("/")
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/me")
def me(request: Request):
    """UI 헤더용: 현재 로그인 사용자·관리자 여부."""
    u = current_user(request)
    return {"auth_mode": config.AUTH_MODE,
            "user": (u or {}).get("user"),
            "admin": is_admin(request)}
