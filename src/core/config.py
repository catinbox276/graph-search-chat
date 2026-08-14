"""중앙 설정 — .env 하나로 모든 환경변수를 제어한다 (pydantic-settings).

다른 모듈은 하드코딩 대신 `from core import config`로 값을 가져온다.
우선순위: 실제 환경변수 > .env > 코드 기본값. 빈 문자열 env는 미설정으로 취급(기본값 적용).
타입 캐스팅·검증·.env 파싱은 전부 pydantic-settings가 담당 — 수동 파서 없음.

역할별 모델 엔드포인트: 사내 vLLM은 모델마다 별도 호스트라 URL을 3개로 분리한다.
CHAT/EMBED/RERANK_URL이 비어 있으면 단일 MODEL_URL로 폴백한다(LM Studio 단일 서빙 호환).
"""
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent          # = src/
REPO_ROOT = ROOT.parent                                # 레포 루트 (.env·data 위치)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8",
        env_ignore_empty=True,   # 빈값 env = 미설정 → 기본값/폴백 적용
        extra="ignore")

    # --- Oracle ---
    ORACLE_DSN: str = "localhost:1521/FREEPDB1"
    ORACLE_USER: str = "system"
    ORACLE_PASSWORD: str = "poc1234"

    # --- 모델 서빙 (OpenAI 호환) ---
    MODEL_API_KEY: str = "lm-studio"
    MODEL_URL: str = "http://127.0.0.1:1234/v1"   # 단일 서빙 폴백(LM Studio)
    CHAT_URL: str = ""                            # 비면 MODEL_URL로 폴백 (아래 validator)
    EMBED_URL: str = ""
    RERANK_URL: str = ""
    CHAT_MODEL: str = Field("qwen/qwen3.6-35b-a3b",
                            validation_alias=AliasChoices("MODEL_NAME", "CHAT_MODEL"))
    EMBED_MODEL: str = "text-embedding-qwen3-embedding-0.6b"
    RERANK_MODEL: str = ""

    # --- DataHub (배치 적재 전용 — ingestion/ingest_bird.py) ---
    # 에이전트 연결은 GMS 직결이 아니라 MCP-over-REST(관리 페이지 등록) — 여긴 시드 스크립트용
    DATAHUB_GMS_URL: str = "http://localhost:8080"

    # --- MCP 기본 서버 (core/mcp_registry.py) ---
    # 주소를 넣으면 mcp_registry에 자동 시드(최초 1회 — 이후 관리 페이지에서 수정/비활성).
    # 사내 HTTP MCP를 배포 env만으로 등록할 때 사용. 빈값=시드 없음.
    MCP_DEFAULT_NAME: str = "mcp"
    MCP_DEFAULT_URL: str = ""
    # streamable_http(표준 MCP) | sse | stdio
    MCP_DEFAULT_TRANSPORT: str = "streamable_http"

    # --- 인증 (자체 계정 — web/auth.py) ---
    # 관리자 = 환경 설정 계정 1개 (DB 아님 — 분실 시 env 수정으로 복구).
    # 일반 계정 = /login에서 가입(id+pw) 후 관리자 승인(app_users.approved='Y')돼야 로그인.
    ADMIN_ID: str = "admin"
    ADMIN_PASSWORD: str = ""      # 비면 기동 실패 (모듈 하단 fail-fast)
    SESSION_SECRET: str = ""      # 비면 기동 실패
    SESSION_MAX_AGE: int = 28800  # 로그인 쿠키 수명(초) — 기본 8시간

    # --- 에이전트 정체성·범위 (시스템 프롬프트 placeholder — agent/agent.py) ---
    AGENT_NAME: str = "지식그래프 챗"   # 어시스턴트 이름
    AGENT_INTRO: str = ""              # 자기소개 문구 (빈값 = 이름 기반 기본 문구)
    AGENT_SCOPE: str = ("사내 데이터(테이블·스키마·조인·리니지) 조회, "
                        "사내 노하우·문제해결 지식 검색")  # 지원 범위 서술

    # --- LLM 호출 ---
    LLM_TEMPERATURE: float = 0.0
    LLM_TIMEOUT: float = 180.0  # LLM 요청 타임아웃(초) — 멈춘 요청이 파이프라인을 무한 대기시키지 않게

    # --- 원천 테이블 접근 제어 (ingestion/source_registry.py) ---
    # 소스로 등록·조회·적재할 수 있는 원천 테이블 화이트리스트 (쉼표구분, 대소문자 무관).
    # 빈값 = 제한 없음(PoC 기본). 사내 전환 시 허용 테이블만 나열 — 목록 밖은 차단.
    SOURCE_TABLE_ALLOWLIST: str = ""  # 모듈 하단에서 frozenset으로 변환해 노출

    # --- 청킹 (ingestion/chunk_corpus.py — 운영은 app_settings가 우선) ---
    CHUNK_CHARS: int = 1200    # 청크 크기(자) — 이하면 청크 1개
    CHUNK_OVERLAP: int = 150   # 인접 청크 겹침(자)

    # --- 하이브리드 검색 (search/corpus_search.py) ---
    RRF_K: int = 60                  # RRF 융합 상수
    SEARCH_TOP_LEXICAL: int = 30     # 렉시컬(FTS5) 후보 수
    SEARCH_TOP_SEMANTIC: int = 30    # 임베딩 코사인 후보 수
    INMEM_RELOAD_SECS: int = 60      # 인메모리 인덱스 버전 확인 주기(초)

    # --- 경로 제안 진입점 매칭 (search/path_suggest.py) ---
    PATH_SIM_ENTRY: float = 0.60

    # --- 그래프 dedup 병합 임계값 (graph/graph_pipeline/merge.py) ---
    # 캘리브레이션: 같은 의도 0.81~0.98, 다른 의도 0.34~0.46 (merge.py 상단 주석)
    DEDUP_SIM_HIGH: float = 0.92       # 이상이면 LLM 확인 없이 즉시 병합
    DEDUP_SIM_THRESHOLD: float = 0.70  # 후보 하한 (이 구간은 LLM 확인)
    # 자동 병합 가드 — 임베딩 단독 즉시 병합은 업계에 없음 (Graphiti Jaccard, Neo4j 편집거리 AND)
    DEDUP_SHORT_NAME_CHARS: int = 12   # 미만이면 자동 병합 대신 LLM
    DEDUP_CHAR_RATIO: float = 0.4      # difflib ratio 하한 (미달이면 LLM으로)
    DEDUP_SELECT_MAX: int = 8          # LLM 후보 선택 프롬프트에 넣는 최대 후보 수

    # --- 그래프 유지보수 (graph/graph_maintenance.py) ---
    MAINT_LOW_COUNT: int = 2       # 패스1: 이 이하 통행이면 흡수 후보
    MAINT_ABSORB_COUNT: int = 1    # 패스2: 이 이하 통행 잎만 흡수
    MAINT_MIN_AGE_DAYS: int = 14   # 패스2 기준 (--age-days로 오버라이드)

    # --- 시간 감쇠 (유지보수 패스3) — design §2 운영 규칙 5 ---
    # 낡은 접근법은 지우지 않고 가라앉힌다. 신선도는 조치(3층 접근법)에 붙는다.
    MAINT_DECAY_HALF_LIFE_DAYS: float = 90.0  # 반감기
    MAINT_DECAY_GRACE_DAYS: float = 30.0      # 이 유휴 기간까진 감쇠 없음
    MAINT_DECAY_FLOOR: float = 0.1            # 감쇠 하한 배수 (0이면 사실상 삭제라 금지)

    # --- 실서비스 게이트 행동 신호 (graph/graph_pipeline/gate.py) — design §3 보강 ---
    SIG_REPEAT_SIM: float = 0.85      # 재질문·재발 판정 질문 유사도
    SIG_TOPIC_MOVE_SIM: float = 0.50  # 이보다 멀어지면 화제 전진
    SEG_SPLIT_SIM: float = 0.35       # 인접 질문이 이보다 멀면 태스크 경계로 분할
    SIG_HASTY_RATIO: float = 0.3      # 턴 간격이 중앙값의 이 배수 미만이면 조급함
    RECUR_DAYS: int = 7               # 재발 판정 창(일) — design §7 미해결 값의 초기치

    # --- 문서 그래프 구조화 (graph/doc_pipeline.py — 운영은 app_settings가 우선) ---
    DOC_EXTRACT_LIMIT: int = 200   # 실행당 처리 문서 수 (야간 반복으로 소진)
    DOC_CONCURRENCY: int = 16      # LLM 판정 동시 요청 수
    DOC_BODY_CHARS: int = 3000     # 판정에 넣는 본문 길이(자, 0=전체)
    DOC_PACK_TOKENS: int = 0       # 0=문서 1건씩 / N=입력 N토큰 예산으로 묶음 판정
    DOC_NO_THINK: int = 1          # 1=판정 시 생각 출력 끔 — A/B 실측 7~8배 빠름, 품질 동일

    # --- 보조 LLM 판정 (같음/다름 이지선다 — graph_pipeline/llm.py) ---
    # 추론 모델의 생각 출력을 끈다 (Qwen3 chat_template_kwargs). 이지선다는 생각 없이도
    # 품질 유지(실측), 켜두면 dedup 확인 1건에 20~30초.
    LLM_AUX_NO_THINK: int = 1

    # --- 임베딩 백필 (ingestion/embed_corpus.py) ---
    EMBED_BATCH: int = 64
    EMBED_CONCURRENCY: int = 4    # 임베딩 서빙 동시 요청 수
    EMBED_TEXT_CHARS: int = 300   # 임베딩 대상 텍스트: 제목+본문 앞 N자

    # --- 코퍼스 빌드 (ingestion/build_corpus.py) ---
    CORPUS_TOP_N: int = Field(0, validation_alias=AliasChoices("CORPUS_TOP_N", "TOP_N"))  # 0=전체

    # --- Oracle 커넥션 풀 (전 모듈 공통) ---
    ORACLE_POOL_MIN: int = 1
    ORACLE_POOL_MAX: int = 4
    ORACLE_POOL_INCREMENT: int = 1

    # --- 활동 로그 보관 (core/events.py) — 전부 쌓되 이 일수 지난 건 야간 회전 삭제 ---
    EVENTS_RETAIN_DAYS: int = 180

    # --- REST 도구 서버 (core/rest_tools.py — 사내 MCP-over-REST) ---
    REST_TOOL_TIMEOUT: float = 30.0

    # --- Oracle 드라이버 모드 ---
    # thin = 순수 파이썬(기본, Instant Client 불필요) / thick = Instant Client 사용
    # (레거시 NLS 문자셋·Kerberos/wallet·AQ/CQN 등 thick 전용 기능이 필요할 때)
    ORACLE_MODE: str = "thin"
    ORACLE_CLIENT_LIB_DIR: str = ""  # thick 경로. 빈값이면 ldconfig/PATH 탐색

    @model_validator(mode="after")
    def _normalize(self):
        """빈 URL의 MODEL_URL 폴백 + 소문자 정규화 — 기존 동작 그대로."""
        self.CHAT_URL = self.CHAT_URL or self.MODEL_URL
        self.EMBED_URL = self.EMBED_URL or self.MODEL_URL
        self.RERANK_URL = self.RERANK_URL or self.MODEL_URL
        self.MCP_DEFAULT_TRANSPORT = self.MCP_DEFAULT_TRANSPORT.strip().lower()
        self.ORACLE_MODE = self.ORACLE_MODE.strip().lower()
        return self


