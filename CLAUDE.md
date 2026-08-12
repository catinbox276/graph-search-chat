# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# graph-search-chat

사용자(주로 데이터 분석가)의 질문에 에이전트가 두 도구 — **DataHub MCP**(복잡한 데이터 조회·조인·조립)와 **사내 블로그 검색**(문제해결 노하우 글) — 로 답하고, 그 대화 로그에서 온톨로지/지식그래프를 자동 추출·병합해, 신규 사용자에게 검증된 문제 해결 경로를 제안하는 시스템. **PoC 파이프라인 실증 완료** — self-play 47세션으로 추출·병합·가중치·실패 표식 전 순환 동작 확인 (docs/poc-results.md).

## 디렉토리 구조 (도메인축)

역할이 아니라 **도메인**으로 나눈다 (호출방식·프로젝트단계로 나누지 않음). "새 기능 어디 넣지?"가 3초 안에 결정되는 게 기준.

```
src/        애플리케이션 코드 (파이썬 루트 — PYTHONPATH=src). import는 `from core import config`처럼 src 기준.
  web/        FastAPI HTTP 계층 — server.py, routers/*, auth.py, deps.py, *.html, shell.css
  agent/      에이전트 조립 — agent.py (DeepAgents, 툴 와이어링)
  search/     검색 도메인 — corpus_search, inmemory_index, ko_tokenize, path_suggest
  ingestion/  적재 도메인 — source_registry + 코퍼스 빌드/적재/청킹/임베딩/토큰화 배치
  graph/      지식그래프 도메인 — graph_pipeline, doc_pipeline, graph_maintenance, selfplay, tasks.yaml
  core/       공용 인프라 — config, db, models, events, settings, model_registry, mcp_registry, oracle_checkpointer, session_ctx, rest_tools
deploy/     인프라 — Dockerfile, k8s/(base·cluster·oracle·ingress)
docs/       설계·기획·구현 문서 / .env·requirements.txt는 레포 루트
```

규칙(불변식): **도메인 외부에서는 패키지 루트만 import**(`from core import config`, 깊은 경로 지양). **`common.py`/`utils.py`/번호 접미사 금지** — 커지면 유스케이스 단위로 쪼갠다(줄 수 아님, 변경 이유 2개 이상이면 분리). 파일이 커지면 `search/corpus_search.py` → `search/corpus_search/` 패키지로 승격하고 `__init__`에서 re-export(외부 import 경로 불변). `core/`는 모든 도메인이 의존하는 공용만 — 도메인 로직 금지.

## 커맨드

테스트 스위트·린터는 없다 (PoC). 검증은 실행 + Oracle 조회 + `/stats`로 한다. 파이썬은 `.venv/bin/python` 사용.

```bash
# 앱 서버 (채팅 UI :8500, 그래프 뷰 :8500/graph)
PYTHONPATH=src .venv/bin/uvicorn web.server:app --port 8500

# 에이전트 단발 실행 (서버 없이 CLI로 질문 1건)
PYTHONPATH=src .venv/bin/python -m agent.agent "financial DB에서 계좌 테이블 뭐 있어?"

# self-play 세션 생성 (재실행 시 완료분 스킵하고 이어함)
PYTHONPATH=src .venv/bin/python -m graph.selfplay [--only R1,R2]

# 그래프 파이프라인: sessions → 게이트 판정 → 4계층 추출 → nodes/edges 병합
PYTHONPATH=src .venv/bin/python -m graph.graph_pipeline

# 그래프 유지보수 (멱등): 저빈도 형제 통합 + 오래된 잎 흡수
PYTHONPATH=src .venv/bin/python -m graph.graph_maintenance [--age-days 14]

# 데이터 준비 (1회, 순서대로): 코퍼스 빌드 → Oracle 적재 → 임베딩 백필 → BIRD 적재
PYTHONPATH=src python3 -m ingestion.build_corpus && PYTHONPATH=src python3 -m ingestion.load_oracle \
  && PYTHONPATH=src python3 -m ingestion.embed_corpus && PYTHONPATH=src python3 -m ingestion.ingest_bird

# 원천 테이블 증분 적재: source_registry 등록분 → corpus_docs (야간 03:10과 동일, 멱등)
PYTHONPATH=src python3 -m ingestion.ingest_sources

# 문서 청킹: 신규·갱신 문서 → corpus_chunks (야간 03:15과 동일, 멱등)
PYTHONPATH=src python3 -m ingestion.chunk_corpus

# 문서 그래프 구조화: 도메인 지정 소스의 corpus_docs를 LLM 판정·그래프 병합 (야간 03:40과 동일)
PYTHONPATH=src .venv/bin/python -m graph.doc_pipeline [--limit N]

# 배포 (전제: k8s + LM Studio :1234 — 상세 절차는 docs/implementation.md "실행 방법")
docker build -f deploy/Dockerfile -t graph-search-chat:latest .
kubectl apply -f deploy/k8s/oracle.yaml          # Oracle StatefulSet
kubectl apply -k deploy/k8s/base                 # standalone (복제본 1) + CronJob
kubectl apply -k deploy/k8s/cluster              # cluster 모드 (복제본 2) — 롤백은 base 재적용
```

