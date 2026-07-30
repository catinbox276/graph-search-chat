# 조사 보고: 대화 기반 조직 지식그래프 시스템

> 조사일: 2026-07-30. 스타 수는 GitHub API 실측 기준.
>
> **핵심 결론**: 설계 전체(대화→온톨로지 추출→사용자별 그래프 병합→빈도 가중치→신규 사용자 가이드)를 통째로 구현한 오픈소스·논문은 없다. 각 단계의 검증된 조각은 존재하며, 공백은 **"사용자별 그래프의 가중 병합 + 경로 기반 방향 제안"** — 이것이 이 프로젝트의 차별점이다.

## 1. 오픈소스 커버리지

| | A. 대화→온톨로지 추출 | B. 사용자별→조직 병합 | C. 빈도 가중치·우선 노출 | D. 방향 제안·신규 편입 |
|---|---|---|---|---|
| Graphiti | ● | ◐ (group_id 격리, 병합은 단일 그래프 우회) | ◐ (커뮤니티 검출 + mentions 리랭커) | ✕ |
| cognee | ● (커스텀 온톨로지 강점) | ◐ (공유 데이터셋+권한) | ✕ | ✕ |
| mem0 | ◐ (사실 위주, 그래프는 유료화) | ◐ (Platform 전용) | ✕ | ✕ |
| Letta | ✕ (텍스트 블록, 그래프 아님) | ◐ | ✕ | ✕ |
| GraphRAG | ◐ (배치 전용) | ✕ | ● (degree/커뮤니티 가중 — 참조 구현) | ✕ |
| AWM / ExpeL | ✕ | ✕ | ◐ | ● (개념적으로; 연구 코드) |

### 주요 프로젝트

- **Graphiti** (getzep, 29K★, Apache-2.0) — 가장 유력한 코어. 대화→bi-temporal 지식그래프, Pydantic 커스텀 온톨로지, 공식 MCP 서버 내장, 모순 시 삭제 대신 시간적 무효화. https://github.com/getzep/graphiti
- **cognee** (29K★, Apache-2.0) — ECL 파이프라인, 온톨로지 커스터마이징이 더 정교. MCP 서버 내장. Graphiti의 대안 코어. https://github.com/topoteretes/cognee
- **Microsoft GraphRAG** (35K★, MIT) — 노드 차수·Leiden 커뮤니티 랭크 가중의 검증된 참조 구현. 배치 전용이라 설계만 차용. https://github.com/microsoft/graphrag
- **mem0** (62K★) — OSS에서 그래프 메모리·조직 공유가 유료 Platform으로 이동 중. 이 용도엔 부적합. https://github.com/mem0ai/mem0
- **Letta** (24K★) — Shared Memory Blocks는 텍스트 수준 공유. https://github.com/letta-ai/letta
- **AWM** (450★) https://github.com/zorazrw/agent-workflow-memory / **ExpeL** (228★) https://github.com/LeapLabTHU/ExpeL / **Voyager** (7K★) https://github.com/MineDojo/Voyager — 경험 재사용·방향 제안의 참조 (연구 코드)
- MCP 메모리 서버: 공식 knowledge-graph memory(JSONL 데모 수준) https://github.com/modelcontextprotocol/servers/tree/main/src/memory , basic-memory(개인용, AGPL) https://github.com/basicmachines-co/basic-memory

**권장 조합(최단 경로)**: Graphiti 코어 + 전 사용자 단일 조직 `group_id`(엔티티 dedup이 자동 병합, 출처는 에피소드 메타데이터 보존) + GraphRAG식 degree/커뮤니티 가중치를 쿼리 레벨 구현 + AWM식 워크플로 유도를 그래프 경로에 적용.

## 2. 관련 논문 (하위 문제별)

시스템을 5개 하위 문제로 분해: P1 대화→KG 추출 / P2 병합 / P3 가중치 / P4 가이드 / P5 확장.

