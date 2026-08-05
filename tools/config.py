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
ADMIN_TOKEN = _get("ADMIN_TOKEN", "poc-admin")

# --- 인증 (SSO, app/auth.py) — docs/integration.md 접점 1 ---
# none    = 인증 없음(로컬 개발 기본)
# header  = 사내 기본 — 전단 SSO가 인증을 끝내고 헤더로 넘겨주는 userId·role 2개만 소비.
#           전제: 앱에 전단 우회 직접 접근 불가(헤더 위조 방지는 네트워크가 보장).
# keycloak= 전단 프록시가 없는 환경 — 앱이 직접 OIDC 코드 플로우 + 서명 세션 쿠키.
AUTH_MODE = _get("AUTH_MODE", "none").strip().lower()
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
