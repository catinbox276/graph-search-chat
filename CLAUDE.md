# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# graph-search-chat

사용자(주로 데이터 분석가)의 질문에 에이전트가 두 도구 — **DataHub MCP**(복잡한 데이터 조회·조인·조립)와 **사내 블로그 검색**(문제해결 노하우 글) — 로 답하고, 그 대화 로그에서 온톨로지/지식그래프를 자동 추출·병합해, 신규 사용자에게 검증된 문제 해결 경로를 제안하는 시스템. **PoC 파이프라인 실증 완료** — self-play 47세션으로 추출·병합·가중치·실패 표식 전 순환 동작 확인 (docs/poc-results.md).

## 커맨드

테스트 스위트·린터는 없다 (PoC). 검증은 실행 + Oracle 조회 + `/stats`로 한다. 파이썬은 `.venv/bin/python` 사용.

```bash
# 앱 서버 (채팅 UI :8500, 그래프 뷰 :8500/graph)
.venv/bin/uvicorn app.server:app --port 8500

# 에이전트 단발 실행 (서버 없이 CLI로 질문 1건)
.venv/bin/python agent/agent.py "financial DB에서 계좌 테이블 뭐 있어?"

# self-play 세션 생성 (재실행 시 완료분 스킵하고 이어함)
.venv/bin/python poc/selfplay.py [--only R1,R2]

# 그래프 파이프라인: sessions → 게이트 판정 → 4계층 추출 → nodes/edges 병합
.venv/bin/python poc/graph_pipeline.py

# 그래프 유지보수 (멱등): 저빈도 형제 통합 + 오래된 잎 흡수
.venv/bin/python -m poc.graph_maintenance [--age-days 14]

# 데이터 준비 (1회, 순서대로): 코퍼스 빌드 → Oracle 적재 → 임베딩 백필 → BIRD 적재
python3 scripts/build_corpus.py && python3 scripts/load_oracle.py \
  && python3 scripts/embed_corpus.py && python3 scripts/ingest_bird.py

# 원천 테이블 증분 적재: source_registry 등록분 → corpus_docs (야간 03:10과 동일, 멱등)
python3 scripts/ingest_sources.py

# 문서 그래프 구조화: 도메인 지정 소스의 corpus_docs를 LLM 판정·그래프 병합 (야간 03:40과 동일)
.venv/bin/python -m poc.doc_pipeline [--limit N]

# 배포 (전제: k8s + LM Studio :1234 — 상세 절차는 docs/implementation.md "실행 방법")
docker build -t graph-search-chat:latest .
kubectl apply -f k8s/oracle.yaml          # Oracle StatefulSet
kubectl apply -k k8s/base                 # standalone (복제본 1) + CronJob
kubectl apply -k k8s/cluster              # cluster 모드 (복제본 2) — 롤백은 base 재적용
```

### 환경변수 (`.env` 하나로 제어 — `tools/config.py`가 기동 시 로드)

모든 설정은 프로젝트 루트 `.env`에서 조절한다. `tools/config.py`가 단일 소스이고 각 모듈은 `from tools import config`로 값을 가져온다(하드코딩 금지). `.env`는 gitignore 대상 — 템플릿은 `.env.example`. python-dotenv 미설치 환경에서도 config의 경량 파서가 `.env`를 읽는다. 우선순위: 실제 환경변수 > `.env` > 코드 기본값.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `ORACLE_DSN` / `ORACLE_USER` / `ORACLE_PASSWORD` | `localhost:1521/FREEPDB1` / `system` / `poc1234` | Oracle 접속. `DSN`/`USER`/`PASSWORD`는 관례상 `tools/blog_search.py`에서 re-export |
| `CHAT_URL` / `MODEL_NAME` | `MODEL_URL` 폴백 / `qwen/qwen3.6-35b-a3b` | LLM(채팅) 엔드포인트·served-model-name |
| `EMBED_URL` / `EMBED_MODEL` | `MODEL_URL` 폴백 / `text-embedding-qwen3-embedding-0.6b` | 임베딩 엔드포인트·모델 (검색·dedup·경로 진입점이 직접 사용) |
| `RERANK_URL` / `RERANK_MODEL` | `MODEL_URL` 폴백 / (빈값) | 리랭커 (레지스트리 슬롯만 — 리랭크 단계 미구현) |
| `MODEL_URL` | `http://127.0.0.1:1234/v1` | 단일 서빙 폴백. CHAT/EMBED/RERANK_URL이 비면 여기로(LM Studio 단일 서빙 호환) |
| `MODEL_API_KEY` | `lm-studio` | OpenAI 호환 키. vLLM에 `--api-key` 미설정 시 더미값이면 됨 |
| `DATAHUB_GMS_URL` | `http://localhost:8080` | DataHub MCP·ingest가 붙는 GMS |
| `ADMIN_TOKEN` | `poc-admin` | 모델 관리 API (`X-Admin-Token` 헤더) — SSO 관리자 역할로도 통과 |
| `AUTH_MODE` | `none` | SSO 인증 (`app/auth.py`, docs/integration.md 접점 1). `header`=사내 기본 — 전단 SSO가 준 `SSO_USER_HEADER`(userId)·`SSO_ROLE_HEADER`(role) 2개만 소비, 로그인 UI 없음. `keycloak`=전단 없는 환경용 직접 OIDC(`KEYCLOAK_*`/`OIDC_*` 필요, PoC 파드는 `k8s/keycloak.yaml`). 관리자 = `OIDC_ADMIN_ROLE`(기본 `gsc-admin`) 역할 보유 |