### 환경변수 (`.env` 하나로 제어 — `core/config.py`가 기동 시 로드)

모든 설정은 프로젝트 루트 `.env`에서 조절한다. `core/config.py`가 단일 소스이고 각 모듈은 `from core import config`로 값을 가져온다(하드코딩 금지). `.env`는 gitignore 대상 — 템플릿은 `.env.example`. python-dotenv 미설치 환경에서도 config의 경량 파서가 `.env`를 읽는다. 우선순위: 실제 환경변수 > `.env` > 코드 기본값.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `ORACLE_DSN` / `ORACLE_USER` / `ORACLE_PASSWORD` | `localhost:1521/FREEPDB1` / `system` / `poc1234` | Oracle 접속. 접속 상수는 `config.ORACLE_*` 직접 참조 |
| `CHAT_URL` / `MODEL_NAME` | `MODEL_URL` 폴백 / `qwen/qwen3.6-35b-a3b` | LLM(채팅) 엔드포인트·served-model-name |
| `EMBED_URL` / `EMBED_MODEL` | `MODEL_URL` 폴백 / `text-embedding-qwen3-embedding-0.6b` | 임베딩 폴백 — **실사용은 model_registry 기본값 우선**(`model_registry.embedding_endpoint()` 한 곳에서 해석, 검색·청크 백필·dedup·경로 진입점 공통. 관리 페이지에서 기본 지정 시 즉시 전환 + 백필이 자동 재임베딩) |
| `RERANK_URL` / `RERANK_MODEL` | `MODEL_URL` 폴백 / (빈값) | 리랭커 (레지스트리 슬롯만 — 리랭크 단계 미구현) |
| `MODEL_URL` | `http://127.0.0.1:1234/v1` | 단일 서빙 폴백. CHAT/EMBED/RERANK_URL이 비면 여기로(LM Studio 단일 서빙 호환) |
| `MODEL_API_KEY` | `lm-studio` | OpenAI 호환 키. vLLM에 `--api-key` 미설정 시 더미값이면 됨 |
| `DATAHUB_GMS_URL` | `http://localhost:8080` | DataHub MCP·ingest가 붙는 GMS |
| `SOURCE_TABLE_ALLOWLIST` | (빈값=제한 없음) | 원천 테이블 화이트리스트 (쉼표구분) — 목록 밖 테이블은 브라우저 조회·소스 등록·야간 적재 전부 차단 (`source_registry.table_allowed` 한 곳으로 강제). 사내 전환 시 허용 테이블만 나열 |
| `ADMIN_ID` / `ADMIN_PASSWORD` | `admin` / (필수) | 자체 계정 인증 (`web/auth.py`, docs/integration.md). 관리자는 이 env 계정 1개 — `ADMIN_PASSWORD` 비면 **기동 실패**(fail-fast). 일반 계정은 `/login` 가입 → `app_users` 미승인 저장 → 관리 페이지 "계정 관리"에서 승인해야 로그인. 관리자가 일반 계정에 is_admin 부여/해제 가능(재로그인 시 반영) |
| `SESSION_SECRET` / `SESSION_MAX_AGE` | (필수) / `28800` | 로그인 서명 토큰(itsdangerous) — 쿠키(httponly)와 `Authorization: Bearer` 이중 수용, 무상태라 복제본 공유·재시작 생존 자동. SECRET 비면 기동 실패 |