| 논문 | 링크 | 매핑 |
|---|---|---|
| **Agent Workflow Memory** (Wang et al. 2024) — 궤적에서 재사용 워크플로 귀납, WebArena 51%↑ | https://arxiv.org/abs/2409.07429 | P3+P5, **가장 직접적으로 겹침** |
| **Zep: Temporal KG for Agent Memory** (2025) — 대화→실시간 bi-temporal KG (Graphiti 논문) | https://arxiv.org/abs/2501.13956 | P1+P5 |
| **Collaborative Memory** (ICML 2025) — 다중 사용자 메모리 병합 + private/shared 2계층 + provenance + 동적 접근제어. 코드 미공개 | https://arxiv.org/abs/2505.18279 | P2 핵심 참조 |
| **Agent KB** (2025) — 에이전트 간 경험 공유 KB, disagreement gate(타인 경험이 추론 방해하는 것 차단) | https://arxiv.org/abs/2507.06229 | P2+P4 |
| **AutoGuide** (2024) — 로그에서 상태-조건부 가이드라인 추출, 현재 상태 매칭분만 제공 | https://arxiv.org/abs/2403.08978 | P4 |
| **ExpeL** (AAAI 2024) — 성공-실패 궤적 쌍 비교로 인사이트 추출·축적 | https://arxiv.org/abs/2308.10144 | P3, 실패 경로 활용 근거 |
| **Reflexion** (NeurIPS 2023) — 실패→언어적 자기반성→에피소드 메모리 | https://arxiv.org/abs/2303.11366 | P3 전신 |
| **Synapse** (ICLR 2024) — 궤적 저장소 + 유사도 검색 재생 | https://arxiv.org/abs/2306.07863 | P4 |
| **Voyager** (2023) — 성장하는 스킬 라이브러리 | https://arxiv.org/abs/2305.16291 | P5 원형 |
| **AgentRR** (2025) — 실행 트레이스 record & replay, 다층 추상화 경험, 공유 저장소 비전 | https://arxiv.org/abs/2505.17716 | P3+P4 |
| **A-MEM** (2025) — Zettelkasten식 자기조직화 메모리, 고정 스키마 없이 그래프가 자람 | https://arxiv.org/abs/2502.12110 | P5 |
| **MemGPT** (2023) — 계층적 메모리 관리의 표준 참조 | https://arxiv.org/abs/2310.08560 | P2 인프라 |
| **Mem0 논문** (2025) — 사실 추출·통합(ADD/UPDATE/DELETE) | https://arxiv.org/abs/2504.19413 | P1 |
| **GraphRAG 논문** (2024) — 코퍼스→전역 그래프 + Leiden 커뮤니티 요약 | https://arxiv.org/abs/2404.16130 | P1+P2 |
| **LLMs4OL** (2023) + 2024 챌린지 — LLM 온톨로지 학습 벤치마크 | https://arxiv.org/abs/2307.16648 , https://arxiv.org/abs/2409.10146 | P1 |
| **TeQoDO** (2025) — 대화 데이터에서 온톨로지 자율 구축 | https://arxiv.org/abs/2507.23358 | P1 최근접 |
| **Procedural KG Extraction** (2024) — 절차 텍스트→절차적 KG 스키마 | https://arxiv.org/abs/2412.03589 | P1 스키마 참고 |
| **대화 로그 프로세스 마이닝** (2026) — 로그→가중 워크플로 그래프(DFG) | https://arxiv.org/abs/2607.06873 | P3, 동일 파이프라인 |
| **PM4Py.LLM** — 프로세스 마이닝 라이브러리 pm4py의 LLM 통합 | https://arxiv.org/abs/2404.06035 | P3 구현 도구 |
| **ConceptNet 5.5** — 수만 명 기여→집단 그래프+빈도 가중치의 20년 선례 | https://arxiv.org/abs/1612.03975 | P2+P3 역사적 원형 |

**관점 전환 포인트**: "반복 경로 가중치"는 학문적으로 **프로세스 마이닝**이다. directly-follows graph(DFG)의 엣지 빈도가 곧 가중치이며, pm4py를 바로 쓸 수 있다.

## 3. 알려진 문제점 (치명적인 순서)

