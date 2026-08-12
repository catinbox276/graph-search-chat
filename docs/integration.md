# 사내 전환 통합 설계 — 외부 의존은 원천 테이블 1개

> 전제가 되는 설계 결정은 [design.md](design.md), 현재 구현 지도는 [implementation.md](implementation.md).
> 이 문서는 **사내(운영) 환경에 붙일 때 저쪽과 맞춰야 하는 접점**을 정의한다. (2026-08-07)

## 전제 (저쪽 환경에 이미 있는 것)

1. **Oracle DB가 이미 있고, 구조화할 원천 데이터가 적재되어 있다.** 데이터 형태는 제각각:
   - 본문 컬럼 1개짜리 (블로그형)
   - 지문/답 2필드 (QA형)
   - 3필드 이상 + 필드 간 관계가 있는 형태
   - 내용 유형: 문제해결, 가이드 등
2. **그 DB 안에 우리 테이블을 만들어도 된다.** 원천 테이블만 저쪽 소유(읽기 전용)이고,
   나머지 저장소는 전부 우리가 같은 DB에 생성한다.
3. **DataHub MCP 서버도 이미 제공된다** — `GET http://<서버>/tools`(도구 목록),
   `POST http://<서버>/call`(도구 실행). 우리는 총 8개 중 읽기 전용 5개
   (`search` / `get_entities` / `list_schema_fields` / `get_lineage` / `get_dataset_queries`)만
   소비한다. 제공받는 것이라 협의 대상이 아니며, 엔드포인트 주소만 관리 페이지/env로 등록한다.

**따라서 저쪽과 맞춰야 하는 접점은 원천 테이블 1개뿐이다.**
인증은 앱이 자체 계정으로 직접 관리하고(아래), 그 외(그래프·세션·레지스트리·체크포인터)는
전부 우리 소유라 협의 대상이 아니다.

---

## 인증 — 자체 계정 (외부 SSO 의존 없음)

> 변천: header(전단 SSO 헤더) → keycloak(직접 OIDC) → gateway/proxy(게이트웨이 토큰 검증)를
> 차례로 구현·리허설했으나, **기획 변경으로 사용자를 앱이 자체 관리**하는 것으로 확정(2026-08-07).
> 구 모드는 코드·설정에서 전부 제거됐다.

### 구조 (web/auth.py)

- **관리자 = 환경 설정 계정 1개** (`ADMIN_ID`/`ADMIN_PASSWORD` — DB가 아니라 env,
  잠금 사고 시 env 수정으로 복구 가능).
- **일반 계정 = 회원가입 + 승인**: `/login`에서 가입(id+pw만) → `app_users`에
  미승인(approved='N')으로 저장 → **관리자가 관리 페이지에서 승인해야 로그인 가능**.
- **2권한**: 일반/관리자. 관리자는 관리 페이지 "계정 관리"에서 일반 계정에
  관리자 권한(is_admin)을 부여/해제할 수 있다 — 재로그인 시부터 반영.
- **세션 = 서명 토큰** (itsdangerous, `SESSION_SECRET` 서명, `SESSION_MAX_AGE` 만료) —
  쿠키(httponly)와 `Authorization: Bearer` 헤더 양쪽으로 수용. 서버 저장소가 없어
  복제본 공유·재시작 생존이 자동(cluster 모드 세션 고정 불필요).
- **슬라이딩 갱신** — 수명 절반을 넘긴 유효 쿠키는 요청마다 자동 재발급.
  계속 사용 중인 사용자는 만료되지 않고, `SESSION_MAX_AGE` 이상 방치 시에만 만료.
  로그인·로그아웃처럼 핸들러가 쿠키를 직접 설정·삭제한 응답에는 개입하지 않는다
  (로그아웃 무효화 방지). Bearer 헤더만 쓰는 스크립트는 갱신 대상 아님(재발급으로 해결).
  만료 시 전 페이지 공통으로 로그인 페이지로 안내하고 재로그인 후 원래 페이지로 복귀.
- **로그인 감사 로그** — 성공/실패/승인 대기 거부를 활동 로그(kind=`auth`)에 기록
  (누가·결과만, 비밀번호는 어디에도 남기지 않음 — 2026-08-12 정책).
- **비밀번호 = PBKDF2-HMAC-SHA256** (stdlib — 의존성 추가 없음).
- 미로그인: 페이지는 `/login`으로 리다이렉트, API는 401.
- 트레이드오프(알려진 것): 서명 토큰은 서버측 폐기가 불가 — 강제 로그아웃·즉시 권한 회수가
  필요해지면 서버측 세션 저장소로 전환해야 한다. 현재는 재로그인 시 반영으로 충분하다고 판단.

### userId가 하는 일 (기존과 동일)

- **세션 분리** — 대화 세션은 사용자에 묶이고 목록은 본인 것만 보인다.
- **멀티턴 기억** — thread_id=세션id 그대로.
- **재발 판정** — 같은 userId 안에서만 매칭 (graph_pipeline.retract_recurrences).
- **관리자 판별** — env 계정 또는 is_admin 부여 계정만 관리 API 통과.

### 설정 (.env / deploy/k8s/base/gsc.env)

```
ADMIN_ID=admin                # 관리자 아이디
ADMIN_PASSWORD=<필수>          # 비어 있으면 기동 실패 (fail-fast)
SESSION_SECRET=<필수>          # 로그인 토큰 서명키
SESSION_MAX_AGE=28800         # 토큰 수명(초), 기본 8시간
```