사내 vLLM은 모델마다 호스트가 달라 URL을 역할별(CHAT/EMBED/RERANK)로 분리한다. served-model-name은 각 호스트 `GET /v1/models`로 확인 후 `.env`에 정확히 기입. 임베딩 모델을 바꾸면 전체 재백필 필요(`scripts/embed_corpus.py`).

**튜닝 옵션**(전부 `.env`·`config.py`에 기본값 있음, 바꿀 때만 조절): `LLM_TEMPERATURE`, 검색 `RRF_K`/`SEARCH_TOP_LEXICAL`/`SEARCH_TOP_SEMANTIC`, 경로 `PATH_SIM_ENTRY`, dedup `DEDUP_SIM_HIGH`/`DEDUP_SIM_THRESHOLD`, 유지보수 `MAINT_LOW_COUNT`/`MAINT_ABSORB_COUNT`/`MAINT_MIN_AGE_DAYS`, 시간 감쇠 `MAINT_DECAY_HALF_LIFE_DAYS`/`MAINT_DECAY_GRACE_DAYS`/`MAINT_DECAY_FLOOR`, 게이트 행동 신호 `SIG_REPEAT_SIM`/`SIG_TOPIC_MOVE_SIM`/`SIG_HASTY_RATIO`/`RECUR_DAYS`(재발 창), 임베딩 `EMBED_BATCH`/`EMBED_CONCURRENCY`/`EMBED_TEXT_CHARS`, 코퍼스 `CORPUS_TOP_N`, Oracle 풀 `ORACLE_POOL_MIN`/`ORACLE_POOL_MAX`/`ORACLE_POOL_INCREMENT`(검색·경로제안·체크포인터 **세 풀 공통**), Oracle Text `ORACLE_TEXT_LEXER`(기본 `WORLD_LEXER`, 한국어 정밀은 `KOREAN_MORPH_LEXER`), Oracle 드라이버 `ORACLE_MODE`(`thin` 기본 / `thick`=Instant Client, `config.py`가 기동 시 `init_oracle_client` 1회 호출 — Dockerfile에 Instant Client 포함). 시드 스키마 중 **1층 도메인은 Oracle `domain_registry` 테이블**이 닫힌 목록(기본 2종은 코드가 시드, 확장은 관리자 API `GET/POST /admin/domains` — 사람 전용, 소급 재분류 없음). 도메인은 등록 때 **용도(scope)를 명시 선택**: `both`(대화+문서)/`chat`(대화 전용)/`doc`(문서 전용 — 대화 분류·폴백에서 제외, 소스 구조화 전용). `DATAHUB_TOOLS`(기본 시드 원천)·`LAYER_KIND`와 프롬프트 길이 가드는 코드에 둔다.

**배포(k8s)**: 컨테이너별 env를 나열하지 않고 `k8s/base/gsc.env` 한 파일 → `configMapGenerator`로 ConfigMap 생성 → 앱·CronJob이 `envFrom`으로 주입받는다(클러스터 DNS·사내 모델 값). 로컬 `.env`와는 별개 파일. 값 변경 시 ConfigMap 이름 해시가 바뀌어 롤링 재시작까지 자동.

## 구현 아키텍처 (큰 그림)