사내 vLLM은 모델마다 호스트가 달라 URL을 역할별(CHAT/EMBED/RERANK)로 분리한다. served-model-name은 각 호스트 `GET /v1/models`로 확인 후 `.env`에 정확히 기입. 임베딩 모델을 바꾸면 백필 배치가 embed_model 불일치 청크를 자동 재백필(`ingestion/embed_corpus.py` — 재백필 중 lexical이 받침). nodes.embedding 재백필과 dedup 임계값 재캘리브레이션은 별도 필요.

**튜닝 옵션**(전부 `.env`·`config.py`에 기본값 있음, 바꿀 때만 조절): `LLM_TEMPERATURE`, `LLM_TIMEOUT`(LLM·임베딩 요청 타임아웃 — 멈춘 요청이 파이프라인을 무한 대기시키지 않게), 검색 `RRF_K`/`SEARCH_TOP_LEXICAL`/`SEARCH_TOP_SEMANTIC`, 경로 `PATH_SIM_ENTRY`, dedup `DEDUP_SIM_HIGH`/`DEDUP_SIM_THRESHOLD`/`DEDUP_SHORT_NAME_CHARS`/`DEDUP_CHAR_RATIO`/`DEDUP_SELECT_MAX`, 유지보수 `MAINT_LOW_COUNT`/`MAINT_ABSORB_COUNT`/`MAINT_MIN_AGE_DAYS`, 시간 감쇠 `MAINT_DECAY_HALF_LIFE_DAYS`/`MAINT_DECAY_GRACE_DAYS`/`MAINT_DECAY_FLOOR`, 게이트 행동 신호 `SIG_REPEAT_SIM`/`SIG_TOPIC_MOVE_SIM`/`SIG_HASTY_RATIO`/`SEG_SPLIT_SIM`(태스크 분할)/`RECUR_DAYS`(재발 창), 임베딩 `EMBED_BATCH`/`EMBED_CONCURRENCY`/`EMBED_TEXT_CHARS`, 청킹 `CHUNK_CHARS`/`CHUNK_OVERLAP`(운영은 app_settings가 우선), 코퍼스 `CORPUS_TOP_N`, Oracle 풀 `ORACLE_POOL_MIN`/`ORACLE_POOL_MAX`/`ORACLE_POOL_INCREMENT`(검색·경로제안·체크포인터 **세 풀 공통**), 활동 로그 보관 `EVENTS_RETAIN_DAYS`(기본 180일), 인메모리 인덱스 `INMEM_RELOAD_SECS`(버전 확인 주기), Oracle 드라이버 `ORACLE_MODE`(`thin` 기본 / `thick`=Instant Client, `config.py`가 기동 시 `init_oracle_client` 1회 호출 — Dockerfile에 Instant Client 포함). 시드 스키마 중 **1층 도메인은 Oracle `domain_registry` 테이블**이 닫힌 목록(기본 2종은 코드가 시드, 확장은 관리자 API `GET/POST /admin/domains` — 사람 전용, 소급 재분류 없음). 도메인은 등록 때 **용도(scope)를 명시 선택**: `both`(대화+문서)/`chat`(대화 전용)/`doc`(문서 전용 — 대화 분류·폴백에서 제외, 소스 구조화 전용). `DATAHUB_TOOLS`(기본 시드 원천)·`LAYER_KIND`와 프롬프트 길이 가드는 코드에 둔다.

**배포(k8s)**: 컨테이너별 env를 나열하지 않고 `deploy/k8s/base/gsc.env` 한 파일 → `configMapGenerator`로 ConfigMap 생성 → 앱·CronJob이 `envFrom`으로 주입받는다(클러스터 DNS·사내 모델 값). 로컬 `.env`와는 별개 파일. 값 변경 시 ConfigMap 이름 해시가 바뀌어 롤링 재시작까지 자동.

## 구현 아키텍처 (큰 그림)

상세 지도·테이블·API는 docs/implementation.md. 코드 흐름의 핵심:

