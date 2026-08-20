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


# 임베딩 계열 이름 힌트 — "embed" 없이도 흔한 임베딩 모델(bge-m3, gte, e5 등).
# 이름 휴리스틱의 한계라 완벽하진 않다 — 확실히 하려면 sync의 능력 테스트(test=True).
_EMBED_HINTS = ("embed", "bge", "gte", "e5", "nomic", "jina", "mxbai", "minilm", "stella")


def _lookup(kind: str, name: str) -> tuple:
    """레지스트리 행의 (base_url, api_key) — 없거나 DB 미기동이면 (None, None).
    모델별 서빙 주소·개발 키 해석의 단일 지점 (환경변수는 폴백)."""
    try:
        with db.session() as s:
            r = s.query(ModelRegistry).filter_by(kind=kind, name=name).first()
            return (r.base_url, r.api_key) if r else (None, None)
    except Exception:
        return (None, None)   # DB 미기동 시에도 .env 폴백으로 동작


def _classify(name: str) -> str:
    n = name.lower()
    if "rerank" in n:                       # bge-reranker처럼 embed 힌트와 겹치므로 먼저
        return "reranker"
    if any(k in n for k in _EMBED_HINTS):
        return "embedding"
    return "llm"


def _set_default_prefer_verified(s, kind: str, verified: set):
    """종류별 기본 모델 자동 지정 — 동작 테스트 통과(verified) 모델을 우선.
    현재 기본값이 verified면 유지, 아니면(없거나 죽은/미검증) verified 첫 모델로,
    verified가 없으면 이름순 첫 모델로. sync가 각 종류에 대해 호출."""
    cur = s.query(ModelRegistry).filter_by(kind=kind, is_default="Y").first()
    if cur is not None:
        if verified and cur.name not in verified:   # 현재 기본이 미검증 → 검증된 것으로 교체
            pick = (s.query(ModelRegistry)
                    .filter(ModelRegistry.kind == kind, ModelRegistry.name.in_(verified))
                    .order_by(ModelRegistry.name).first())
            if pick:
                cur.is_default = "N"
                pick.is_default = "Y"
        return  # 그 외에는 현재 기본값 유지
    pick = None
    if verified:
        pick = (s.query(ModelRegistry)
                .filter(ModelRegistry.kind == kind, ModelRegistry.name.in_(verified))
                .order_by(ModelRegistry.name).first())
    if pick is None:
        pick = (s.query(ModelRegistry).filter_by(kind=kind)
                .order_by(ModelRegistry.name).first())
    if pick:
        pick.is_default = "Y"


def _ensure_default(s, kind: str):
    """그 종류에 기본값이 없으면 이름순 첫 모델을 기본값으로 (수동 등록 경로용)."""
    has = s.query(ModelRegistry).filter_by(kind=kind, is_default="Y").first()
    if not has:
        first = (s.query(ModelRegistry).filter_by(kind=kind)
                 .order_by(ModelRegistry.name).first())
        if first:
            first.is_default = "Y"


def _payload_ok(r, key: str) -> bool:
    """진짜 응답인지 — 사내 게이트웨이는 에러도 HTTP 200으로 주고 본문에 {"error":...}를
    담으므로, 상태코드가 아니라 '에러 아님 + 기대 페이로드(key) 존재'로 판정."""
    if r.status_code != 200:
        return False
    try:
        j = r.json()
    except Exception:
        return False
    return not j.get("error") and bool(j.get(key))


def _probe_kind(base_url: str, name: str, timeout: float = 6.0,
                api_key: str = "") -> tuple:
    """모델 능력을 실제 호출로 판정 — (kind, 근거). 상태코드가 아니라 응답 본문으로.
    순서 중요: 임베딩 모델은 /rerank에도 답하므로(스코어) embeddings를 먼저 본다 —
    embeddings 진짜 응답=embedding, 아니면서 rerank 진짜 응답=reranker, chat=llm.
    셋 다 아니면(접근 불가·미지원) 이름 휴리스틱 폴백."""
    base = base_url.rstrip("/")
    hdr = {"Authorization": f"Bearer {api_key.strip() or config.MODEL_API_KEY}"}
    # 1) 임베딩 (임베딩 모델은 여기 진짜 벡터로 응답 — 리랭커는 error 본문)
    try:
        r = httpx.post(f"{base}/embeddings", headers=hdr, timeout=timeout,
                       json={"model": name, "input": "ping"})
        if _payload_ok(r, "data"):
            return "embedding", "임베딩 응답 정상"
    except Exception:
        pass
    # 2) 리랭커 (임베딩이 아닌데 /rerank가 진짜 results면 리랭커)
    try:
        r = httpx.post(f"{base}/rerank", headers=hdr, timeout=timeout,
                       json={"model": name, "query": "test", "documents": ["a", "b"]})
        if _payload_ok(r, "results"):
            return "reranker", "리랭크 응답 정상"
    except Exception:
        pass
    # 3) 채팅(생성)
    try:
        r = httpx.post(f"{base}/chat/completions", headers=hdr, timeout=timeout,
                       json={"model": name,
                             "messages": [{"role": "user", "content": "ping"}],
                             "max_tokens": 1})
        if _payload_ok(r, "choices"):
            return "llm", "채팅 응답 정상"
    except Exception:
        pass
    return _classify(name), "동작 판정 불가(미지원·접근 불가) — 이름 휴리스틱 폴백"


