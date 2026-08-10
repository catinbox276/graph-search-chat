"""모델 레지스트리 — model_registry 테이블 (ORM).

- 등록: LM Studio /v1/models 동기화 (이름 휴리스틱으로 llm/embedding/reranker 분류)
- LLM 기본값: 사용자가 UI에서 세션별 선택 (레지스트리의 default는 초기값)
- embedding/reranker 기본값: 관리자 API로만 변경 (임베딩 교체 = 전체 재백필 필요)
- 테이블 생성은 db.init_schema(). DB 미기동 시 get_default/embedding_endpoint는
  .env 폴백으로 동작 (import 시 접속하지 않음).
"""
import httpx

from tools import config, db
from tools.models import ModelRegistry

MODEL_URL = config.CHAT_URL  # sync는 LLM 호스트의 /v1/models만 조회


def _classify(name: str) -> str:
    n = name.lower()
    if "embed" in n:
        return "embedding"
    if "rerank" in n:
        return "reranker"
    return "llm"


def _ensure_default(s, kind: str):
    """그 종류에 기본값이 없으면 이름순 첫 모델을 기본값으로."""
    has = s.query(ModelRegistry).filter_by(kind=kind, is_default="Y").first()
    if not has:
        first = (s.query(ModelRegistry).filter_by(kind=kind)
                 .order_by(ModelRegistry.name).first())
        if first:
            first.is_default = "Y"


def sync_from_serving() -> dict:
    """모델 서빙(/v1/models)에서 목록을 받아 레지스트리에 등록(업서트)."""
    models = httpx.get(f"{MODEL_URL}/models", timeout=10).json()["data"]
    added = []
    with db.session() as s:
        for m in models:
            kind = _classify(m["id"])
            if not s.get(ModelRegistry, (kind, m["id"])):
                s.add(ModelRegistry(kind=kind, name=m["id"]))
                added.append(f"{kind}:{m['id']}")
        s.flush()
        for kind in ("llm", "embedding", "reranker"):
            _ensure_default(s, kind)
    return {"registered": added, "total": len(models)}


def list_models(kind: str | None = None) -> list:
    with db.session() as s:
        q = s.query(ModelRegistry)
        q = q.filter_by(kind=kind).order_by(ModelRegistry.name) if kind \
            else q.order_by(ModelRegistry.kind, ModelRegistry.name)
        return [{"kind": r.kind, "name": r.name, "enabled": r.enabled == "Y",
                 "default": r.is_default == "Y", "base_url": r.base_url or ""}
                for r in q.all()]


def add_model(kind: str, name: str, base_url: str = "", enabled: bool = True):
    """수동 등록/수정 (사내 vLLM처럼 sync 못 하는 호스트의 모델)."""
    if kind not in ("llm", "embedding", "reranker"):
        raise ValueError(f"kind는 llm/embedding/reranker 중 하나: {kind}")
    with db.session() as s:
        row = s.get(ModelRegistry, (kind, name))
        if row:
            row.base_url = base_url or None
            row.enabled = "Y" if enabled else "N"
        else:
            s.add(ModelRegistry(kind=kind, name=name, base_url=base_url or None,
                                enabled="Y" if enabled else "N"))
            s.flush()
        _ensure_default(s, kind)


def embedding_endpoint() -> tuple:
    """임베딩 (base_url, 모델명) — 레지스트리 기본값 우선, 없으면 .env 폴백.
    검색·청크 백필·dedup·경로 진입점이 전부 이걸 쓴다 (한 곳에서 해석)."""
    try:
        with db.session() as s:
            r = s.query(ModelRegistry).filter_by(
                kind="embedding", is_default="Y", enabled="Y").first()
            if r:
                return (r.base_url or config.EMBED_URL), r.name
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
    with db.session() as s:
        row = s.get(ModelRegistry, (kind, name))
        if row:
            row.enabled = "Y" if enabled else "N"


def get_default(kind: str, fallback: str) -> str:
    try:
        with db.session() as s:
            r = s.query(ModelRegistry).filter_by(kind=kind, is_default="Y").first()
            return r.name if r else fallback
    except Exception:
        return fallback  # DB 미기동 시에도 동작


def set_default(kind: str, name: str) -> None:
    with db.session() as s:
        if not s.get(ModelRegistry, (kind, name)):
            raise ValueError(f"미등록 모델: {kind}/{name} (먼저 sync 하세요)")
        s.query(ModelRegistry).filter_by(kind=kind).update({"is_default": "N"})
        s.query(ModelRegistry).filter_by(kind=kind, name=name).update(
            {"is_default": "Y"})


if __name__ == "__main__":
    print(sync_from_serving())
    for m in list_models():
        print(m)
