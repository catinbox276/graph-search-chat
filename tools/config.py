"""중앙 설정 — .env 하나로 모든 환경변수를 제어한다.

이 모듈을 import하면 프로젝트 루트의 .env를 os.environ에 로드한다(있을 때).
다른 모듈은 하드코딩 대신 여기서 값을 가져온다. 우선순위: 실제 환경변수 > .env > 기본값.

역할별 모델 엔드포인트: 사내 vLLM은 모델마다 별도 호스트라 URL을 3개로 분리한다.
CHAT/EMBED/RERANK_URL이 비어 있으면 단일 MODEL_URL로 폴백한다(LM Studio 단일 서빙 호환).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """.env를 os.environ에 로드. python-dotenv가 있으면 그걸 쓰고,
    없으면 최소 파서로 폴백(KEY=VALUE, # 주석, 따옴표 제거). 실제 환경변수는 덮지 않는다."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ModuleNotFoundError:
        pass
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)  # 이미 설정된 실제 환경변수 우선


_load_env(ROOT / ".env")


def _get(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def _getf(key: str, default: float) -> float:
    try:
        return float(_get(key, str(default)))
    except ValueError:
        return default


def _geti(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


# --- Oracle ---
ORACLE_DSN = _get("ORACLE_DSN", "localhost:1521/FREEPDB1")
ORACLE_USER = _get("ORACLE_USER", "system")
ORACLE_PASSWORD = _get("ORACLE_PASSWORD", "poc1234")

# --- 모델 서빙 (OpenAI 호환) ---
MODEL_API_KEY = _get("MODEL_API_KEY", "lm-studio")
_MODEL_URL = _get("MODEL_URL", "http://127.0.0.1:1234/v1")  # 단일 서빙 폴백(LM Studio)
CHAT_URL = _get("CHAT_URL", _MODEL_URL)
EMBED_URL = _get("EMBED_URL", _MODEL_URL)
RERANK_URL = _get("RERANK_URL", _MODEL_URL)
CHAT_MODEL = _get("MODEL_NAME", "qwen/qwen3.6-35b-a3b")
EMBED_MODEL = _get("EMBED_MODEL", "text-embedding-qwen3-embedding-0.6b")
RERANK_MODEL = _get("RERANK_MODEL", "")

# --- DataHub / 관리 ---
DATAHUB_GMS_URL = _get("DATAHUB_GMS_URL", "http://localhost:8080")

# --- MCP 기본 서버 (tools/mcp_registry.py) ---
# 주소를 넣으면 mcp_registry에 자동 시드(최초 1회 — 이후 관리 페이지에서 수정/비활성 가능).
# 사내 HTTP MCP를 배포 env만으로 등록할 때 사용. 빈값=시드 없음.
MCP_DEFAULT_NAME = _get("MCP_DEFAULT_NAME", "mcp")
MCP_DEFAULT_URL = _get("MCP_DEFAULT_URL", "")
# streamable_http(표준 MCP) | rest(사내 GET /tools + POST /call) | sse
MCP_DEFAULT_TRANSPORT = _get("MCP_DEFAULT_TRANSPORT", "streamable_http").strip().lower()

# --- 인증 (SSO, app/auth.py) — docs/integration.md 접점 1 ---
# none    = 인증 없음(로컬 개발 기본)
# header  = 사내 기본 — 전단 SSO가 인증을 끝내고 헤더로 넘겨주는 userId·role 2개만 소비.
#           전제: 앱에 전단 우회 직접 접근 불가(헤더 위조 방지는 네트워크가 보장).
# keycloak= 전단 프록시가 없는 환경 — 앱이 직접 OIDC 코드 플로우 + 서명 세션 쿠키.
# gateway = 게이트웨이 SSO 미들웨어 — 요청의 JWT를 게이트웨이 검증 API로 보내
#           JSON({userId, roles})을 받는다. 앱이 아는 주소는 GATEWAY_AUTH_URL 하나.
AUTH_MODE = _get("AUTH_MODE", "none").strip().lower()
GATEWAY_AUTH_URL = _get("GATEWAY_AUTH_URL", "")           # 검증 API (gateway 모드 필수)
GATEWAY_TOKEN_HEADER = _get("GATEWAY_TOKEN_HEADER", "Authorization")  # JWT가 실려오는 헤더
# 검증 API 호출: POST {GATEWAY_TOKEN_FIELD: <JWT>} — 사내 스펙 (2026-08-06 확정)
GATEWAY_TOKEN_FIELD = _get("GATEWAY_TOKEN_FIELD", "accessToken")  # 요청 바디의 토큰 필드명
# 응답 필드는 점 표기 중첩 경로 지원 — 사내 응답: {"result": {"userId":…, "role":…}}
GATEWAY_USER_FIELD = _get("GATEWAY_USER_FIELD", "result.userId")
GATEWAY_ROLE_FIELD = _get("GATEWAY_ROLE_FIELD", "result.role")
GATEWAY_CACHE_TTL = _geti("GATEWAY_CACHE_TTL", 60)         # 검증 결과 캐시(초) — 게이트웨이 부하 방지

# --- REST 도구 서버 어댑터 (tools/rest_tools.py — mcp_registry transport='rest') ---
REST_TOOL_TIMEOUT = _getf("REST_TOOL_TIMEOUT", 30.0)  # /tools·/call 호출 타임아웃(초)
GATEWAY_TIMEOUT = _getf("GATEWAY_TIMEOUT", 5.0)            # 검증 API 호출 타임아웃(초) — 초과 시 미인증(fail-closed)
SSO_USER_HEADER = _get("SSO_USER_HEADER", "X-Auth-Request-User")    # userId 헤더명
SSO_ROLE_HEADER = _get("SSO_ROLE_HEADER", "X-Auth-Request-Groups")  # role 헤더명(구분자 ,;공백)
KEYCLOAK_PUBLIC_URL = _get("KEYCLOAK_PUBLIC_URL", "http://localhost:8080").rstrip("/")
KEYCLOAK_INTERNAL_URL = _get("KEYCLOAK_INTERNAL_URL", KEYCLOAK_PUBLIC_URL).rstrip("/")
KEYCLOAK_REALM = _get("KEYCLOAK_REALM", "gsc")
OIDC_CLIENT_ID = _get("OIDC_CLIENT_ID", "gsc-app")
OIDC_CLIENT_SECRET = _get("OIDC_CLIENT_SECRET", "")
OIDC_ADMIN_ROLE = _get("OIDC_ADMIN_ROLE", "gsc-admin")  # 이 역할 보유 시 관리자(header·keycloak 공통)
APP_BASE_URL = _get("APP_BASE_URL", "http://localhost:8500").rstrip("/")  # redirect_uri 기준
SESSION_SECRET = _get("SESSION_SECRET", "gsc-poc-session-secret")  # 사내 전환 시 Secret로
SESSION_MAX_AGE = _geti("SESSION_MAX_AGE", 28800)  # 로그인 쿠키 수명(초) — 기본 8시간

# --- LLM 호출 ---
LLM_TEMPERATURE = _getf("LLM_TEMPERATURE", 0.0)

# --- 원천 테이블 접근 제어 (tools/source_registry.py) ---
# 소스로 등록·조회·적재할 수 있는 원천 테이블 화이트리스트 (쉼표구분, 대소문자 무관).
# 빈값 = 제한 없음(PoC 기본). 사내 전환 시 허용 테이블만 나열 — 목록 밖은 브라우저
# 조회·등록·야간 적재 전부 차단된다.
SOURCE_TABLE_ALLOWLIST = frozenset(
    t.strip().upper() for t in _get("SOURCE_TABLE_ALLOWLIST", "").split(",") if t.strip())

# --- 청크 임베딩 (scripts/chunk_corpus.py — docs/schema.md §5) ---
# 문서를 청크로 잘라 시맨틱 검색이 긴 본문 뒷부분도 잡게 한다. app_settings가 우선.
CHUNK_CHARS = _geti("CHUNK_CHARS", 1200)    # 청크 크기(자) — 이하면 청크 1개
CHUNK_OVERLAP = _geti("CHUNK_OVERLAP", 150)  # 인접 청크 겹침(자)

# --- 하이브리드 검색 (tools/blog_search.py) ---
RRF_K = _geti("RRF_K", 60)                        # RRF 융합 상수
SEARCH_TOP_LEXICAL = _geti("SEARCH_TOP_LEXICAL", 30)   # Oracle Text 후보 수
SEARCH_TOP_SEMANTIC = _geti("SEARCH_TOP_SEMANTIC", 30)  # 임베딩 코사인 후보 수

# --- 경로 제안 진입점 매칭 (tools/path_suggest.py) ---
PATH_SIM_ENTRY = _getf("PATH_SIM_ENTRY", 0.60)

# --- 그래프 dedup 병합 임계값 (poc/graph_pipeline.py) ---
# 캘리브레이션: 같은 의도 0.81~0.98, 다른 의도 0.34~0.46 (파일 상단 주석 참조)
DEDUP_SIM_HIGH = _getf("DEDUP_SIM_HIGH", 0.92)     # 이상이면 LLM 확인 없이 즉시 병합
DEDUP_SIM_THRESHOLD = _getf("DEDUP_SIM_THRESHOLD", 0.70)  # 후보 하한(이 구간은 LLM 확인)
# 자동 병합 가드 — 임베딩 단독 즉시 병합은 업계에 없음 (Graphiti Jaccard, Neo4j 편집거리 AND).
# 짧은 이름은 임베딩이 불안정하므로 자동 병합 제외(엔트로피 게이트 유사), 문자 유사도 AND 조건.
DEDUP_SHORT_NAME_CHARS = _geti("DEDUP_SHORT_NAME_CHARS", 12)  # 미만이면 자동 병합 대신 LLM
DEDUP_CHAR_RATIO = _getf("DEDUP_CHAR_RATIO", 0.4)  # difflib ratio 하한 (미달이면 LLM으로)
DEDUP_SELECT_MAX = _geti("DEDUP_SELECT_MAX", 8)    # LLM 후보 선택 프롬프트에 넣는 최대 후보 수

# --- 그래프 유지보수 (poc/graph_maintenance.py) ---
MAINT_LOW_COUNT = _geti("MAINT_LOW_COUNT", 2)      # 패스1: 이 이하 통행이면 흡수 후보
MAINT_ABSORB_COUNT = _geti("MAINT_ABSORB_COUNT", 1)  # 패스2: 이 이하 통행 잎만 흡수
MAINT_MIN_AGE_DAYS = _geti("MAINT_MIN_AGE_DAYS", 14)  # 패스2 기준(--age-days로 오버라이드)

# --- 시간 감쇠 (poc/graph_maintenance.py 패스3) — design §2 운영 규칙 5 ---
# 낡은 접근법은 지우지 않고 가라앉힌다. 신선도는 조치(3층 접근법)에 붙는다.
MAINT_DECAY_HALF_LIFE_DAYS = _getf("MAINT_DECAY_HALF_LIFE_DAYS", 90.0)  # 반감기
MAINT_DECAY_GRACE_DAYS = _getf("MAINT_DECAY_GRACE_DAYS", 30.0)  # 이 유휴 기간까진 감쇠 없음
MAINT_DECAY_FLOOR = _getf("MAINT_DECAY_FLOOR", 0.1)  # 감쇠 하한 배수 (0이면 사실상 삭제라 금지)

# --- 실서비스 게이트 행동 신호 (poc/graph_pipeline.py) — design §3 보강 ---
SIG_REPEAT_SIM = _getf("SIG_REPEAT_SIM", 0.85)      # 재질문·재발 판정 질문 유사도
SIG_TOPIC_MOVE_SIM = _getf("SIG_TOPIC_MOVE_SIM", 0.50)  # 이보다 멀어지면 화제 전진
SIG_HASTY_RATIO = _getf("SIG_HASTY_RATIO", 0.3)     # 턴 간격이 중앙값의 이 배수 미만이면 조급함
RECUR_DAYS = _geti("RECUR_DAYS", 7)                 # 재발 판정 창 (일) — design §7 미해결 값의 초기치

# --- 문서 그래프 구조화 (poc/doc_pipeline.py) ---
# 아래 3개는 기본값 — 운영 중 변경은 app_settings(관리 UI 📚 소스 > 전처리 설정)가 우선
DOC_EXTRACT_LIMIT = _geti("DOC_EXTRACT_LIMIT", 200)  # 실행당 처리 문서 수 (야간 반복으로 소진)
DOC_CONCURRENCY = _geti("DOC_CONCURRENCY", 16)       # LLM 판정 동시 요청 수 (vLLM 여유 있으면 UI에서 상향)
DOC_BODY_CHARS = _geti("DOC_BODY_CHARS", 3000)       # 판정에 넣는 본문 길이(자)
DOC_PACK_TOKENS = _geti("DOC_PACK_TOKENS", 0)        # 0=문서 1건씩 / N=입력 N토큰 예산으로 묶음 판정
DOC_NO_THINK = _geti("DOC_NO_THINK", 1)              # 1=판정 시 추론(생각) 출력 끔 — A/B 실측 7~8배 빠름, 품질 동일

# --- 보조 LLM 판정 (같음/다름 이지선다 등 — graph_pipeline.llm_same) ---
# 추론 모델의 생각 출력을 끈다 (Qwen3 chat_template_kwargs). 이지선다는 생각 없이도
# 품질이 유지되고(문서 판정 A/B로 실측), 켜두면 dedup 확인 1건에 20~30초가 걸린다.
LLM_AUX_NO_THINK = _geti("LLM_AUX_NO_THINK", 1)

# --- 임베딩 백필 (scripts/embed_corpus.py) ---
EMBED_BATCH = _geti("EMBED_BATCH", 64)
EMBED_CONCURRENCY = _geti("EMBED_CONCURRENCY", 4)  # 임베딩 서빙 동시 요청 수
EMBED_TEXT_CHARS = _geti("EMBED_TEXT_CHARS", 300)  # 임베딩 대상 텍스트: 제목+본문 앞 N자

# --- 코퍼스 빌드 (scripts/build_corpus.py) ---
CORPUS_TOP_N = _geti("CORPUS_TOP_N", int(_get("TOP_N", "0")))  # 0=전체 (구 TOP_N 호환)

# --- Oracle 커넥션 풀 (전 모듈 공통) ---
ORACLE_POOL_MIN = _geti("ORACLE_POOL_MIN", 1)
ORACLE_POOL_MAX = _geti("ORACLE_POOL_MAX", 4)
ORACLE_POOL_INCREMENT = _geti("ORACLE_POOL_INCREMENT", 1)

# --- Oracle Text (scripts/load_oracle.py) ---
# WORLD_LEXER=한국어/영어 혼합 자동. 사내 19c 한국어 정밀도는 KOREAN_MORPH_LEXER.
ORACLE_TEXT_LEXER = _get("ORACLE_TEXT_LEXER", "WORLD_LEXER")

# --- Oracle 드라이버 모드 ---
# thin = 순수 파이썬(기본, Instant Client 불필요) / thick = Oracle Instant Client 사용
# (레거시 NLS 문자셋·Kerberos/wallet·AQ/CQN 등 thick 전용 기능이 필요할 때)
ORACLE_MODE = _get("ORACLE_MODE", "thin").strip().lower()
ORACLE_CLIENT_LIB_DIR = _get("ORACLE_CLIENT_LIB_DIR", "")  # thick 경로. 빈값이면 ldconfig/PATH 탐색


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