상세 지도·테이블·API는 docs/implementation.md. 코드 흐름의 핵심:

- **모놀리스 이미지 1개** — `app/server.py`(FastAPI)가 SSE 스트리밍·세션 기록·그래프 데이터·모델 관리 API를 전부 담당. 기동 시 Oracle에서 임베딩 행렬 1.4GB를 메모리에 로드(`tools/blog_search.load_matrix`). CronJob(파이프라인·유지보수·백필)도 같은 이미지.
- **에이전트** — `agent/agent.py`가 DeepAgents로 조립. 툴 = `suggest_paths`(새 문제 시 최우선 호출, 시스템 프롬프트로 강제) + `search_blog`/`read_blog_post`(함수 직접 등록) + DataHub 공식 MCP(stdio, 유일한 MCP). 모델별 에이전트 캐시는 server.py의 `_agents`.
- **하이브리드 검색** (`tools/blog_search.py`) — Oracle Text(lexical) top-30 + 인메모리 행렬 코사인(semantic) top-30 → RRF 융합. 검색 대상은 통합 코퍼스 `corpus_docs`(문서 id=`소스명:원천id`, 없으면 구 blog_posts 폴백). 검색당 임베딩 계산은 질의 1건뿐. 임베딩 없으면 lexical 단독으로도 동작.
- **원천 테이블 적재** (`tools/source_registry.py` + `scripts/ingest_sources.py`) — 구조화할 저쪽 테이블은 관리자가 `source_registry`에 등록(테이블·id·시간 컬럼·필드→역할 매핑 title/body/question/answer/meta/url·content_kind — API `GET/POST /admin/sources`, UI 📚 소스). 야간 배치가 ts 워터마크 증분으로 역할 조립해 corpus_docs에 MERGE. **원천 테이블은 읽기 전용(SELECT만)**. 상세: docs/integration.md 접점 2.
- **문서 그래프 구조화** (`poc/doc_pipeline.py`) — 소스에 **그래프 도메인을 지정하면**(source_registry.domain, UI 셀렉트) 야간 03:40 배치가 corpus_docs 문서를 그 도메인의 정의·extract_hint 기준으로 LLM 판정: fits면 목표·접근법을 추출해 대화와 같은 그래프에 병합(`get_or_create` dedup 재사용), **기준 미달은 excluded**(corpus_docs.graph_status·graph_note). 증거는 `doc:소스:id` — sessions와 조인 안 되므로 성공/실패 카운트엔 안 섞임. 도메인 미지정 소스는 검색 전용. LLM 판정은 동시(스레드), 병합은 직렬. **운영 설정은 `app_settings` 테이블**(tools/settings.py — 관리 UI 📚 소스 > 전처리 설정에서 재배포 없이 변경): 실행당 건수·동시성·본문 길이·전처리 전용 모델. 소스별 액션: 드라이런(판정만)·실패 재시도·초기화 재처리(그래프 기여 회수 후 재구조화 — 이중 카운트 방지). 초기화는 도메인 단위(`POST /admin/domains/{도메인}/reset` — 그 도메인의 모든 소스)·전역(`POST /admin/reset-all-docs`)도 지원 — 셋 다 문서 유래 기여만 회수, 대화 세션 기여는 불변.
- **경로 제안** (`tools/path_suggest.py`) — 그래프에서 검증 경로 제안 + 실패 이력 경고. 노출을 `suggestions` 테이블에 기록(채택률 보정용). 성공/실패는 판정 카운트로 관리 (불리언 금지 — PoC에서 실증된 결정).
- **멀티턴 기억** — `tools/oracle_checkpointer.py` (LangGraph 체크포인터를 Oracle `lg_checkpoints`/`lg_writes`로 외부화). thread_id=세션id. 복제본 공유·재시작 생존이라 cluster 모드에서 세션 고정 불필요.
- **그래프 파이프라인** (`poc/graph_pipeline.py`) — 세션 게이트 2갈래(태스크 세션=expect 기준 LLM 판정 / UI 세션=행동 신호 코드 판정 — 후퇴 2개↑ fail, 전진만 있으면 success, 나머지 미판정) → 4계층 추출(도메인은 닫힌 목록, 목표·접근법은 LLM, 행동은 tool_calls에서 결정적) → dedup 병합 → 재발 소급 취소(같은 증상 `RECUR_DAYS` 내 재방문 시 success를 'retracted'로, 기여 가중치 회수). dedup 임계값: 코사인 ≥0.92 즉시 병합, 0.70~0.92는 LLM 동일 의도 확인 (캘리브레이션 근거는 파일 상단 주석). 유지보수(`poc/graph_maintenance.py`)는 형제 통합·잎 흡수에 더해 패스3 시간 감쇠(유휴 3층 접근법 가중치를 반감기 곡선으로 하강, 멱등).
- **Oracle 단일 DB** — 테이블: `corpus_docs`(통합 코퍼스+embedding BLOB, 구 `blog_posts`는 소스 1호로 흡수), `source_registry`, `domain_registry`, `sessions`(+user_id), `nodes`/`edges`/`node_evidence`, `suggestions`, `model_registry`, `lg_checkpoints`/`lg_writes`. DSN 등 접속 상수는 `tools/blog_search.py`에서 import하는 게 관례.
- **야간 배치** — CronJob 03:00 graph-pipeline(UI 세션 포함 미판정분 처리), 03:10 원천 증분 적재, 03:20 유지보수, 03:30 임베딩 백필, 03:40 문서 그래프 구조화.

