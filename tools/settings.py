"""런타임 설정 저장소 (app_settings 테이블) — 재배포 없이 관리 UI에서 바꾸는 값.

.env(tools/config.py)는 기동 시 고정되는 인프라 값(접속 주소 등)이고, 여기는
운영 중 조절하는 값(전처리 배치량·동시성·모델 등)이다. 우선순위:
app_settings(DB) > .env/코드 기본값 — 값이 없으면 config 기본값으로 폴백한다.
model_registry가 같은 패턴의 선례 (DB 저장 + 관리자 API).
"""


def ensure(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'APP_SETTINGS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE app_settings (
            key   VARCHAR2(100) PRIMARY KEY,
            value CLOB,
            updated TIMESTAMP DEFAULT SYSTIMESTAMP)""")
        return
    # 구버전 VARCHAR2(400) → CLOB 재구축 (시스템 프롬프트 등 긴 값 저장용, 멱등)
    cur.execute("""SELECT data_type FROM user_tab_columns
                   WHERE table_name = 'APP_SETTINGS' AND column_name = 'VALUE'""")
    if cur.fetchone()[0] != "CLOB":
        cur.execute("""CREATE TABLE app_settings_v2 (
            key   VARCHAR2(100) PRIMARY KEY,
            value CLOB,
            updated TIMESTAMP DEFAULT SYSTIMESTAMP)""")
        cur.execute("""INSERT INTO app_settings_v2 (key, value, updated)
                       SELECT key, value, updated FROM app_settings""")
        cur.execute("DROP TABLE app_settings PURGE")
        cur.execute("ALTER TABLE app_settings_v2 RENAME TO app_settings")


def get_all(cur) -> dict:
    ensure(cur)
    cur.execute("SELECT key, value FROM app_settings")
    return {k: (v.read() if hasattr(v, "read") else v) for k, v in cur.fetchall()}


def set_many(cur, values: dict):
    """키별 upsert — 빈값이면 삭제(기본값으로 복귀)."""
    ensure(cur)
    for k, v in values.items():
        if v is None or str(v).strip() == "":
            cur.execute("DELETE FROM app_settings WHERE key = :1", [k])
        else:
            cur.execute("""MERGE INTO app_settings s USING dual ON (s.key = :k)
                           WHEN MATCHED THEN UPDATE SET value = :v, updated = SYSTIMESTAMP
                           WHEN NOT MATCHED THEN INSERT (key, value) VALUES (:k, :v)""",
                        {"k": k, "v": str(v).strip()[:8000]})


def get_int(settings: dict, key: str, default: int) -> int:
    try:
        return int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