_settings = Settings()
if not _settings.ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD가 비어 있습니다 — .env에 관리자 비밀번호를 설정하세요")
if not _settings.SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET이 비어 있습니다 — 로그인 쿠키 서명 키를 설정하세요")

# 모듈 속성으로 노출 — 기존 사용처(`config.CHAT_URL` 등) 전부 불변
globals().update(_settings.model_dump())
# 쉼표 문자열 → frozenset (사용처는 `T in config.SOURCE_TABLE_ALLOWLIST`)
SOURCE_TABLE_ALLOWLIST = frozenset(
    t.strip().upper() for t in _settings.SOURCE_TABLE_ALLOWLIST.split(",") if t.strip())


def _init_oracle_mode():
    """thick 모드면 프로세스당 1회 Instant Client 초기화 — 어떤 접속·풀 생성보다 먼저.
    config가 전 모듈에서 가장 먼저 import되므로 여기가 유일하게 안전한 위치."""
    if ORACLE_MODE != "thick":
        return
    try:
        import oracledb
    except ModuleNotFoundError:
        return  # oracledb 없는 경량 소비자(build_corpus 등) — 실제 접속 시점에 어차피 에러
    kwargs = {"lib_dir": ORACLE_CLIENT_LIB_DIR} if ORACLE_CLIENT_LIB_DIR else {}
    try:
        oracledb.init_oracle_client(**kwargs)
    except Exception as e:
        if "already" in str(e).lower():  # 중복 초기화(모듈 재로드 등)는 무시
            return
        raise RuntimeError(
            f"Oracle thick 모드 초기화 실패: {e}\n"
            f"→ Instant Client 설치 후 ORACLE_CLIENT_LIB_DIR 지정, 또는 ORACLE_MODE=thin"
        ) from e


_init_oracle_mode()
