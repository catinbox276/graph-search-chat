# 사내 전환 통합 설계 — 외부 의존은 딱 2개

> 전제가 되는 설계 결정은 [design.md](design.md), 현재 구현 지도는 [implementation.md](implementation.md).
> 이 문서는 **사내(운영) 환경에 붙일 때 저쪽과 맞춰야 하는 접점**을 정의한다. (2026-08-05)

## 전제 (저쪽 환경에 이미 있는 것)

1. **SSO(Keycloak)가 이미 있다.** 사용자는 우리 앱에 도달하는 시점에 이미 인증된 상태다.
   우리 앱이 로그인 화면을 만들 필요가 없고, 만들어서도 안 된다.
2. **Oracle DB가 이미 있고, 구조화할 원천 데이터가 적재되어 있다.** 데이터 형태는 제각각:
   - 본문 컬럼 1개짜리 (블로그형)
   - 지문/답 2필드 (QA형)
   - 3필드 이상 + 필드 간 관계가 있는 형태
   - 내용 유형: 문제해결, 가이드 등
3. **그 DB 안에 우리 테이블을 만들어도 된다.** 원천 테이블만 저쪽 소유(읽기 전용)이고,
   나머지 저장소는 전부 우리가 같은 DB에 생성한다.
4. **DataHub MCP 서버도 이미 제공된다** — `GET http://<서버>/tools`(도구 목록),
   `POST http://<서버>/call`(도구 실행). 우리는 총 8개 중 읽기 전용 5개
   (`search` / `get_entities` / `list_schema_fields` / `get_lineage` / `get_dataset_queries`)만
   소비한다. 제공받는 것이라 협의 대상이 아니며, 엔드포인트 주소만 .env로 받는다.

**따라서 우리가 맞춰야 하는 접점은 SSO와 원천 테이블, 2개뿐이다.**
그 외(그래프·세션·레지스트리·체크포인터)는 전부 우리 소유라 협의 대상이 아니다.

---

## 접점 1 — SSO: 사용자 식별을 받아쓴다 (로그인 없음)

### 원칙

- 인증은 전단(SSO 게이트웨이/프록시)이 끝낸다. 앱은 **인증하지 않고 식별만 소비**한다.
- 앱이 받는 것은 userId 하나다. 이것으로:
  - **세션 분리** — 대화 세션은 사용자에 묶인다. 여러 대화를 겹쳐 쌓아도
    사용자별로 독립이고, 세션 목록은 본인 것만 보인다.
  - **멀티턴 기억** — thread_id=세션id 그대로 (세션이 이미 사용자에 묶이므로 충분).
  - **재발 판정** — 같은 userId 안에서만 매칭. 다른 사람이 같은 문제를 만난 건
    재발이 아니라 경로가 유효하다는 신호 (graph_pipeline.retract_recurrences).
  - **관리자 판별** — SSO의 역할(realm role 등)로 관리자 API 접근 제어.

### 앱의 인증 모드 (`AUTH_MODE`, tools/config.py)

| 모드 | 용도 | userId 출처 |
|---|---|---|
| `none` | 로컬 개발 | 없음 (user_id NULL) |
| `header` | **사내 기본** — 전단 SSO가 인증 후 헤더로 식별 전달 | `SSO_USER_HEADER`(예: `X-Auth-Request-User`)에서 읽음 |
| `keycloak` | 전단 프록시가 없는 환경 — 앱이 직접 OIDC 코드 플로우 | ID 토큰 `preferred_username` |

- 이 클러스터의 Istio 정책이 이미 `X-Auth-Request-User: dalgo@quantumcns.ai` 헤더를
  쓰고 있다 — 사내 SSO가 oauth2-proxy 계열 헤더 주입 방식이라는 근거. `header` 모드는
  이 패턴을 그대로 신뢰한다.
- **`header` 모드의 전제**: 사용자가 앱에 전단을 우회해 직접 접근할 수 없어야 한다
  (헤더는 위조 가능하므로). 인그레스/네트워크 정책으로 보장하고, 배포 체크리스트에 포함.
- `keycloak` 모드(직접 OIDC)는 구현되어 있고(app/auth.py + k8s/keycloak.yaml PoC 파드),
  전단 없는 검증·데모 환경에서 사용자 분리를 실험하는 용도로 유지한다.

### 앱이 저쪽과 맞출 값 (전부 .env) — **앱이 SSO에서 보는 것은 userId·role 2개뿐**

```
AUTH_MODE=header
SSO_USER_HEADER=X-Auth-Request-User      # userId 헤더명 (필수 — 없으면 401)
SSO_ROLE_HEADER=X-Auth-Request-Groups    # role 헤더명 (,;공백 구분 목록, 선택)
OIDC_ADMIN_ROLE=gsc-admin                # role 목록에 이 값이 있으면 관리자
```

### 구현 상태

| 항목 | 상태 |
|---|---|
| sessions.user_id 컬럼 + 기록 | 완료 (server.py log_turn) |
| 재발 판정 사용자 단위 매칭 | 완료 (graph_pipeline.retract_recurrences) |
| `header` 모드 (userId·role 헤더 소비) | 완료 (app/auth.py — 미식별 401, 로그인 UI 없음) |
| `keycloak` 모드 (직접 OIDC — 데모·검증용) | 완료 (app/auth.py + k8s/keycloak.yaml) |
| 사용자별 세션 목록·이어하기 UI | **미구현** — GET /sessions(본인 것만) + 사이드바 |
| suggestions에 user 기록 (채택률 사용자 차원) | 미구현 (선택) |

---

## 접점 2 — 구조화 원천 테이블: 관리자가 선택·등록한다

### 문제

