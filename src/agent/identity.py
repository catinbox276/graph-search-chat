"""에이전트 정체성 — 이름·소개·지원범위. 관리 페이지(app_settings) 우선, 없으면 config.

시스템 프롬프트(agent)와 입력 라우터(triage)가 같은 값을 쓰도록 한 곳에서 해석한다.
DB 미기동/CLI 단독 실행 시 config 폴백. deepagents 등 무거운 import 없음(triage도 씀).
"""
from core import config


def identity(st: dict | None = None) -> tuple:
    """(name, intro, scope) — 관리 설정 우선, 없으면 config 기본값.
    st(app_settings.get_all() 결과)를 넘기면 DB를 다시 읽지 않는다."""
    if st is None:
        try:
            from core import settings
            st = settings.get_all()
        except Exception:
            st = {}
    name = (st.get("agent_name") or "").strip() or config.AGENT_NAME
    intro = (st.get("agent_intro") or "").strip() or config.AGENT_INTRO
    scope = (st.get("agent_scope") or "").strip() or config.AGENT_SCOPE
    intro = intro or f"사내 데이터와 지식 검색을 돕는 어시스턴트 '{name}'입니다"
    return name, intro, scope
