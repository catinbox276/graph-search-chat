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
            base_url VARCHAR2(500),
            registered TIMESTAMP DEFAULT SYSTIMESTAMP,
            PRIMARY KEY (kind, name))""")
        return
    # 모델별 서빙 주소 (사내 vLLM은 모델마다 호스트가 다름 — 빈값은 역할별 env 폴백)
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name='MODEL_REGISTRY' AND column_name='BASE_URL'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE model_registry ADD (base_url VARCHAR2(500))")


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
        q = "SELECT kind, name, enabled, is_default, base_url FROM model_registry"
        if kind:
            cur.execute(q + " WHERE kind=:1 ORDER BY name", [kind])
        else:
            cur.execute(q + " ORDER BY kind, name")
        return [{"kind": r[0], "name": r[1], "enabled": r[2] == "Y",
                 "default": r[3] == "Y", "base_url": r[4] or ""}
                for r in cur.fetchall()]


def add_model(kind: str, name: str, base_url: str = "", enabled: bool = True):
    """수동 등록/수정 (사내 vLLM처럼 sync 못 하는 호스트의 모델)."""
    if kind not in ("llm", "embedding", "reranker"):
        raise ValueError(f"kind는 llm/embedding/reranker 중 하나: {kind}")
    with _con() as con:
        cur = con.cursor()
        _ensure(cur)
        cur.execute("""MERGE INTO model_registry r USING dual ON (r.kind=:k AND r.name=:n)
                       WHEN MATCHED THEN UPDATE SET base_url=:u, enabled=:e
                       WHEN NOT MATCHED THEN INSERT (kind, name, base_url, enabled)
                       VALUES (:k, :n, :u, :e)""",
                    {"k": kind, "n": name, "u": base_url or None,
                     "e": "Y" if enabled else "N"})
        # 그 종류의 첫 모델이면 기본값으로
        cur.execute("SELECT COUNT(*) FROM model_registry WHERE kind=:1 AND is_default='Y'",
                    [kind])
        if not cur.fetchone()[0]:
            cur.execute("""UPDATE model_registry SET is_default='Y'
                           WHERE kind=:1 AND name=:2""", [kind, name])
        con.commit()


def embedding_endpoint() -> tuple:
    """임베딩 (base_url, 모델명) — 레지스트리 기본값 우선, 없으면 .env 폴백.
    검색·청크 백필·dedup·경로 진입점이 전부 이걸 쓴다 (한 곳에서 해석).
    모델 교체 시 청크는 embed_model 버저닝으로 자동 재백필, nodes는 별도 재백필."""
    try:
        with _con() as con:
            cur = con.cursor()
            _ensure(cur)
            cur.execute("""SELECT name, base_url FROM model_registry
                           WHERE kind='embedding' AND is_default='Y' AND enabled='Y'""")
            r = cur.fetchone()
        if r:
            return (r[1] or config.EMBED_URL), r[0]
    except Exception:
        pass  # DB 미기동 시에도 동작
    return config.EMBED_URL, config.EMBED_MODEL


_emb_clients = {}


def embedding_client() -> tuple:
    """(OpenAI 클라이언트, 모델명) — base_url별 클라이언트 재사용."""
    from openai import OpenAI
    url, name = embedding_endpoint()
    if url not in _emb_clients:
        _emb_clients[url] = OpenAI(base_url=url, api_key=config.MODEL_API_KEY)
    return _emb_clients[url], name


def set_enabled(kind: str, name: str, enabled: bool):
    with _con() as con:
        cur = con.cursor()
        cur.execute("""UPDATE model_registry SET enabled=:1
                       WHERE kind=:2 AND name=:3""",
                    ["Y" if enabled else "N", kind, name])
        con.commit()


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
