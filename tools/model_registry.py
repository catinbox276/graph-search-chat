"""모델 레지스트리 — Oracle model_registry 테이블.

- 등록: LM Studio /v1/models 동기화 (이름 휴리스틱으로 llm/embedding/reranker 분류)
- LLM 기본값: 사용자가 UI에서 세션별 선택 (레지스트리의 default는 초기값)
- embedding/reranker 기본값: 관리자 API로만 변경 (임베딩 교체 = 전체 재백필 필요)
"""
import httpx
import oracledb

from tools import config

DSN = config.ORACLE_DSN
USER = config.ORACLE_USER
PASSWORD = config.ORACLE_PASSWORD
MODEL_URL = config.CHAT_URL  # sync는 LLM 호스트의 /v1/models만 조회 (임베딩·리랭커는 별도 호스트)


_pool = None


def _con():
    """풀에서 커넥션을 빌려준다(재사용). `with _con() as con:`로 써서 예외에도 반납 보장.
    풀은 첫 사용 시 지연 생성 — DB 미기동 시 import는 실패하지 않고, get_default가 fallback으로 동작."""
    global _pool
    if _pool is None:
        _pool = oracledb.create_pool(user=USER, password=PASSWORD, dsn=DSN,
                                     min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                                     increment=config.ORACLE_POOL_INCREMENT)
    return _pool.acquire()


def _ensure(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name='MODEL_REGISTRY'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE model_registry (
            kind VARCHAR2(20), name VARCHAR2(200),
            enabled CHAR(1) DEFAULT 'Y', is_default CHAR(1) DEFAULT 'N',
            registered TIMESTAMP DEFAULT SYSTIMESTAMP,
            PRIMARY KEY (kind, name))""")


def _classify(name: str) -> str:
    n = name.lower()
    if "embed" in n:
        return "embedding"
    if "rerank" in n:
        return "reranker"
    return "llm"


def sync_from_serving() -> dict:
    """모델 서빙(/v1/models)에서 목록을 받아 레지스트리에 등록(업서트)."""
    models = httpx.get(f"{MODEL_URL}/models", timeout=10).json()["data"]
    added = []
    with _con() as con:
        cur = con.cursor()
        _ensure(cur)
        for m in models:
            kind = _classify(m["id"])
            cur.execute("""MERGE INTO model_registry r USING dual
                           ON (r.kind=:k AND r.name=:n)
                           WHEN NOT MATCHED THEN INSERT (kind, name) VALUES (:k, :n)""",
                        {"k": kind, "n": m["id"]})
            if cur.rowcount:
                added.append(f"{kind}:{m['id']}")
        # 종류별 default 없으면 첫 모델을 default로
        for kind in ("llm", "embedding", "reranker"):
            cur.execute("SELECT COUNT(*) FROM model_registry WHERE kind=:1 AND is_default='Y'", [kind])
            if not cur.fetchone()[0]:
                cur.execute("""UPDATE model_registry SET is_default='Y'
                               WHERE kind=:k AND name=(SELECT MIN(name) FROM model_registry WHERE kind=:k)""",
                            {"k": kind})
        con.commit()
    return {"registered": added, "total": len(models)}


def list_models(kind: str | None = None) -> list:
    with _con() as con:
        cur = con.cursor()
        _ensure(cur)
        q = "SELECT kind, name, enabled, is_default FROM model_registry"
        if kind:
            cur.execute(q + " WHERE kind=:1 ORDER BY name", [kind])
        else:
            cur.execute(q + " ORDER BY kind, name")
        return [{"kind": r[0], "name": r[1], "enabled": r[2] == "Y",
                 "default": r[3] == "Y"} for r in cur.fetchall()]


def get_default(kind: str, fallback: str) -> str:
    try:
        with _con() as con:
            cur = con.cursor()
            _ensure(cur)
            cur.execute("SELECT name FROM model_registry WHERE kind=:1 AND is_default='Y'", [kind])
            r = cur.fetchone()
        return r[0] if r else fallback
    except Exception:
        return fallback  # DB 미기동 시에도 동작 (fallback = 기존 하드코딩 값)


def set_default(kind: str, name: str) -> None:
    with _con() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM model_registry WHERE kind=:1 AND name=:2",
                    [kind, name])
        if not cur.fetchone()[0]:
            raise ValueError(f"미등록 모델: {kind}/{name} (먼저 sync 하세요)")
        cur.execute("UPDATE model_registry SET is_default='N' WHERE kind=:1", [kind])
        cur.execute("UPDATE model_registry SET is_default='Y' WHERE kind=:1 AND name=:2",
                    [kind, name])
        con.commit()


if __name__ == "__main__":
    print(sync_from_serving())
    for m in list_models():
        print(m)