- **모놀리스 이미지 1개** — `web/server.py`(FastAPI)가 SSE 스트리밍·세션 기록·그래프 데이터·모델 관리 API를 전부 담당. 기동 시 Oracle 코퍼스를 SQLite `:memory:` 검색 인덱스로 빌드(`search/corpus_search.reload_index` → `search/inmemory_index`). CronJob(파이프라인·유지보수·백필)도 같은 이미지.
- **에이전트** — `agent/agent.py`가 DeepAgents로 조립. 툴 = `suggest_paths`(새 문제 시 최우선 호출, 시스템 프롬프트로 강제) + `search_docs`/`read_doc`(함수 직접 등록 — 통합 코퍼스 검색·열람, 구명 search_blog/read_blog_post에서 개명) + **소스별 검색 도구 자동 생성**(`search_{소스명}` — source_registry 등록 = 검색 툴 등록) + **mcp_registry에 등록된 도구 서버들**(streamable_http/sse/stdio + **rest** — 사내 `GET /tools`+`POST /call` 패턴 전용 어댑터 `core/rest_tools.py`, MCP와 무손실 1:1 매핑. 시드 없음 — 관리 페이지나 MCP_DEFAULT_URL로 등록, 주소 등록만으로 도구 자동 조립). 모델별 에이전트 캐시는 server.py의 `_agents`. **전역 제어는 관리 페이지 /admin > 에이전트 설정**(app_settings: `agent_system_prompt` 덮어쓰기·`agent_mcp_enabled`·`agent_disabled_tools` — 저장 시 캐시 무효화로 다음 질문부터 반영, MCP 서버 주소는 .env 소관).
- **하이브리드 검색** (`search/corpus_search.py` + `search/inmemory_index.py`) — **SQLite `:memory:` 인덱스**로 FTS5(lexical, Kiwi 형태소 원형) top-30 + sqlite-vec 코사인(semantic, 현재 EMBED_MODEL 벡터만) top-30 → 문서 단위 best-chunk 집계 → RRF 융합. **진실 소스는 Oracle(corpus_docs·corpus_chunks), 인덱스는 기동 시·버전 변경 시 재빌드하는 파생물** — 별도 검색 서버 아님, Oracle Text 권한 불필요. 문서 id=`소스명:원천id`, 검색당 임베딩 계산은 질의 1건뿐. 임베딩 없으면 lexical 단독 동작. 스키마 상세: docs/schema.md.
- **원천 테이블 적재** (`ingestion/source_registry.py` + `ingestion/ingest_sources.py`) — 구조화할 저쪽 테이블은 관리자가 `source_registry`에 등록(테이블·id·시간 컬럼·필드→역할 매핑 title/body/question/answer/meta/url·content_kind — API `GET/POST /admin/sources`, 관리 페이지 /admin). 야간 배치가 ts 워터마크 증분으로 역할 조립해 corpus_docs에 MERGE. **원천 테이블은 읽기 전용(SELECT만)**. 상세: docs/integration.md.
- **문서 그래프 구조화** (`graph/doc_pipeline.py`) — 소스에 **그래프 도메인을 지정하면**(source_registry.domain, UI 셀렉트) 야간 03:40 배치가 corpus_docs 문서를 그 도메인의 정의·extract_hint 기준으로 LLM 판정: fits면 목표·접근법을 추출해 대화와 같은 그래프에 병합(`get_or_create` dedup 재사용), **기준 미달은 excluded**(corpus_docs.graph_status·graph_note). 증거는 node_evidence(kind='doc', ref=`소스:id`) — 세션 증거와 분리돼 성공/실패 카운트엔 안 섞임. 도메인 미지정 소스는 검색 전용. LLM 판정은 동시(스레드), 병합은 직렬. **운영 설정은 `app_settings` 테이블**(core/settings.py — 관리 페이지 /admin > 전처리 설정에서 재배포 없이 변경): 실행당 건수·동시성·본문 길이·전처리 전용 모델. 소스별 액션: 드라이런(판정만)·실패 재시도·초기화 재처리(그래프 기여 회수 후 재구조화 — 이중 카운트 방지). 초기화는 도메인 단위(`POST /admin/domains/{도메인}/reset` — 그 도메인의 모든 소스)·전역(`POST /admin/reset-all-docs`)도 지원 — 셋 다 문서 유래 기여만 회수, 대화 세션 기여는 불변.
- **경로 제안** (`search/path_suggest.py`) — 그래프에서 검증 경로 제안 + 실패 이력 경고. 진입 매칭은 문서 검색과 같은 인메모리 SQLite 하이브리드를 2층 목표 노드에 재사용(`inmemory_index`의 gfts+gvec, RRF 융합 — 임베딩 미서빙 시 렉시컬 폴백). 노출을 `suggestions` 테이블에 기록(채택률 보정용). 성공/실패는 판정 카운트로 관리 (불리언 금지 — PoC에서 실증된 결정).
- **멀티턴 기억** — `core/oracle_checkpointer.py` (LangGraph 체크포인터를 Oracle `lg_checkpoints`/`lg_writes`로 외부화). thread_id=세션id. 복제본 공유·재시작 생존이라 cluster 모드에서 세션 고정 불필요.
- **그래프 파이프라인** (`graph/graph_pipeline.py`) — UI 세션은 먼저 **태스크 세그먼트로 분할**(인접 질문 임베딩 < `SEG_SPLIT_SIM`이면 화제 단절 — 세그먼트마다 게이트·추출 독립, 성공·실패 혼합 세션은 unknown 폴백으로 기여 없음) 후 세션 게이트 2갈래(태스크 세션=expect 기준 LLM 판정 / UI 세션=행동 신호 코드 판정 — 후퇴 2개↑ fail, 전진만 있으면 success, 나머지 미판정). UI 세션 추출은 **fits(도메인 적합— 잡담·일반 상식 기여 제외)·grounded(도구 근거 — 일반 지식 답변은 기여 보류)** 판정 동반 (문서 파이프라인과 대칭) → 4계층 추출(도메인은 닫힌 목록, 목표·접근법은 LLM, 행동은 tool_calls에서 결정적) → dedup 병합 → 재발 소급 취소(같은 증상 `RECUR_DAYS` 내 재방문 시 success를 'retracted'로, 기여 가중치 회수). dedup 임계값: 코사인 ≥0.92이고 문자 가드(이름 12자 미만 제외 + difflib ratio ≥0.4) 통과 시 즉시 병합, 그 외 후보(0.70~)는 LLM 후보 선택(`llm_select` — 여러 형제 중 같은 의도 하나 고르기, 쌍별 이지선다보다 정확) 1회로 판정 (캘리브레이션 근거는 파일 상단 주석. 임베딩 모델 교체 시 임계값 재캘리브레이션 필요). 유지보수(`graph/graph_maintenance.py`)는 형제 통합·잎 흡수에 더해 패스3 시간 감쇠(유휴 3층 접근법 가중치를 반감기 곡선으로 하강, 멱등).
- **Oracle 단일 DB + ORM 경계** — 전 테이블은 `core/models.py`(SQLAlchemy 2.x)에 선언, 생성은 `core/db.py`의 `init_schema()`(서버 기동 시 create_all + 시드, 멱등). **규약: 단순 CRUD(레지스트리·계정·설정·기록)는 ORM(`db.session()`), MERGE 업서트·대량 배치·PL/SQL·체크포인터는 raw SQL 유지** — 18c/19c 대상, 23ai 전환 시 VECTOR 컬럼만 모델에 추가(SQLAlchemy 2.0.41+). 구버전 DB의 ALTER 마이그레이션은 각 모듈 ensure가 담당. `lg_checkpoints`/`lg_writes`는 체크포인터 소유(모델 없음).
- **활동 로그** (`core/events.py` + `app_events` 테이블) — 정상·비정상 전부 기록. 웹 요청(미들웨어가 전 요청 — `/static`·`/stats` 제외, **요청·응답 JSON 본문을 절단 없이 전문으로 detail에 저장**(2026-08-12 정책) — 비밀 키 `[REDACTED]`, `/auth*`류·SSE·`/admin/events` 자체는 본문 제외)·로그인 성공/실패(kind='auth', `web/auth.py`가 비밀번호 없이 누가·결과만 명시 기록 — 2026-08-12 정책 변경)·에이전트 도구 호출(status='call' 입력 args 전문 + status='result' 결과 전문)·야간 배치 시작/완료/실패(스택트레이스)·미처리 예외(전역 핸들러). `events.log()`는 예외를 삼켜 로깅이 앱을 못 죽인다. 보관 `EVENTS_RETAIN_DAYS`(180일) 초과분은 야간 유지보수가 회전 삭제. 조회는 관리 페이지 /admin > 활동 로그(`GET /admin/events[/{id}]` — kind/level 필터·검색·페이지). `level`은 Oracle 예약어라 DB 컬럼명 `lvl`(ORM 속성은 level).
- **야간 배치** — CronJob 03:00 graph-pipeline(UI 세션 포함 미판정분 처리), 03:10 원천 증분 적재, 03:15 문서 청킹, 03:20 유지보수(+무결성 점검 리포트 + 활동 로그 보관 회전), 03:30 청크 임베딩 백필(모델 불일치 자동 재백필), 03:40 문서 그래프 구조화.

