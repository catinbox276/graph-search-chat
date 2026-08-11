"""운영 설정 — app_settings 테이블 (ORM). 관리 페이지에서 재배포 없이 변경.

빈값 저장 = 키 삭제(코드 기본값으로 복귀). 테이블 생성은 db.init_schema().
(구 VARCHAR2→CLOB 마이그레이션은 폐기 — 현행 DB는 전부 CLOB, 새 DB는 모델이 CLOB)
"""
from sqlalchemy import func

from core import db
from core.models import AppSetting


def get_all() -> dict:
    with db.session() as s:
        return {r.key: (r.value or "") for r in s.query(AppSetting).all()}


def set_many(values: dict):
    """키별 upsert — 빈값이면 삭제(기본값으로 복귀)."""
    with db.session() as s:
        for k, v in values.items():
            v = str(v).strip() if v is not None else ""
            row = s.get(AppSetting, k)
            if not v:
                if row:
                    s.delete(row)
            elif row:
                row.value, row.updated = v, func.current_timestamp()
            else:
                s.add(AppSetting(key=k, value=v))


def get_int(settings: dict, key: str, default: int) -> int:
    try:
        return int(str(settings.get(key, "")).strip() or default)
    except (TypeError, ValueError):
        return default