## 문서
- `docs/plan.md` — 기획 보고 (비전공자용: 배경·별도 프로젝트 결정·도구 3종·대화 자산화 근거·관리 방안)
- `docs/research.md` — 오픈소스/논문/문제점 조사 (출처 링크 포함)
- `docs/design.md` — 확정된 설계 결정 (Mermaid 다이어그램 포함)
- `docs/poc-datasets.md` — PoC용 공개 데이터셋 카탈로그 (다운로드 링크·라이선스)
- `docs/poc-results.md` — PoC 실증 결과 (병합·가중치 검증, 캘리브레이션 수치, 남은 이슈)
- `docs/implementation.md` — 구현 아키텍처 지도 (컴포넌트·테이블·API·실행법·한계)
- `docs/integration.md` — 사내 전환 통합 설계 (외부 의존 2개: SSO 사용자 식별 소비 + 원천 테이블 source_registry)
- `system-overview.drawio` — 시각 자료 5페이지 (개요 / 4계층 / 세션 판정 / 19c 구성 / 구현 아키텍처)
- `docs/component-architecture.drawio` — 컴포넌트별 아키텍처 8페이지 (에이전트 / 앱 서버 / 검색 / 경로 제안 / 파이프라인 / 유지보수 / 저장소 / 배포)

## 핵심 설계 결정 (변경 시 docs/design.md도 갱신)
1. **4계층 스키마, 위는 닫고 아래는 연다** — 1~2층(도메인·목표)은 사람이 고정한 시드 스키마, 3~4층(접근법·행동)은 LLM 자동 확장. 3층 "접근법"이 추천 단위.
2. **세션 게이트** — 실시간 추출 금지. 세션 종료 시 성공/명시적 실패/미완결 3갈래 판정. 성공만 양의 가중치, 실패는 이유·조건과 함께 경고용, 미완결은 그래프에 안 씀.
3. **추천 + 자유 이탈** — 경로 제안은 강제 아님. 시스템이 유도한 통행은 가중치 기여 할인(피드백 루프 보정).
4. **실패 경로는 진입 직전에만 경고, 하드 차단 금지** — 재시도 성공 시 bi-temporal supersession으로 표식 무효화.
5. **저장소: Oracle 19c 단독** (사내 표준 DB 고정) — nodes/edges 테이블 + 재귀 CTE(≤3 hop) + Oracle Text(한국어). 벡터 검색은 진입점 LLM 분류(닫힌 1~2층)와 애플리케이션 브루트포스 dedup으로 대체. Graphiti는 Oracle 미지원이라 추출 파이프라인 직접 구현. 상세: docs/design.md §5.
6. **책임 경계** — 클러스터된 데이터·메타데이터는 DataHub 공식 MCP로만 접근 (DataHub 내부 저장·인덱싱·스케일링은 우리 관심 밖). 우리가 만들고 운영하는 저장·검색은 Oracle 19c 하나뿐. 별도 검색 엔진(벡터DB 등) 도입 금지.

## 최우선 위험 (설계 리뷰 시 항상 확인)
- 인기 가중 피드백 루프 (노출 대비 채택률로 보정)
- 메모리 오염 (세션 게이트 + provenance 롤백)
- dedup 오류의 hop당 곱셈 전파 (검색 2~3 hop 제한)