## 문서
- `docs/plan.md` — 기획 보고 v2 (2026-08-07: PoC 실증 완료 후 사내 전환 준비 시점 — v1 대비 변경·운영 기능·전환 접점). 초기 기획·연구 근거 원문은 `docs/plan-v1.md` 아카이브
- `docs/research.md` — 오픈소스/논문/문제점 조사 (출처 링크 포함)
- `docs/references/` — 설계 결정별 선례 조사 아카이브 (파일명: 날짜-주제. 첫 문서: 사용자 제어 설계의 선례 — 전자동·전수 수작업의 실패와 선택적 게이트의 실증)
- `docs/design.md` — 확정된 설계 결정 (Mermaid 다이어그램 포함)
- `docs/poc-datasets.md` — PoC용 공개 데이터셋 카탈로그 (다운로드 링크·라이선스)
- `docs/poc-results.md` — PoC 실증 결과 (병합·가중치 검증, 캘리브레이션 수치, 남은 이슈)
- `docs/implementation.md` — 구현 아키텍처 지도 (컴포넌트·테이블·API·실행법·한계)
- `docs/schema.md` — 테이블 스키마 설계 (실스키마 전체 + 청크 임베딩·모델 버저닝 확장 설계, 마이그레이션 계획)
- `docs/integration.md` — 사내 전환 통합 설계 (외부 의존은 원천 테이블 source_registry 1개 — 인증은 자체 계정)
- `docs/system-overview.drawio` — 시각 자료 5페이지 (개요 / 4계층 / 세션 판정 / 19c 구성 / 구현 아키텍처)
- `docs/component-architecture.drawio` — 컴포넌트별 아키텍처 8페이지 (에이전트 / 앱 서버 / 검색 / 경로 제안 / 파이프라인 / 유지보수 / 저장소 / 배포)

