"""모델·MCP 레지스트리와 에이전트 설정 — 저장 시 에이전트 캐시 무효화."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from web.deps import check_admin, clear_agents
from core import mcp_registry, model_registry, settings
from search.corpus_search import reload_index

router = APIRouter()


@router.get("/reload")
def reload_embeddings(request: Request):
    """임베딩 백필 진행 중 인덱스 갱신용 (서버 재시작 불필요) — 관리자 전용."""
    check_admin(request)
    return {"loaded": reload_index()}


@router.get("/admin/models/all")
def admin_models_all(request: Request):
    """관리자: 전체 모델 목록 (종류·주소·기본값·활성)."""
    check_admin(request)
    return {"models": model_registry.list_models(),
            "embedding_in_use": model_registry.embedding_endpoint()[1]}


class ModelAddIn(BaseModel):
    kind: str            # llm | embedding | reranker
    name: str            # served-model-name (호스트 /v1/models 값 그대로)
    base_url: str = ""   # 이 모델의 서빙 주소 — 빈값이면 역할별 .env(CHAT/EMBED/RERANK_URL)
    enabled: bool = True
    # 이 모델의 개발 키 — None(미전송)=기존 키 유지, ""=삭제(.env 전역 키 폴백), 그 외=설정
    api_key: str | None = None


@router.post("/admin/models/add")
def admin_model_add(inp: ModelAddIn, request: Request):
    """관리자: 모델 수동 등록/수정 (사내 vLLM처럼 sync가 못 닿는 호스트용).
    모델별 서빙 주소·개발 키를 여기서 설정 — 지정하면 그 모델 호출에 그 키를 쓴다."""
    check_admin(request)
    if inp.base_url and not inp.base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "base_url은 http(s):// 주소여야 합니다")
    try:
        model_registry.add_model(inp.kind, inp.name.strip(), inp.base_url.strip(),
                                 inp.enabled, inp.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    clear_agents()  # LLM 목록·주소·키가 바뀌었을 수 있음 → 다음 질문부터 재조립
    return {"ok": True}


class SyncIn(BaseModel):
    base_url: str = ""   # 지정 시 그 호스트만 조회, 빈값이면 설정된 채팅·임베딩·리랭커 호스트 전부
    test: bool = True    # 기본: 모델마다 실제 호출로 종류 판정(동작 기준). False면 이름만(빠름)
    api_key: str = ""    # 이 호스트의 개발 키 — 조회·판정에 쓰고 등록 모델에도 저장(빈값=.env)


@router.post("/admin/models/sync")
def admin_sync(inp: SyncIn, request: Request):
    """관리자: 모델 서빙에서 목록 동기화(등록) — 발견 모델 전부 base_url과 함께 등록.
    종류는 기본 이름 휴리스틱(즉시), test=True면 실제 호출로 판정. 설정 모델명이
    서빙에 없어도 등록은 진행(사람이 이후 기본값 선택). base_url 없으면 역할별 호스트 전부."""
    check_admin(request)
    if inp.base_url and not inp.base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "base_url은 http(s):// 주소여야 합니다")
    return model_registry.sync_from_serving(inp.base_url.strip(), inp.test,
                                            (inp.api_key or "").strip())


class SelectIn(BaseModel):
    kind: str   # llm | embedding | reranker
    name: str


@router.post("/admin/models/select")
def admin_select(inp: SelectIn, request: Request):
    """관리자: 종류별 기본 모델 지정. 임베딩 교체는 전체 재백필 필요."""
    check_admin(request)
    try:
        model_registry.set_default(inp.kind, inp.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    warn = None
    if inp.kind == "embedding":
        n = reload_index()  # 새 모델 벡터로 인덱스 재빌드 — 백필 전이면 lexical이 받침
        warn = (f"임베딩 기본값 변경됨 — 검색·dedup·경로 진입점이 즉시 이 모델을 씁니다. "
                f"현재 이 모델의 청크 벡터 {n}건 로드 (백필 배치가 불일치분을 자동 재임베딩, "
                "재백필 중엔 lexical이 받침). nodes.embedding 재백필과 dedup 임계값 "
                "재캘리브레이션은 별도 필요")
    return {"ok": True, "kind": inp.kind, "default": inp.name, "warning": warn}



@router.get("/admin/mcp")
def admin_mcp_list(request: Request):
    """관리자: 등록된 MCP 서버 목록."""
    check_admin(request)
    from core import mcp_registry
    return {"servers": mcp_registry.list_servers()}


class McpIn(BaseModel):
    name: str
    transport: str = "streamable_http"  # streamable_http | sse | stdio
    url: str = ""       # http 계열: MCP 엔드포인트 주소
    command: str = ""   # stdio: 실행 파일
    enabled: bool = True


@router.post("/admin/mcp")
def admin_mcp_upsert(inp: McpIn, request: Request):
    """관리자: MCP 서버 등록/수정 — 저장 즉시 다음 질문부터 도구가 조립된다."""
    check_admin(request)
    if not inp.name.strip():
        raise HTTPException(400, "name은 필수입니다")
    from core import mcp_registry
    try:
        mcp_registry.upsert(inp.name, inp.transport, inp.url, inp.command, inp.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    clear_agents()  # 다음 질문부터 새 MCP 구성으로 조립
    return {"ok": True, "note": "다음 질문부터 반영 (에이전트 재조립)"}


@router.get("/admin/agent-settings")
async def admin_agent_settings_get(request: Request):
    """관리자: 에이전트 전역 설정 조회 — 시스템 프롬프트·MCP 사용·도구별 활성."""
    check_admin(request)
    from agent.agent import default_system_prompt, discover_tools
    from agent.triage import default_triage_prompt
    from agent.identity import identity
    st = settings.get_all()
    dname, dintro, dscope = identity({})  # 설정 무시한 config 기본값(placeholder용)
    return {"system_prompt": (st.get("agent_system_prompt") or ""),
            "default_prompt": default_system_prompt(st),
            "triage_prompt": (st.get("agent_triage_prompt") or ""),
            "default_triage_prompt": default_triage_prompt(),
            "agent_name": (st.get("agent_name") or ""),
            "agent_intro": (st.get("agent_intro") or ""),
            "agent_scope": (st.get("agent_scope") or ""),
            "default_name": dname, "default_intro": dintro, "default_scope": dscope,
            "mcp_enabled": st.get("agent_mcp_enabled", "1") != "0",
            "no_think": st.get("agent_no_think", "") == "1",
            "block_hanzi": st.get("agent_block_hanzi", "") == "1",
            "disabled_tools": [t.strip() for t in
                               (st.get("agent_disabled_tools") or "").split(",")
                               if t.strip()],
            "tools": await discover_tools()}


class AgentSettingsIn(BaseModel):
    system_prompt: str = ""      # 빈값 = 코드 기본 프롬프트 사용
    triage_prompt: str = ""      # 빈값 = 코드 기본 라우터 프롬프트 사용
    agent_name: str = ""         # 빈값 = config 기본(정체성 — 이름/소개/지원범위)
    agent_intro: str = ""
    agent_scope: str = ""
    mcp_enabled: bool = True     # DataHub MCP 전역 on/off
    no_think: bool = False       # True=추론(생각) 출력 끔 — 빠르지만 복잡한 추론엔 품질↓
    block_hanzi: bool = False    # True=한자(중국어) 차단 (guided_regex) — 생성 단계 제외
    disabled_tools: list[str] = []  # 비활성 도구 이름 목록 (builtin·MCP 공통)


@router.post("/admin/agent-settings")
def admin_agent_settings_set(inp: AgentSettingsIn, request: Request):
    """관리자: 에이전트 전역 설정 저장 — 캐시를 비워 다음 질문부터 재조립."""
    check_admin(request)
    from core import settings
    if len(inp.system_prompt) > 8000 or len(inp.triage_prompt) > 8000:
        raise HTTPException(400, "프롬프트는 각 8000자 이내여야 합니다")
    settings.set_many({
        "agent_system_prompt": inp.system_prompt.strip(),
        "agent_triage_prompt": inp.triage_prompt.strip(),
        "agent_name": inp.agent_name.strip(),
        "agent_intro": inp.agent_intro.strip(),
        "agent_scope": inp.agent_scope.strip(),
        "agent_mcp_enabled": "" if inp.mcp_enabled else "0",
        "agent_no_think": "1" if inp.no_think else "",
        "agent_block_hanzi": "1" if inp.block_hanzi else "",
        "agent_disabled_tools": ",".join(
            t.strip() for t in inp.disabled_tools if t.strip()),
    })
    clear_agents()  # 모델별 에이전트 캐시 무효화 — 다음 요청이 새 설정으로 조립
    return {"ok": True, "note": "다음 질문부터 반영 (에이전트 재조립)"}

