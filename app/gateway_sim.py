"""PoC 게이트웨이 SSO 미들웨어 시뮬레이터 — 사내 구조 리허설용.

사내 구조: Keycloak(JWT 발급) → 게이트웨이 SSO(검증 API, JSON 응답) → 앱.
이 파드가 그 게이트웨이 역할을 흉내낸다 — Keycloak 표준 introspection(RFC 7662)에
위임해 JWT를 검증하고 {userId, roles} JSON을 돌려준다 (추가 의존성 없음).

사내 전환 시 이 파드는 사라지고, 앱의 GATEWAY_AUTH_URL만 실제 미들웨어로 바뀐다.

usage: uvicorn app.gateway_sim:app --port 8600   (k8s/gateway-sim.yaml)
검증:  GET /verify  (Authorization: Bearer <JWT>)  → {"userId": ..., "roles": [...]}
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from fastapi import FastAPI, HTTPException, Request

from tools import config

app = FastAPI(title="gateway-sim")
INTROSPECT = (f"{config.KEYCLOAK_INTERNAL_URL}/realms/{config.KEYCLOAK_REALM}"
              "/protocol/openid-connect/token/introspect")


@app.get("/verify")
async def verify(request: Request):
    raw = (request.headers.get("Authorization") or "").strip()
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not token:
        raise HTTPException(401, "no token")
    async with httpx.AsyncClient(timeout=5) as cli:
        r = await cli.post(INTROSPECT, data={
            "token": token,
            "client_id": config.OIDC_CLIENT_ID,
            "client_secret": config.OIDC_CLIENT_SECRET,
        })
    j = r.json() if r.status_code == 200 else {}
    if not j.get("active"):
        raise HTTPException(401, "invalid or expired token")
    return {"userId": j.get("preferred_username") or j.get("sub"),
            "roles": (j.get("realm_access") or {}).get("roles", [])}


@app.get("/healthz")
def healthz():
    return {"ok": True}