## 핵심 설계 결정 (변경 시 docs/design.md도 갱신)
1. **4계층 스키마, 위는 닫고 아래는 연다** — 1~2층(도메인·목표)은 사람이 고정한 시드 스키마, 3~4층(접근법·행동)은 LLM 자동 확장. 3층 "접근법"이 추천 단위.
2. **세션 게이트** — 실시간 추출 금지. 세션 종료 시 성공/명시적 실패/미완결 3갈래 판정. 성공만 양의 가중치, 실패는 이유·조건과 함께 경고용, 미완결은 그래프에 안 씀.
3. **추천 + 자유 이탈** — 경로 제안은 강제 아님. 시스템이 유도한 통행은 가중치 기여 할인(피드백 루프 보정).
4. **실패 경로는 진입 직전에만 경고, 하드 차단 금지** — 재시도 성공 시 bi-temporal supersession으로 표식 무효화.
5. **저장소: Oracle 19c 단독** (사내 표준 DB 고정, 진실 소스) — nodes/edges 테이블 + 재귀 CTE(≤3 hop). **검색은 기동 시 Oracle에서 빌드하는 SQLite `:memory:` 인덱스**(FTS5+sqlite-vec) — 별도 영속 검색 서버가 아니라 파생 인덱스라 design §6(별도 검색엔진 금지) 정신 유지, Oracle Text 권한 불필요. Graphiti는 Oracle 미지원이라 추출 파이프라인 직접 구현. 상세: docs/design.md §5.
6. **책임 경계** — 클러스터된 데이터·메타데이터는 DataHub 공식 MCP로만 접근 (DataHub 내부 저장·인덱싱·스케일링은 우리 관심 밖). 우리가 만들고 운영하는 저장·검색은 Oracle 19c 하나뿐. 별도 검색 엔진(벡터DB 등) 도입 금지.

## 최우선 위험 (설계 리뷰 시 항상 확인)
- 인기 가중 피드백 루프 (노출 대비 채택률로 보정)
- 메모리 오염 (세션 게이트 + provenance 롤백)
- dedup 오류의 hop당 곱셈 전파 (검색 2~3 hop 제한)
