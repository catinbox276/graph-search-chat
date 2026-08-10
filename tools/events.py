"""활동 로그 — 정상·비정상 이벤트를 Oracle app_events에 적재 (docs/plan.md §7).

원칙:
- log()은 **절대 예외를 밖으로 던지지 않는다** — 로깅 실패가 본 기능을 막으면 안 된다.
- "전부 쌓기"라 무한 증가하므로 purge_old()로 보관 기간(EVENTS_RETAIN_DAYS) 회전.
  야간 유지보수(graph_maintenance)가 호출한다.
- 테이블 생성은 db.init_schema() (models.AppEvent). 조회는 관리 페이지 /admin.
"""
from tools import config, db
from tools.models import AppEvent


def log(kind, source="", level="info", actor=None, ref=None, status=None,
        duration_ms=None, summary="", detail=None):
    """이벤트 1건 기록. 어떤 경우에도 예외를 삼킨다 (로깅이 앱을 못 죽이게)."""
    try:
        with db.session() as s:
            s.add(AppEvent(kind=kind, level=level, source=(source or "")[:200],
                           actor=(actor or None), ref=(ref or None),
                           status=(str(status) if status is not None else None),
                           duration_ms=duration_ms, summary=(summary or "")[:1000],
                           detail=detail))
    except Exception:
        pass  # 로깅 실패는 무시 — 본 기능 보호가 우선


def purge_old(days=None):
    """보관 기간 지난 이벤트 삭제 (멱등). 반환: 삭제 건수 (실패 시 0)."""
    days = days if days is not None else config.EVENTS_RETAIN_DAYS
    try:
        from sqlalchemy import text
        with db.session() as s:
            n = s.execute(
                text("DELETE FROM app_events WHERE ts < SYSTIMESTAMP - :d"),
                {"d": days}).rowcount
        return n or 0
    except Exception:
        return 0