def sync_from_serving(base_url: str = "", test: bool = True,
                      api_key: str = "") -> dict:
    """모델 서빙 목록 동기화(업서트). base_url 지정 시 그 호스트만, 없으면 설정된
    채팅·임베딩·리랭커 호스트 전부 조회(중복 제거). 각 모델을 base_url과 함께 등록.
    api_key 지정 시 조회·판정에 그 키를 쓰고, 등록되는 모델에도 그 키를 저장
    (호스트+키를 한 번에 등록 — 사내 게이트웨이처럼 호스트마다 키가 다른 환경).

    종류 판정: 기본은 능력 테스트(실제 호출) — 이름은 태생적으로 불안정(bge-m3 등
    'embed' 없는 임베딩)하므로 동작으로 판정. 접근 불가 모델은 이름 휴리스틱 폴백.
    test=False면 이름만으로 빠르게(테스트 생략). 발견 모델은 설정 모델명 유무와
    무관하게 전부 등록(등록 후 사람이 기본값 선택)."""
    hosts = ([base_url.rstrip("/")] if base_url.strip()
             else list(dict.fromkeys(
                 u.rstrip("/") for u in
                 (config.CHAT_URL, config.EMBED_URL, config.RERANK_URL) if u)))
    key = api_key.strip()
    hdr = {"Authorization": f"Bearer {key or config.MODEL_API_KEY}"}
    errors, total = [], 0

    # 1) 목록 조회 + 종류 판정을 DB 세션 밖에서 먼저 (능력 테스트가 느려도 트랜잭션을
    #    오래 안 잡음 — 님이 지적한 'for문이 처리 못하고 롤백'을 구조적으로 방지).
    classified = []  # (host, name, kind, why)
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
            kind, why = (_probe_kind(host, name, api_key=key) if test
                         else (_classify(name), "이름 휴리스틱"))
            classified.append((host, name, kind, why))

    # 2) 짧은 트랜잭션으로 등록 (네트워크 없음).
    results = []
    with db.session() as s:
        for host, name, kind, why in classified:
            # 같은 이름의 다른 종류 잔재 제거 — 재분류(예: llm→embedding) 시 양쪽에
            # 남는 중복 방지. 서빙 모델명은 하나의 참 종류를 가진다는 전제.
            stale = s.query(ModelRegistry).filter(
                ModelRegistry.name == name, ModelRegistry.kind != kind).all()
            for old in stale:
                s.delete(old)
            row = s.get(ModelRegistry, (kind, name))
            if row:
                row.base_url = host          # 주소 갱신(다른 호스트로 옮겼을 수도)
                if key:
                    row.api_key = key        # 지정 키로 갱신 (미지정이면 기존 키 유지)
                status = "갱신"
            else:
                s.add(ModelRegistry(kind=kind, name=name, base_url=host,
                                    api_key=key or None))
                status = "신규"
            results.append({"kind": kind, "name": name, "base_url": host,
                            "why": why, "status": status})
        s.flush()
        for kind in ("llm", "embedding", "reranker"):
            # 이번 동기화에서 '응답 정상'으로 확인된 모델 = 동작 검증됨(이름 폴백·접근불가 제외)
            verified = {r["name"] for r in results
                        if r["kind"] == kind and "정상" in (r["why"] or "")}
            _set_default_prefer_verified(s, kind, verified)
    registered = [f"{r['kind']}:{r['name']}" for r in results if r["status"] == "신규"]
    return {"registered": registered, "total": total, "hosts": hosts,
            "models": results, "errors": errors}


def list_models(kind: str | None = None) -> list:
    """목록 — api_key는 값이 아니라 설정 여부(has_key)만 노출 (키 유출 방지)."""
    with db.session() as s:
        q = s.query(ModelRegistry)
        q = q.filter_by(kind=kind).order_by(ModelRegistry.name) if kind \
            else q.order_by(ModelRegistry.kind, ModelRegistry.name)
        return [{"kind": r.kind, "name": r.name, "enabled": r.enabled == "Y",
                 "default": r.is_default == "Y", "base_url": r.base_url or "",
                 "has_key": bool((r.api_key or "").strip())}
                for r in q.all()]


