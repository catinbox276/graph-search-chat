# 구현 아키텍처 (PoC 실물)

> 설계 근거는 [design.md](design.md), 실증 수치는 [poc-results.md](poc-results.md).
> 이 문서는 **현재 리포에서 실제로 돌아가는 것**의 지도다. (2026-08-02 기준)
> **배치 형태: LM Studio(모델 서빙)를 제외한 전부가 k8s 파드** — 앱(Deployment), Oracle(StatefulSet), DataHub(helm), 배치(CronJob).

## 전체 구성

```mermaid
flowchart LR
    subgraph browser["브라우저 (localhost:8500)"]
        UI["채팅 UI /<br/>SSE 토큰 스트리밍·접이식 단계"]
        GV["지식그래프 뷰 /graph<br/>force-directed·상세 패널"]
    end
    subgraph srv["앱 파드 gsc-app (Deployment, 이미지 1개 = 모놀리스)"]
        API["/chat·/chat/stream(SSE)<br/>/models·/admin/models/*<br/>/graph/data·/stats·/reload"]
        MEM["MemorySaver<br/>(멀티턴 기억, thread=세션)"]
        MTX["임베딩 행렬 1.4GB<br/>(기동 시 Oracle에서 로드)"]
    end
    subgraph agentbox["에이전트 A (DeepAgents)"]
        AG["suggest_paths → 도구 선택<br/>모델별 캐시"]
    end
    LMS["LM Studio :1234<br/>LLM 4종 + 임베딩 3종"]
    subgraph ora["D: Oracle 파드 (k8s, :1521)"]
        T1["blog_posts (+embedding BLOB)"]
        T2["sessions (증거·판정)"]
        T3["nodes/edges/node_evidence"]
        T4["suggestions·model_registry"]
    end
    DH["C: DataHub (helm 파드 ~10개)<br/>:9002 UI / GMS는 클러스터 DNS<br/>샘플 67 + BIRD 19테이블"]
    MCP["B: mcp-server-datahub<br/>(공식 MCP, stdio)"]
    UI & GV --> API
    API --> AG
    AG -->|"함수: blog_search·path_suggest"| ora
    AG -->|MCP| MCP --> DH
    AG --> LMS
    MTX -.-> ora
    subgraph batch["CronJob (앱과 같은 이미지)"]
        P2["graph-pipeline 03:00<br/>게이트→추출→병합 (UI 세션 포함)"]
        P3["embed-backfill 03:30<br/>새 문서 임베딩"]
    end
    P2 & P3 --> ora
```

## 저장소 (Oracle 단일 DB — design §5 원칙 유지)

| 테이블 | 내용 | 만든 곳 |
|---|---|---|
| `blog_posts` | 노하우 코퍼스 343,045건 + `embedding` BLOB(질의·dedup용 원본) | load_oracle.py / embed_corpus.py |
| `sessions` | 대화 증거 계층: 질문·툴호출·답변·게이트 판정(verdict) | server.py / selfplay.py |
| `nodes` / `edges` / `node_evidence` | 4계층 지식그래프 + 가중치 + 출처 | graph_pipeline.py |
| `suggestions` | 경로 제안 노출 기록 (채택률 보정용 데이터) | path_suggest.py |
| `model_registry` | 모델 등록·기본값 (LLM=사용자, 임베딩·리랭커=관리자) | model_registry.py |

## 주요 컴포넌트와 파일

| 파일 | 역할 | 핵심 결정 |
|---|---|---|
| `agent/agent.py` | DeepAgents 에이전트. 툴 = suggest_paths + 블로그 검색 2종 + DataHub MCP | 새 문제 → suggest_paths 먼저 (시스템 프롬프트) |
| `tools/blog_search.py` | 하이브리드 검색: Oracle Text(lexical) + 인메모리 행렬 코사인(semantic) → RRF | 검색당 임베딩 계산은 질의 1건뿐 |
| `tools/path_suggest.py` | 그래프 방향 제안 + 실패 이력 경고 | 성공/실패는 판정 카운트 (불리언 금지 — 실증됨) |
| `tools/model_registry.py` | 모델 등록/기본값. LM Studio 동기화 | 임베딩 교체 시 재백필 경고 |
| `app/server.py` | FastAPI: SSE 스트리밍, 세션 기록, 모델 API(관리자 토큰), 그래프 데이터 | HTML no-store (캐시 사고 방지) |
| `app/index.html` `graph.html` `shell.css` | 통합 UI 셸(노드-엣지 네비), 예시 칩, force-directed 그래프+패널 | 색=의미: 파랑 경로·초록 검증·빨강 실패우세 |
| `poc/selfplay.py` `graph_pipeline.py` | 47세션 생성 / 게이트→4계층 추출→병합(코사인 0.72)→가중치 | 캘리브레이션 실측 기반 |
| `scripts/` | 코퍼스 빌드·Oracle 적재·임베딩 백필(병렬 4)·BIRD ingest | |
| `k8s/oracle.yaml` | Oracle StatefulSet+PVC+probe (사내 19c로 이미지만 교체) | |

