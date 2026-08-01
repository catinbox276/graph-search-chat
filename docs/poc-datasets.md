# PoC용 공개 데이터셋 카탈로그

> 조사일: 2026-08-01 (웹 실사 기준). 사내 데이터를 쓸 수 없을 때 PoC를 대체 구동할 공개 데이터 목록.
> 역할 정의는 [design.md §0](design.md) 참조 — Role A = 사내 블로그(문제→해결 글) 대체, Role B = DataHub(분석가 질문·테이블 조인) 대체.

## 종합 추천 (즉시 다운로드 가능 조합)

1. **Role A**: Ask Ubuntu + Super User 덤프 (archive.org, 합계 ~2.4GB, CC BY-SA) → 채택답변 필터 → 필요분만 LLM 한국어 번역. 한국어 실데이터 소량은 네이버 지식인 공개본(1.7K행) 추가
2. **Role B**: BIRD dev(질문 1,534 + 스키마 + 정답 SQL) + TPC-DS를 Postgres에 적재 후 DataHub 커넥터로 ingest. 카탈로그 장식은 `datahub docker ingest-sample-data`
3. **연결 시나리오 팁**: RelBench의 `rel-stack`(StackExchange 관계형 DB 7테이블)을 쓰면 Role A(질문 글)와 Role B(테이블 조인)가 **같은 도메인**으로 자연스럽게 이어진다

## Role A — "사내 블로그" 대체: 문제→해결 트러블슈팅 코퍼스

### StackExchange 공식 덤프 (1순위)

| 항목 | 내용 |
|---|---|
| 다운로드 | https://archive.org/details/stackexchange_20251231 (2025-12-31 최신 미러) / 과거분 https://archive.org/details/stackexchange |
| 형식·크기 | 사이트별 7z(XML). superuser 1.3GB, askubuntu 1.1GB, serverfault 862MB (전체 91.6GB — 사이트별 선택) |
| 라이선스 | CC BY-SA 4.0 (출처표시 시 상업 이용 가능) |
| 게이트 | 없음 (즉시) |
| PoC 매핑 | "pip이 프록시 뒤에서 안 됨 → 설정파일 수정" 류 문제→답변 쌍 그대로. `AcceptedAnswerId` 필터로 고품질만 추출 |

파싱 없이 바로 쓰는 Hugging Face 가공본:
- https://huggingface.co/datasets/HuggingFaceH4/stack-exchange-preferences — 사이트별 폴더, 질문+답변 랭킹 쌍
- https://huggingface.co/datasets/common-pile/stackexchange — 질문+답변을 문서 단위로 묶음 (RAG 인덱싱 적합)

### 한국어 옵션

| 데이터셋 | URL | 내용 | 게이트 | 평가 |
|---|---|---|---|---|
| 네이버 지식인 Q&A 공개본 | https://huggingface.co/datasets/CertifiedJoon/Korean-Instruction | 실제 지식인 1,720행 (CDLA-Permissive 2.0) | 없음 | 즉시 사용 가능한 유일한 지식인 공개본. 소규모 |
| AI Hub 민원 콜센터 Q&A | https://aihub.or.kr/aidata/30716 | Q&A 110만 쌍 | **한국 계정+승인 (1~2일)** | 문제→해결 형태 최유사, 대량 |
| KorQuAD 1.0/2.0 | https://korquad.github.io/ | 위키 MRC | 없음 | **2.0은 CC BY-ND — 가공·재배포 제한 주의.** 검색 품질 테스트 보조용 |
| velog / 요즘IT 크롤 | — | 기성 덤프 없음 확인 | — | 직접 크롤링 필요 (velog는 GraphQL 열려 있어 난도 낮음). PoC 기간 고려 시 비추천 |

**결론**: 즉시 받을 수 있는 한국어 기술 트러블슈팅 덤프는 사실상 없다. 현실적 방안 = StackExchange(영어, 대량) 일부를 LLM 번역해 "사내 블로그" 코퍼스 합성 + 지식인 1.7K(한국어 실데이터).

### 보조: GitHub 이슈·IT 티켓

- https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified — 이슈+해결 패치 500건, 인간 검증. 문제→해결 매핑이 가장 깨끗함
- https://huggingface.co/datasets/bigcode/the-stack-github-issues — 대규모 이슈+댓글 (대화형 해결 과정 포함)
- https://www.kaggle.com/datasets/tobiasbueck/multilingual-customer-support-tickets — 상담원 답변 포함 IT 티켓 (Kaggle 가입만). 다른 티켓 데이터셋 다수는 합성이라 비추천

## Role B — DataHub / 데이터 분석가 대체

### Text-to-SQL 벤치마크 (분석가 질문 + 스키마 + 정답 SQL)

| 데이터셋 | URL | 내용 | 게이트 |
|---|---|---|---|
| **BIRD-SQL** ★최적 | https://bird-bench.github.io/ | 12,751 질문-SQL, 95 DB(37 도메인), 도메인 지식(evidence) 주석. dev 1,534문항 ~500MB | 없음 (CC BY-SA) |
| Spider 1.0 | https://yale-lily.github.io/spider | 질문 10K + 200 DB 스키마 + SQLite (~1GB). 스키마가 작고 깔끔해 데모 시연용 | 없음 (CC BY-SA) |
| Spider 2.0 | https://github.com/xlang-ai/Spider2 | 엔터프라이즈급 595 태스크 (700~3,000 컬럼) | **BigQuery/Snowflake 계정 필요 — PoC엔 과함.** 로컬 실행분 `spider2-lite`만 고려 |

### DataHub 데모 메타데이터

- 내장 샘플: `datahub docker ingest-sample-data` — datasets/dashboards/리니지 일괄 적재. 큐레이션 팩(`showcase-ecommerce`, `covid-bigquery`) 지원: https://docs.datahub.com/docs/generated/ingestion/sources/demo-data
- 추천 흐름: 샘플 팩으로 카탈로그 채우고, 실제 질의 대상 테이블은 아래 데이터를 Postgres/DuckDB에 넣고 커넥터로 ingest → 진짜 스키마+리니지 생성

### 멀티테이블 스키마 데이터 (조인·리니지 시나리오)

| 데이터셋 | 획득 | 구조 | 평가 |
|---|---|---|---|
| TPC-H | DuckDB `INSTALL tpch` (1분) | 8테이블 3NF | 조인 시나리오 표준 |
| TPC-DS | DuckDB `INSTALL tpcds` | 24테이블 스노플레이크 | 테이블 수가 많아 카탈로그 데모에 더 그럴듯함 |
| RelBench | https://relbench.stanford.edu / `pip install relbench` | 실데이터 7종 (rel-stack, rel-amazon, rel-f1 등 3~15테이블) | **rel-stack이 Role A와 도메인 연결됨** |

## 주의사항
- AI Hub는 전 데이터 승인 게이트(한국 휴대폰 인증). 급하면 배제
- KorQuAD 2.0은 ND 라이선스 — 파생물 제한
- Spider 2.0 풀버전은 클라우드 크리덴셜 필요
- CC BY-SA 데이터를 사내 시연 이상으로 쓰려면 출처표시·동일조건 의무 확인