지금은 `blog_posts`(제목+본문 단일 구조, 우리 스크립트가 생성) 하나가 코퍼스의 전부다.
사내에서는 **이미 적재된 저쪽 테이블**을 그대로 읽어야 하고, 테이블마다 구조가 다르다.
어떤 테이블의 어떤 필드를 어떤 역할로 쓸지는 코드가 아니라 **관리자가 지정**해야 한다.

### 해법: `source_registry` (domain_registry와 같은 패턴 — 사람 전용 시드 테이블)

관리자가 접속된 DB의 테이블을 골라 등록한다:

| 컬럼 | 의미 | 예 |
|---|---|---|
| `source_name` | 소스 식별명 (PK) | `troubleshoot_blog`, `faq_qa` |
| `table_name` | 원천 테이블 (읽기 전용) | `LEGACY_KNOWHOW` |
| `id_column` | 고유 id 필드 | `DOC_ID` |
| `ts_column` | 생성/수정 시간 필드 — 증분 적재 기준 | `CREATED_AT` |
| `field_map` | 컬럼→역할 매핑 (JSON) — 어떤 필드를 구조화할지 | `{"title":"SUBJECT","question":"BODY_Q","answer":"BODY_A"}` |
| `content_kind` | 내용 유형 — 추출·검색 프롬프트에 반영 | `문제해결` / `가이드` |
| `domain` | 그래프 구조화 도메인 (NULL=검색 전용) — 지정 시 야간 03:40 배치가 이 도메인 기준으로 문서를 LLM 판정·그래프 병합 | `사내 노하우` |
| `enabled` | 적재 대상 여부 | `Y/N` |

- **역할(role) 어휘는 닫아둔다**: `title / body / question / answer / meta / url`.
  1필드 블로그형은 `body` 하나, QA형은 `question`+`answer`, N필드는 조합.
  역할이 검색 문서 조립 방식(아래)을 결정한다 — 필드 간 관계는 역할 조합으로 표현.
- 관리 통로는 domain_registry와 동일: 관리자 API(`GET/POST /admin/sources`) + 관리 UI 모달.
  등록을 돕기 위해 `GET /admin/sources/tables`(접속 DB의 테이블·컬럼 목록 조회)를 제공.

### 파이프라인 일반화

1. **적재(야간 증분)** — 등록된 소스마다 `ts_column > 마지막 적재 시각` 신규분을 읽어
   역할 매핑으로 **검색 문서를 조립**(예: QA형은 "Q: {question}\nA: {answer}")하고
   통합 코퍼스 테이블(`corpus_docs`: source_name, src_id, title, text, embedding, ts)에 넣는다.
   기존 `blog_posts`는 "소스 1호"로 등록되어 같은 흐름에 흡수된다.
2. **임베딩 백필** — 기존 03:30 CronJob이 corpus_docs 기준으로 동작 (embed_corpus.py 일반화).
3. **검색** — 하이브리드 검색(blog_search.py)이 corpus_docs를 대상으로 동작.
   `content_kind`는 검색 결과 라벨과 (도메인 extract_hint처럼) 프롬프트 힌트에 쓴다.
4. **읽기 도구** — `read_blog_post`가 문서 id `"소스명:원천id"`를 받도록 일반화
   (도구명은 기존 그래프 4층 행동·도메인 시드와의 호환을 위해 유지. 구형 blog id도 동작).
5. **문서 그래프 구조화(선택)** — 소스에 도메인을 지정하면 야간 03:40 배치가 문서를 LLM 판정해
   기준 통과분만 그래프에 병합(미달은 excluded + 사유). 운영 도구 완비: 드라이런(판정만),
   실패 재시도, 초기화 재처리(소스/도메인/전역 — 그래프 기여 회수 후 재구조화, 대화 세션 기여 불변),
   처리 현황 프로그래스 UI(5초 폴링), 전처리 설정(app_settings — 건수·동시성·모델, 재배포 불필요).

### 원칙 재확인

- **원천 테이블은 읽기 전용.** UPDATE/DELETE/DDL 금지. 인덱스가 필요하면 corpus_docs(우리 것)에 만든다.
- **우리 테이블은 같은 DB에 생성** — sessions, nodes/edges/node_evidence, suggestions,
  model_registry, domain_registry, **source_registry, corpus_docs(신규)**, lg_checkpoints/lg_writes.
- design §6(별도 검색 엔진 도입 금지)은 그대로 — 전부 Oracle 하나에서.

---

## 요약: 사내 전환 체크리스트

1. **SSO**: `AUTH_MODE=header` + 헤더명 2개(`SSO_USER_HEADER`=userId, `SSO_ROLE_HEADER`=role)만
   협의. 전단 우회 접근 차단 확인. — 앱 쪽 구현은 완료(user_id 기록·사용자 단위 재발 판정 포함).
2. **원천 테이블**: 관리자가 UI에서 테이블·id·시간·필드 역할을 등록(`source_registry`).
   야간 증분 적재가 자동으로 코퍼스·임베딩·검색에 반영.
3. 그 외 전부(그래프·세션·레지스트리·체크포인터)는 같은 Oracle에 우리가 생성 — 협의 불필요.

### 남은 구현 (착수 전 확인용 목록)

- [x] `header` 인증 모드 (auth.py — userId·role 헤더 2개 소비)
- [x] 사용자별 세션 목록·이어하기 (GET /sessions + UI 사이드바 — 소유권 검사 포함)
- [x] `source_registry` 테이블 + 관리자 API/UI (`/admin/sources`, 테이블·컬럼 브라우저 포함)
- [x] 적재 일반화: corpus_docs + 증분 적재 배치(scripts/ingest_sources.py, 야간 03:10)
      + embed/검색/read 도구 전환 (corpus 없으면 blog_posts 폴백 — 전환기 무중단)
