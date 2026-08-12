"""모델 레지스트리 — model_registry 테이블 (ORM).

- 등록: 설정된 서빙 호스트들의 /v1/models 동기화 (능력 테스트로 llm/embedding/reranker 분류)
- LLM 기본값: 사용자가 UI에서 세션별 선택 (레지스트리의 default는 초기값)
- embedding/reranker 기본값: 관리자 API로만 변경 (임베딩 교체 = 전체 재백필 필요)
- 테이블 생성은 db.init_schema(). DB 미기동 시 get_default/embedding_endpoint는
  .env 폴백으로 동작 (import 시 접속하지 않음).
"""
import httpx

from core import config, db
from core.models import ModelRegistry


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


def _probe_kind(base_url: str, name: str, timeout: float = 6.0) -> tuple:
    """모델 능력을 실제 호출로 판정 — (kind, 근거). 이름 휴리스틱보다 정확:
    /embeddings 성공→embedding, /chat/completions 성공→llm, 이름 rerank→reranker,
    둘 다 실패 시 이름 휴리스틱 폴백."""
    base = base_url.rstrip("/")
    hdr = {"Authorization": f"Bearer {config.MODEL_API_KEY}"}
    if "rerank" in name.lower():
        return "reranker", "이름 기준(rerank)"
    try:
        r = httpx.post(f"{base}/embeddings", headers=hdr, timeout=timeout,
                       json={"model": name, "input": "ping"})
        if r.status_code == 200 and (r.json().get("data")):
            return "embedding", "임베딩 응답 정상"
    except Exception:
        pass
    try:
        r = httpx.post(f"{base}/chat/completions", headers=hdr, timeout=timeout,
                       json={"model": name,
                             "messages": [{"role": "user", "content": "ping"}],
                             "max_tokens": 1})
        if r.status_code == 200:
            return "llm", "채팅 응답 정상"
    except Exception:
        pass
    return _classify(name), "테스트 불가 — 이름 휴리스틱"


def sync_from_serving(base_url: str = "", test: bool = False) -> dict:
    """모델 서빙 목록 동기화(업서트). base_url 지정 시 그 호스트만, 없으면 설정된
    채팅·임베딩·리랭커 호스트 전부 조회(중복 제거). 각 모델을 base_url과 함께 등록.

    종류 판정: 기본은 이름 휴리스틱(즉시). test=True면 모델마다 실제 호출로 판정 —
    정확하지만 모델당 최대 2요청이라 모델이 많으면 느리다. 어느 쪽이든 발견한 모델은
    전부 등록한다(설정 모델명이 서빙에 없어도 무관 — 등록 후 사람이 기본값 선택)."""
    hosts = ([base_url.rstrip("/")] if base_url.strip()
             else list(dict.fromkeys(
                 u.rstrip("/") for u in
                 (config.CHAT_URL, config.EMBED_URL, config.RERANK_URL) if u)))
    hdr = {"Authorization": f"Bearer {config.MODEL_API_KEY}"}
    results, errors, total = [], [], 0
    with db.session() as s:
        for host in hosts:
            try:
                data = httpx.get(f"{host}/models", headers=hdr,
                                 timeout=10).json().get("data") or []
            except Exception as e:
                errors.append(f"{host} — {type(e).__name__}: {str(e)[:100]}")
                continue
            total += len(data)
            for m in data:
                name = m.get("id")
                if not name:
                    continue
                kind, why = (_probe_kind(host, name) if test
                             else (_classify(name), "이름 휴리스틱"))
                row = s.get(ModelRegistry, (kind, name))
                if row:
                    row.base_url = host          # 주소 갱신(다른 호스트로 옮겼을 수도)
                    status = "갱신"
                else:
                    s.add(ModelRegistry(kind=kind, name=name, base_url=host))
                    status = "신규"
                results.append({"kind": kind, "name": name, "base_url": host,
                                "why": why, "status": status})
        s.flush()
        for kind in ("llm", "embedding", "reranker"):
            _ensure_default(s, kind)
    registered = [f"{r['kind']}:{r['name']}" for r in results if r["status"] == "신규"]
    return {"registered": registered, "total": total, "hosts": hosts,
            "models": results, "errors": errors}


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


def probe_serving(timeout: float = 5.0) -> list:
    """기동 시 설정된 모델 서빙 연결 점검 — 채팅·임베딩 엔드포인트에 GET /models.
    [{role,url,model,ok,found,detail}] 반환, 예외 안 냄(서버 기동을 못 막게)."""
    checks = [("chat", config.CHAT_URL, get_default("llm", None) or config.CHAT_MODEL)]
    try:
        eurl, emodel = embedding_endpoint()
        checks.append(("embedding", eurl, emodel))
    except Exception:
        pass
    out, seen = [], set()
    for role, url, name in checks:
        if not url or (url, name) in seen:
            continue
        seen.add((url, name))
        rec = {"role": role, "url": url, "model": name or "",
               "ok": False, "found": False, "detail": ""}
        try:
            r = httpx.get(url.rstrip("/") + "/models",
                          headers={"Authorization": f"Bearer {config.MODEL_API_KEY}"},
                          timeout=timeout)
            rec["ok"] = r.status_code == 200
            ids = [m.get("id") for m in (r.json().get("data") or [])] if rec["ok"] else []
            rec["found"] = bool(name) and name in ids
            avail = ("" if rec["found"] else
                     f", 서빙 모델: [{', '.join(str(i) for i in ids[:10])}"
                     f"{' …' if len(ids) > 10 else ''}] — 서빙 동기화 후 기본 지정하세요")
            rec["detail"] = (f"HTTP {r.status_code}, 서빙 모델 {len(ids)}개"
                             + ("" if rec["found"]
                                else (f", 설정 모델명 '{name}' 미발견" if name
                                      else ", 설정 모델명 없음") + avail))
        except Exception as e:
            rec["detail"] = f"{type(e).__name__}: {str(e)[:150]}"
        out.append(rec)
    return out


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
