"""SSO 인증 — 앱이 소비하는 건 userId·role 2개뿐 (docs/integration.md 접점 1).

AUTH_MODE:

- proxy (사내 표준 — 타 서비스와 동일): 기본은 게이트웨이가 붙여준 userId 헤더
  (PROXY_USER_HEADER)를 신뢰 — 토큰 검증 없음. 요청 쿼리에 ?authMode=gateway가
  있을 때만 토큰(X-DL-Access-Token/Authorization)을 검증 API로 확인한다.
  전제: 앱에 게이트웨이 우회 직접 접근 불가 (헤더 신뢰의 근거는 네트워크).

- gateway: 모든 요청을 토큰 검증 (proxy의 상시 검증판 — 리허설·강화 환경용).

- keycloak (PoC 데모 전용 — 게이트웨이 없는 환경의 브라우저 로그인): 앱이 직접 OIDC 코드 플로우.
  - 로그인 상태는 서명 쿠키(itsdangerous)로만 유지 — 서버 저장소가 없어
    복제본 공유·재시작 생존이 자동 (cluster 모드에서 세션 고정 불필요).
  - 역할은 access_token의 realm_access.roles에서 읽는다.
  - ID 토큰 서명 검증은 생략한다: 코드→토큰 교환이 client_secret으로 인증된
    서버-서버 채널이라 OIDC Core 3.1.3.7이 TLS 채널 검증으로 갈음을 허용.
    대신 iss(발급자)·aud(수신자)·state(브라우저 바인딩 nonce)는 검증한다.

관리자 = OIDC_ADMIN_ROLE 역할 보유 또는 GATEWAY_ADMIN_USERS 지정.
사내 전환: AUTH_MODE=proxy + PROXY_USER_HEADER (+검증용 GATEWAY_AUTH_URL).
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
    """인증이 켜져 있는가 (proxy / gateway / keycloak)."""
    return config.AUTH_MODE in ("proxy", "gateway", "keycloak")


def enabled() -> bool:
    """keycloak(직접 OIDC) 모드인가 — /oidc/* 라우트는 이 모드에서만 동작."""
    return config.AUTH_MODE == "keycloak"


def _ep(base: str, name: str) -> str:
    return f"{base}/realms/{config.KEYCLOAK_REALM}/protocol/openid-connect/{name}"


def current_user(request: Request) -> dict | None:
    """로그인 정보 {user, roles} — 모드별 출처에서 읽고, 없으면 None.

    gateway: 요청 헤더의 토큰을 게이트웨이 검증 API로 확인
    keycloak: 서명 쿠키 (위조·만료면 None)
    """
    if config.AUTH_MODE == "proxy":
        # 요청 단위 스위치: ?authMode=gateway → 토큰 검증, 기본 → userId 헤더 신뢰
        if request.query_params.get("authMode") == "gateway":
            return _token_user(request)
        uid = (request.headers.get(config.PROXY_USER_HEADER) or "").strip()
        if not uid:
            if config.AUTH_DEBUG:
                import sys
                print(f"[auth-debug] userId 헤더({config.PROXY_USER_HEADER}) 없음 — "
                      f"수신 헤더: {sorted(request.headers.keys())}", file=sys.stderr)
            return None
        roles = []
        if config.PROXY_ROLE_HEADER:
            raw = request.headers.get(config.PROXY_ROLE_HEADER) or ""
            roles = [r for r in re.split(r"[,;\s]+", raw) if r]
        return {"user": uid, "roles": roles}
    if config.AUTH_MODE == "gateway":
        return _token_user(request)
    if config.AUTH_MODE == "keycloak":
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            return _signer.loads(raw, max_age=config.SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None
    return None


def _token_user(request: Request) -> dict | None:
    """토큰 검증 경로 — X-DL-Access-Token 우선, Authorization 폴백 (헤더→쿠키 순)."""
    for name in config.GATEWAY_TOKEN_HEADERS:
        raw = (request.headers.get(name) or request.cookies.get(name) or "").strip()
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
        if token:
            return _gateway_verify(token)
    if config.AUTH_DEBUG:  # 진단: 게이트웨이가 실제로 뭘 보내는지 (이름만, 값 미기록)
        import sys
        print(f"[auth-debug] 토큰 없음 — 수신 헤더: {sorted(request.headers.keys())} "
              f"/ 쿠키: {sorted(request.cookies.keys())}", file=sys.stderr)
    return None


_gw_cache = {}  # token -> (만료 epoch, user) — 매 요청 게이트웨이 왕복 방지


def _dig(obj, path: str):
    """점 표기 중첩 경로 조회 — 'result.userId' 같은 응답 스펙 대응."""
    for k in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _gateway_verify(token: str) -> dict | None:
    """게이트웨이 검증 API 호출 — 사내 스펙: POST {accessToken} → {result:{userId, role}}.
    필드명은 GATEWAY_TOKEN/USER/ROLE_FIELD로 매핑. 실패 이유는 stderr에 남긴다(토큰 미기록)."""
    import sys
    import time
    now = time.time()
    hit = _gw_cache.get(token)
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = httpx.post(config.GATEWAY_AUTH_URL,
                       json={config.GATEWAY_TOKEN_FIELD: token},
                       headers={"Authorization": f"Bearer {token}"},  # 바디+헤더 동시 요구 (사내 스펙)
                       timeout=config.GATEWAY_TIMEOUT)
        if r.status_code != 200:
            print(f"[auth] 게이트웨이 검증 거부: HTTP {r.status_code}", file=sys.stderr)
            return None
        j = r.json()
        uid = str(_dig(j, config.GATEWAY_USER_FIELD) or "").strip()
        if not uid:
            print(f"[auth] 게이트웨이 응답에 사용자 필드({config.GATEWAY_USER_FIELD}) 없음"
                  f" — 최상위 키: {list(j)[:5]}", file=sys.stderr)
            return None
        roles = _dig(j, config.GATEWAY_ROLE_FIELD) or []
        if isinstance(roles, str):
            roles = [x for x in re.split(r"[,;\s]+", roles) if x]
        user = {"user": uid, "roles": [str(x) for x in roles]}
    except Exception as e:
        print(f"[auth] 게이트웨이 검증 호출 실패({config.GATEWAY_AUTH_URL}): "
              f"{type(e).__name__}", file=sys.stderr)
        return None  # 게이트웨이 장애 = 미인증 (fail-closed)
    if len(_gw_cache) > 1000:  # 무한 성장 가드
        _gw_cache.clear()
    _gw_cache[token] = (now + config.GATEWAY_CACHE_TTL, user)
    return user


def _unauthorized() -> HTTPException:
    if enabled():
        return HTTPException(401, "로그인이 필요합니다 — /oidc/login")
    if config.AUTH_MODE == "proxy":
        return HTTPException(401, f"사용자 식별({config.PROXY_USER_HEADER} 헤더 또는 "
                                  "?authMode=gateway 토큰)이 없습니다 — 게이트웨이를 경유해 접속하세요")
    return HTTPException(401, f"유효한 토큰({'/'.join(config.GATEWAY_TOKEN_HEADERS)})이 "
                              "없습니다 — 게이트웨이 SSO를 경유해 접속하세요")


def require_user(request: Request) -> dict | None:
    """API 가드: 미식별 401. AUTH_MODE=none이면 통과(None)."""
    if not active():
        return None
    u = current_user(request)
    if not u:
        raise _unauthorized()
    return u


def is_admin(request: Request) -> bool:
    if not active():  # AUTH_MODE=none — 로컬 개발 한정, 루프백 접속만 관리자 허용
        # fail-open 방지: 설정 누락 배포에서 원격이 관리 기능을 열 수 없게
        client = request.client.host if request.client else ""
        return client in ("127.0.0.1", "::1")
    u = current_user(request)
    if not u:
        return False
    if config.OIDC_ADMIN_ROLE in u.get("roles", []):
        return True
    # 게이트웨이 응답에 role이 없는 환경용 폴백 — userId 지정 목록
    return u.get("user") in config.GATEWAY_ADMIN_USERS


def page_guard(request: Request) -> RedirectResponse | None:
    """페이지 가드 — keycloak: 로그인으로 리다이렉트 / gateway: 401 (로그인은 게이트웨이 담당)."""
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
