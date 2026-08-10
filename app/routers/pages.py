"""페이지 서빙 — 로그인 가드 후 정적 HTML (내용 API는 각자 권한 검사)."""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app import auth
from app.deps import ROOT

router = APIRouter()


@router.get("/static/shell.css")
def shell_css():
    return FileResponse(ROOT / "app" / "shell.css",
                        headers={"Cache-Control": "no-store"})


@router.get("/")
def index(request: Request):
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "index.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/contrib")
def contrib_page(request: Request):
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "contrib.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/graph")
def graph_page(request: Request):
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "graph.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/login")
def login_page():
    """로그인/회원가입 페이지 — 유일하게 가드 없는 화면."""
    return FileResponse(ROOT / "app" / "login.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/admin")
def admin_page(request: Request):
    """관리 콘솔 페이지 — 도메인·소스·처리 현황·전처리 설정 (모달에서 분리).
    페이지는 로그인만 요구, 내용 API가 각자 관리자 권한을 검사한다."""
    if (r := auth.page_guard(request)):
        return r
    return FileResponse(ROOT / "app" / "admin.html",
                        headers={"Cache-Control": "no-store"})