def add_model(kind: str, name: str, base_url: str = "", enabled: bool = True,
              api_key: str | None = None):
    """수동 등록/수정 (사내 vLLM처럼 sync 못 하는 호스트의 모델).
    api_key: None=변경 없음(기존 키 유지), ""=삭제(.env 전역 키로 폴백), 그 외=설정."""
    if kind not in ("llm", "embedding", "reranker"):
        raise ValueError(f"kind는 llm/embedding/reranker 중 하나: {kind}")
    with db.session() as s:
        row = s.get(ModelRegistry, (kind, name))
        if row:
            row.base_url = base_url or None
            row.enabled = "Y" if enabled else "N"
            if api_key is not None:
                row.api_key = api_key.strip() or None
        else:
            s.add(ModelRegistry(kind=kind, name=name, base_url=base_url or None,
                                enabled="Y" if enabled else "N",
                                api_key=(api_key or "").strip() or None))
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


def api_key_for(kind: str, name: str) -> str:
    """이 모델의 개발 키 — 레지스트리 값 우선, 없으면 .env 전역 MODEL_API_KEY."""
    return (_lookup(kind, name)[1] or "").strip() or config.MODEL_API_KEY


def chat_endpoint(name: str = "") -> tuple:
    """채팅 LLM (base_url, 모델명, api_key) — 모델별 레지스트리 값 우선, .env 폴백.
    에이전트·전처리 판정·라우터가 전부 이걸로 해석한다 (모델마다 다른 호스트·키 지원)."""
    resolved = (name or "").strip() or get_default("llm", config.CHAT_MODEL)
    url, key = _lookup("llm", resolved)
    return ((url or "").strip() or config.CHAT_URL, resolved,
            (key or "").strip() or config.MODEL_API_KEY)


_emb_clients, _chat_clients = {}, {}


def chat_client(name: str = "") -> tuple:
    """(OpenAI 클라이언트, 모델명) — (주소, 키) 조합별 클라이언트 재사용."""
    from openai import OpenAI
    url, resolved, key = chat_endpoint(name)
    ck = (url, key)
    if ck not in _chat_clients:
        # 타임아웃 필수 — 멈춘 요청이 배치·요청 스레드를 무한 대기시키지 않게
        _chat_clients[ck] = OpenAI(base_url=url, api_key=key, timeout=config.LLM_TIMEOUT)
    return _chat_clients[ck], resolved


def embedding_client(name: str = "") -> tuple:
    """(OpenAI 클라이언트, 모델명) — (주소, 키) 조합별 클라이언트 재사용.
    name 지정 시 그 임베딩 모델의 주소·키를 레지스트리에서 조회(run별 임베딩). 없으면 기본."""
    from openai import OpenAI
    url, resolved = embedding_endpoint()          # 기본(기본 임베딩)
    if name:
        u, _k = _lookup("embedding", name)
        if u is not None or _k is not None:        # 등록된 모델이면 그 주소로
            url, resolved = ((u or "").strip() or config.EMBED_URL), name
    key = api_key_for("embedding", resolved)
    ck = (url, key)
    if ck not in _emb_clients:
        # 타임아웃 필수 — 임베딩 엔드포인트가 멈추면 그래프 병합(메인 스레드)이 무한 대기.
        _emb_clients[ck] = OpenAI(base_url=url, api_key=key, timeout=config.LLM_TIMEOUT)
    return _emb_clients[ck], resolved


def probe_serving(timeout: float = 5.0) -> list:
    """기동 시 설정된 모델 서빙 연결 점검 — 채팅·임베딩 엔드포인트에 GET /models.
    [{role,url,model,ok,found,detail}] 반환, 예외 안 냄(서버 기동을 못 막게)."""
    curl, cname, ckey = chat_endpoint()      # 모델별 주소·키 해석 (레지스트리 우선)
    checks = [("chat", curl, cname, ckey)]
    try:
        eurl, emodel = embedding_endpoint()
        checks.append(("embedding", eurl, emodel, api_key_for("embedding", emodel)))
    except Exception:
        pass
    out, seen = [], set()
    for role, url, name, key in checks:
        if not url or (url, name) in seen:
            continue
        seen.add((url, name))
        rec = {"role": role, "url": url, "model": name or "",
               "ok": False, "found": False, "detail": ""}
        try:
            r = httpx.get(url.rstrip("/") + "/models",
                          headers={"Authorization": f"Bearer {key}"},
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