## API 요약 (localhost:8500)

- `POST /chat/stream` — SSE: token(생각·답변 실시간) / tool / tool_end(정리된 응답) / answer
- `POST /chat` — 비스트리밍 (스크립트용). 둘 다 `model` 필드로 LLM 선택, 멀티턴 기억
- `GET /models` — 사용자용 LLM 목록 · `POST /admin/models/{sync,select}` — 관리자(X-Admin-Token)
- `GET /graph/data` — 노드(사용·성공·실패 카운트)·엣지 · `GET /stats` · `GET /reload`(임베딩 행렬 갱신)

## 실행 방법 (전부 파드)

```bash
# 전제: Docker Desktop(k8s 활성) + LM Studio(:1234, 모델 로드) — 모델 서빙만 호스트
kubectl create secret generic mysql-secrets --from-literal=mysql-root-password=datahub
helm repo add datahub https://helm.datahubproject.io/
helm install prerequisites datahub/datahub-prerequisites
helm install datahub datahub/datahub \
  --set global.datahub.metadata_service_authentication.enabled=false  # PoC 한정
kubectl apply -f k8s/oracle.yaml   # Oracle StatefulSet
docker build -t graph-search-chat:latest .
kubectl apply -f k8s/app.yaml -f k8s/cron.yaml   # 앱 Deployment + 야간 CronJob 2종
# 데이터 준비(1회, 호스트): build_corpus.py → load_oracle.py → embed_corpus.py → ingest_bird.py
# 접속: 채팅 :8500 / 그래프 :8500/graph / DataHub :9002 (datahub/datahub)
# 사내 전환: 이미지를 레지스트리로 push, MODEL_URL을 vLLM 서비스로, 인증·Secret 활성화
```

## 알려진 한계 (사내 전환 시 체크리스트)

1. 로컬은 대형 LLM 1개만 동시 로드(LM Studio, 유일한 비파드 구성요소) — GPU 서빙(vLLM 파드)에서 드롭다운 선택 그대로 동작
2. ~~멀티턴 기억은 서버 메모리~~ → **Oracle 체크포인터로 외부화 완료** (lg_checkpoints/lg_writes) — 복제본 공유·재시작 생존
3. 관리자 인증은 단일 토큰(PoC) — 사내는 SSO 연동
4. 리랭커는 레지스트리 슬롯만 존재(검색 파이프라인에 리랭크 단계 미구현)
5. 야간 CronJob이 UI 세션 포함 미판정분을 자동 처리 (03:00 파이프라인, 03:30 임베딩). 로컬은 야간에 맥·LM Studio가 켜져 있어야 동작
6. 나머지는 poc-results.md "남은 것" 참조 (채택률 보정, supersession 자동화 등)

## 배포 모드 전환 (standalone ↔ cluster)

Milvus·OpenSearch가 설치 모드를 나누는 방식처럼, 오라클·모델 서빙을 제외한 컴포넌트를 두 모드로 운용한다. 전환은 kustomize 적용 한 줄 — 데이터·설정 마이그레이션 없음.

### 스탠다드 → 클러스터

```bash
kubectl apply -k k8s/cluster        # 앱 2복제본 + 세션 고정(ClientIP)
kubectl rollout status deploy/gsc-app
kubectl get pods -l app=gsc-app     # 2/2 Ready 확인

# DataHub도 클러스터로 (선택, 사내 규모에서):
helm upgrade datahub datahub/datahub --reuse-values -f k8s/datahub-values-cluster.yaml
```

### 클러스터 → 스탠다드 (롤백)

```bash
kubectl apply -k k8s/base           # 복제본 1로 축소, 세션 고정 해제
```

### 전환 시 주의사항

1. **멀티턴 기억**: Oracle 체크포인터로 외부화되어 **세션 고정 불필요 — 복제본 간 자유 라우팅** (요청이 어느 파드로 가든 기억 공유, 재시작에도 생존). 전환 시 기억 관련 주의사항 없음
2. **리소스**: 앱 복제본당 메모리 요청 3Gi(임베딩 행렬 1.4GB 포함) — 복제본 수 × 3Gi 여유 확인. 로컬 검증 때 두 번째 복제본이 노드 disk-pressure로 Pending된 사례 있음 → `docker system prune`으로 해소
3. **CronJob은 모드 무관** — concurrencyPolicy: Forbid라 복제본 수와 무관하게 단일 실행
4. **Oracle·모델 서빙은 모드 대상 아님** — Oracle은 StatefulSet 단일(사내 HA는 DB 팀 영역), 모델 서빙은 호스트 LM Studio(사내는 vLLM 파드로 별도 구성)