### ① 인기 가중 피드백 루프 — 아키텍처에 내장된 최대 위험
"빈번한 경로 노출↑ → 더 빈번" 구조는 추천시스템의 퇴행성 피드백 루프와 동형. 경로 빈도가 유용성이 아니라 **노출 이력**을 측정하게 되고, 틀린 해법도 초기에 빈번하면 굳어진다.
- Jiang et al., Degenerate Feedback Loops (AIES 2019): https://dl.acm.org/doi/10.1145/3306618.3314288
- Mansoury et al., Bias Amplification (CIKM 2020): https://arxiv.org/pdf/2007.13019
- Bias/Debias 서베이: https://arxiv.org/pdf/2010.03240
- **완화**: 원시 빈도 대신 노출 대비 채택률(propensity 보정), 탐색 예산(비인기 경로 일정 비율 노출), 시스템 유도 통행은 가중치 기여 할인, 다양성 지표 병행 추적.

### ② 메모리 오염 — 대화가 곧 쓰기 채널
- **MINJA** (NeurIPS 2025): 일반 사용자가 쿼리만으로 공유 메모리 오염, 주입 성공률 98.2%. https://arxiv.org/abs/2503.03704
- **AgentPoison** (NeurIPS 2024): 오염률 0.1% 미만으로 공격 성공률 80%+. https://arxiv.org/abs/2407.12784
- 실사고: SpAIware(ChatGPT 장기 메모리 프롬프트 주입). Governed Shared Memory(공유 메모리 4대 실패 모드): https://arxiv.org/abs/2606.24535
- **완화**: 쓰기 전 검증, 출처별 신뢰 점수, provenance 기반 일괄 롤백. "검증된 완결 세션만 그래프에 쓴다"는 세션 게이트가 1차 방어선.

### ③ 엔티티 해소(dedup) 정확도 — 오류의 곱셈 전파
dedup 정확도 85%면 5-hop 경로 신뢰도 0.85⁵ ≈ 44%. 작고 정확한 그래프가 크고 노이즈 많은 그래프를 이긴다.
- https://www.sowmith.dev/blog/graphrag-entity-disambiguation
- Zep 팀 자체 보고(LLM 전용 dedup의 고비용·불안정 → 퍼지 매칭 전환): https://blog.getzep.com/llm-rag-knowledge-graphs-faster-and-more-dynamic/
- LLM 추출 실패 유형 분류: TRIAGE https://arxiv.org/pdf/2607.03447 , GraphEval https://arxiv.org/abs/2407.10793
- mem0 실사용 이슈: 의미적 중복(#4896), 모순 처리 오동작(#4536), 감쇠 부재(#5330)

### ④ 온톨로지 병합의 고전적 난제
독립 구축된 온톨로지는 개념화·granularity가 제각각 — 30년 미해결 분야. 단순 매칭 병합은 과병합과 파편화(가중치 희석)를 동시에 유발.
- Shvaiko & Euzenat, Ten Challenges: https://link.springer.com/chapter/10.1007/978-3-540-88873-4_18
- **완화**: 공유 시드 스키마 고정 + 추출을 스키마에 grounding (→ design.md의 4계층 스키마).

### ⑤ 감쇠/망각 부재 시 그래프 부패 + 비용
- 통제 없는 메모리는 수십 회 실행 내 붕괴: https://tianpan.co/blog/2026-04-12-the-forgetting-problem-when-agent-memory-becomes-a-liability
- 초기 GraphRAG 인덱싱 $33K 사례 → LazyGraphRAG로 99.9% 절감: https://github.com/microsoft/graphrag/discussions/440 , https://medium.com/graph-praxis/the-graphrag-cost-cliff-how-33-000-became-33-in-eighteen-months-be1b0fbe37e4
- **완화**: Zep bi-temporal supersession(삭제 대신 대체 마킹)이 업계 표준, 트리거 기반/지연 추출.

### ⑥ 교차 사용자 프라이버시
대화 속 이름·내부 정보가 추출된 트리플로 타인에게 노출되는 것 자체가 유출. 비식별화를 추출 단계에 내장.
- https://arxiv.org/pdf/2502.13172 , https://arxiv.org/abs/2505.18279 , https://arxiv.org/html/2604.16548v1