### 구현 상태

| 항목 | 상태 |
|---|---|
| 가입/승인/로그인/로그아웃 (+ /login UI) | 완료 (web/auth.py + app/login.html) |
| 계정 관리 UI (승인·권한 부여/해제·삭제) | 완료 (/admin "계정 관리" — GET /admin/users + POST /admin/users/act) |
| 쿠키 + Bearer 이중 수용 | 완료 (스크립트/API 호출용) |
| sessions.user_id 기록·본인 세션만 목록 | 완료 (server.py) |
| 재발 판정 사용자 단위 매칭 | 완료 (graph_pipeline.retract_recurrences) |
| suggestions에 user 기록 (채택률 사용자 차원) | 미구현 (선택) |

---

## 접점 — 구조화 원천 테이블: 관리자가 선택·등록한다

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
| `url_enabled` | 원본 링크 노출 여부 — 끄면 검색 결과·출처·문서 뷰에서 링크 숨김 | `Y/N` |
| `enabled` | 적재 대상 여부 | `Y/N` |

- **역할(role) 어휘는 닫아둔다**: `title / body / question / answer / meta / url`.
  1필드 블로그형은 `body` 하나, QA형은 `question`+`answer`, N필드는 조합.
  역할이 검색 문서 조립 방식(아래)을 결정한다 — 필드 간 관계는 역할 조합으로 표현.
- 관리 통로는 domain_registry와 동일: 관리자 API(`GET/POST /admin/sources`) + 관리 페이지.
  등록을 돕기 위해 `GET /admin/sources/tables`(접속 DB의 테이블·컬럼 목록 조회)를 제공.
- `SOURCE_TABLE_ALLOWLIST`(.env)로 등록·조회·적재 가능한 테이블을 화이트리스트로 제한할 수 있다.

### 파이프라인 일반화

1. **적재(야간 증분)** — 등록된 소스마다 `ts_column > 마지막 적재 시각` 신규분을 읽어
   역할 매핑으로 **검색 문서를 조립**(예: QA형은 "Q: {question}\nA: {answer}")하고
   통합 코퍼스 테이블(`corpus_docs`: source_name, src_id, title, text, embedding, ts)에 넣는다.
   기존 `blog_posts`는 "소스 1호"로 등록되어 같은 흐름에 흡수된다.
   야간을 기다릴 필요 없이 관리 페이지에서 즉시 실행 가능: **지금 적재**(증분+청킹),
   **전량 재적재**(해당 소스 코퍼스·워터마크 리셋 후 처음부터), **지금 구조화**(백그라운드로
   미처리 0건까지 drain — 실행 중 리셋·재적재는 409로 차단해 카운트 꼬임 방지).
2. **임베딩 백필** — 기존 03:30 CronJob이 corpus_chunks 기준으로 동작 (embed_corpus.py).
3. **검색** — 하이브리드 검색(corpus_search.py)이 corpus_docs/corpus_chunks를 대상으로 동작.
   `content_kind`는 검색 결과 라벨과 (도메인 extract_hint처럼) 프롬프트 힌트에 쓴다.
4. **읽기 도구** — `read_doc`(구 `read_blog_post`)이 문서 id `"소스명:원천id"`를 받는다.
   전체 검색은 `search_docs`(구 `search_blog`) — 개명 시 그래프 4층 행동 노드·도메인 시드도 함께 마이그레이션했다.
5. **문서 그래프 구조화(선택)** — 소스에 도메인을 지정하면 야간 03:40 배치가 문서를 LLM 판정해
   기준 통과분만 그래프에 병합(미달은 excluded + 사유). 운영 도구 완비: 드라이런(판정만),
   실패 재시도, 초기화 재처리(소스/도메인/전역 — 그래프 기여 회수 후 재구조화, 대화 세션 기여 불변),
   처리 현황 프로그래스 UI(5초 폴링), 전처리 설정(app_settings — 건수·동시성·모델, 재배포 불필요).

### 원칙 재확인

- **원천 테이블은 읽기 전용.** UPDATE/DELETE/DDL 금지. 인덱스가 필요하면 corpus_docs(우리 것)에 만든다.
- **우리 테이블은 같은 DB에 생성** — sessions, nodes/edges/node_evidence, suggestions,
  model_registry, domain_registry, mcp_registry, source_registry, corpus_docs/corpus_chunks,
  app_users, app_settings, lg_checkpoints/lg_writes.
- design §6(별도 검색 엔진 도입 금지)은 그대로 — 전부 Oracle 하나에서.

---

## 요약: 사내 전환 체크리스트

1. **인증**: 협의 불필요 — 자체 계정. `.env`에 `ADMIN_ID`/`ADMIN_PASSWORD`/`SESSION_SECRET`만
   설정(누락 시 기동 실패). 일반 사용자는 가입 → 관리 페이지 승인.
2. **원천 테이블**: 관리자가 UI에서 테이블·id·시간·필드 역할을 등록(`source_registry`).
   야간 증분 적재가 자동으로 코퍼스·임베딩·검색에 반영. 필요 시 `SOURCE_TABLE_ALLOWLIST`로 제한.
3. **DataHub 도구 서버**: 관리 페이지(또는 `MCP_DEFAULT_NAME/URL/TRANSPORT` env)에 주소만 등록 —
   사내 REST 서버(GET /tools + POST /call)는 `transport=rest`.
4. 그 외 전부(그래프·세션·레지스트리·체크포인터)는 같은 Oracle에 우리가 생성 — 협의 불필요.
