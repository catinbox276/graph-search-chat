# 2026-08-10 — 사용자 제어 설계의 선례 조사: 사람이 통제하는 지식 시스템은 성공했는가

> **왜 이 조사를 했나**: 이 프로젝트의 설계 철학 — "LLM에게 전권을 주지 않는다. 사용자의
> 도메인 경험에 최대한 의존해 지식화하고, 사용자가 직접 제어·수정하게 하며, 사용자가
> 잘할수록 에이전트가 좋아지는 시스템을 만든다" — 를 plan.md에 명문화하기 전에,
> **같은 방향을 시도한 선례들이 실제로 어떤 결과를 냈는지** 확인하기 위해서다.
> 철학이 아니라 실증으로 방향을 정당화(또는 기각)하는 것이 목적이었다.
>
> **무엇을 조사했나**: 두 갈래 병렬 조사 —
> ① 사람이 통제하는 지식베이스·지식그래프 구축의 역사적/현행 사례와 성과
> ② 사용자 제어형 AI 메모리·사용자 피드백 플라이휠 제품·연구의 성과
>
> **결론 요약**: 실패한 것은 양극단(전자동·전수 수작업·무통제 입력)이었고, 살아남은 설계는
> "사람이 기준을 정의하고, 애매한 것만 사람에게 보내는 선택적 게이트"였다 — 이 프로젝트의
> 방향과 일치한다. 단 3가지 조건(사람 배치는 사람이 우위인 지점에만 / 행동 신호는 숙련
> 가중 필요 / 기여가 본인 이득으로 돌아와야 지속)과 2가지 보완 과제(탐색 노출, 사용자
> 숙련 가중)가 확인됐다.

---

## 파트 A — 사람이 통제하는 지식그래프 구축의 선례

### A1. Freebase vs Wikidata — 커뮤니티 큐레이션 KG의 명암

- Freebase(2007, Metaweb→Google 인수)는 커뮤니티+자동 수집 혼합 개방형 KG, 4,600만 토픽 —
  Google Knowledge Graph의 핵심 원천이었으나 2014-12 폐쇄, "개방 협업 지식베이스는
  Wikidata가 더 적합"이 공식 사유 ([Wikipedia: Freebase](https://en.wikipedia.org/wiki/Freebase_(database)),
  [Search Engine Land](https://searchengineland.com/google-close-freebase-helped-feed-knowledge-graph-211103))
- **이관은 사실상 실패**: Wikidata 커뮤니티가 출처 기준 미달을 이유로 일괄 자동 임포트를
  거부, 사람이 한 건씩 승인하는 도구를 요구 — 2019년까지 1,000만 후보 중 **52.8만 건(약
  5%)만 큐레이션 완료** ([Wikidata WikiProject Freebase](https://www.wikidata.org/wiki/Wikidata:WikiProject_Freebase),
  [The Great Migration 논문](https://dl.acm.org/doi/10.1145/2872427.2874809))
- Wikidata의 생존: 2025년 약 1억 2,200만 아이템·누적 25억 편집·활동 편집자 4.1만 명 —
  "출처 강제 + 인간 검증 게이트 + 개방 커뮤니티"가 지속 요인
  ([Wikidata:Statistics](https://www.wikidata.org/wiki/Wikidata:Statistics))
- 교훈: 검증된 소량이 미검증 대량을 이긴다. 동시에 인간 게이트의 처리량 한계(5% 소화)도
  같은 사건이 보여줌.

### A2. Google Knowledge Vault — 전자동 추출의 미출시

- 2014년 웹 스케일 자동 추출 KB — 16억 사실 수집, 그중 **신뢰 가능("90%+ 참") 사실은
  2.71억(약 17%)** ([Google Research 논문](https://research.google/pubs/pub45634/),
  [Wikipedia: Google Knowledge Graph](https://en.wikipedia.org/wiki/Google_Knowledge_Graph))
- 결과: 제품화되지 않음 — Google이 "연구 프로젝트일 뿐"이라 해명 후 언급 중단. 실서비스
  Knowledge Graph는 인간 큐레이션 원천 기반 유지
  ([Search Engine Land](https://searchengineland.com/hold-horses-knowledge-vault-just-research-project-now-204549))
- 2차 피해: Knowledge Vault 산출물을 쓴 Freebase→Wikidata 이관 도구가 품질 불신으로 좌초 —
  자동 추출 산출물은 인간 검증 커뮤니티에서 "부채"로 취급됨.

### A3. CYC / 전문가 시스템 — 40년 수작업 온톨로지의 평결

- 1984년부터 상식 전체를 수작업 논리 규칙으로 입력 — 최종 약 3,000만 어서션, **$2억,
  2,000인년** 투입 ([ACM](https://dl.acm.org/doi/10.1145/219717.219745),
  [Yuxi Liu의 Cyc 에세이](https://yuxi-liu-wired.github.io/essays/posts/cyc/))
- 평결: 실패 — 상업 용도는 표준 전문가시스템 수준, "더 높은 지능"의 증거 없음
  ([Obituary for Cyc](https://mjtsai.com/blog/2025/04/14/obituary-for-cyc/))
- 단 수작업 지식의 '검증 가능성' 가치는 LLM 시대에 재평가 중
  ([Lenat & Marcus 2023](https://arxiv.org/pdf/2308.04445))

### A4. Palantir Foundry Ontology — 인간 정의 온톨로지의 상업적 검증

- 사람이 명시적으로 모델링한 온톨로지(객체·링크·액션)를 모든 앱·AI 에이전트의 공통 기반
  이자 가드레일로 삼는 구조 ([Palantir Docs](https://www.palantir.com/docs/foundry/ontology/overview),
  [Palantir Blog: Connecting AI to Decisions](https://blog.palantir.com/connecting-ai-to-decisions-with-the-palantir-ontology-c73f7b0a1a72))
- 결과: AIP(LLM+온톨로지) 출시 후 급성장, FY2025 매출 약 $45억
  ([FY2025 연차보고서(SEC)](https://www.sec.gov/Archives/edgar/data/0001321655/000132165526000021/fy2025palantirars.pdf)).
  온톨로지 구축·유지 비용과 벤더 종속은 비판점
  ([Towards AI 분석](https://pub.towardsai.net/palantir-foundry-ontology-how-it-works-what-problems-it-solves-and-where-it-falls-short-d8b4a1ae4900))

### A5. Stack Overflow — 평판으로 책임을 결속한 큐레이션, 그리고 고사

- 투표·평판·권한 위계로 품질과 책임을 결속 — 15년간 개발 지식의 표준
  ([평판 시스템 분석 연구](https://www.researchgate.net/publication/262366731_Analysis_of_the_reputation_system_and_user_contributions_on_a_question_answering_website_StackOverflow))
- LLM 시대 붕괴: 월 질문 수 2022-11 10.9만 → 2024-12 2.56만(**−76.5%**)
  ([Pragmatic Engineer](https://blog.pragmaticengineer.com/stack-overflow-is-almost-dead/),
  [ppc.land](https://ppc.land/stack-overflow-traffic-collapses-as-ai-tools-reshape-how-developers-code/))
- 역설: 인간 큐레이션 15년 치가 LLM 학습용 유료 자산으로 재확인 — OpenAI·Google 라이선스
  계약 ([IT Pro](https://www.itpro.com/technology/artificial-intelligence/openais-deal-with-stack-overflow-just-landed-it-goldmine-of-developer-data-heres-why-itll-transform-llm-development))
- 교훈: 책임 결속은 품질을 만들지만, **기여 인센티브가 소비 경로와 분리되면 커뮤니티는
  고사한다.**

### A6. 학술 HITL-KG 문헌 (2020–2026) — 선택적 게이트의 실증

- LLM+인간 통합 검증: 정밀도 +12%, 단 재현율 하락으로 F1 −5% — 트레이드오프 측정
  ([IPM 2025](https://www.sciencedirect.com/science/article/pii/S030645732500086X))
- **자동 판정이 불일치한 것만 인간 검토**: 158건 주석만으로 F1 +5pt — 전수·전자동 모두
  능가. 산출물의 5–6%만 검증해도 유의미 개선 ([같은 논문](https://dl.acm.org/doi/10.1016/j.ipm.2025.104145))
- 온톨로지 공학 SLR: 정확성·의미 충실성 평가에는 전문가-in-the-loop이 여전히 필수
  ([Semantic Web Journal SLR](https://www.semantic-web-journal.net/system/files/swj3864.pdf))
- 역사적 대조군 NELL(CMU 2010–): 자동 학습 6개월 후 카테고리 1/4이 정밀도 25–60%로 붕괴 —
  "실수가 실수를 학습"하는 드리프트. 주기적 인간 오류 라벨링이 교정 수단
  ([NELL AAAI 2010](https://burrsettles.com/pub/carlson.aaai10.pdf),
  [CACM 2018](https://dl.acm.org/doi/10.1145/3191513))

### A7. 기업 지식관리의 교훈

- 기업 위키 실패 패턴: 거버넌스 실종("저자 평등"), 정보 부패, 참여 임계 미달
  ([Bloomfire](https://bloomfire.com/blog/disadvantages-of-using-corporate-wikis/),
  [Starmind](https://www.starmind.com/blog/corporate-wiki-for-knowledge-management))
- Deloitte 2020: 조직 75%가 지식 보존을 중요하다 했지만 준비된 곳은 9%, 37%가 "기여
  인센티브 부재"를 장벽으로 지목
  ([Deloitte](https://www.deloitte.com/us/en/insights/topics/talent/human-capital-trends/2020/knowledge-management-strategy.html))
- 성공 조건: "누구나 쓴다"가 아니라 "누군가 책임지고 승인한다"(모더레이터 승인 게이트)
- 보조 사례 — Gene Ontology: 좁은 도메인 + 명확한 검증 기준 + 전문가 커뮤니티 조건에서
  수작업 큐레이션이 수십 년 지속 가능함을 증명, 크라우드소싱+검증 하이브리드로 진화
  ([GO 주석 모범사례](https://pubmed.ncbi.nlm.nih.gov/27812934/),
  [CACAO](https://journals.plos.org/ploscompbiol/article?id=10.1371%2Fjournal.pcbi.1009463))

---

## 파트 B — 사용자 제어형 AI 메모리와 피드백 플라이휠의 선례

### B1. ChatGPT Memory — 열람·수정·삭제형 메모리의 시행착오

- 2024-02 출시, "사용자가 메모리를 통제한다"가 설계 근거 — 관리 UI, 개별/전체 삭제,
  임시 채팅 ([OpenAI](https://openai.com/index/memory-and-new-controls-for-chatgpt/))
- 문제: 사용자 연구에서 대다수가 저장된 기억을 보고 부정적 기대 위반 경험, "정말 잊는지"
  불신 ([CHI 2026](https://dl.acm.org/doi/full/10.1145/3772318.3791635));
  삭제한 메모리가 되살아나는 버그 ([piunikaweb](https://piunikaweb.com/2025/10/01/chatgpt-users-cant-delete-memory/));
  사용자들은 "삭제 후 재작성"이 아닌 직접 편집을 요구
  ([OpenAI 커뮤니티](https://community.openai.com/t/editing-memories-instead-of-delete-and-recreate/979193))
- 교훈: 열람·삭제권만으로 부족 — **부분 편집 + 삭제가 관철된다는 신뢰**가 통제감의 핵심.

### B2. Claude Memory (Anthropic) — 현재의 수렴 설계

- 완전 옵트인 + 기억 전체 가시화 + **직접 편집** + 프로젝트별 분리 메모리 + 시크릿 채팅
  ([Anthropic](https://www.anthropic.com/news/memory))
- 스코프 분리(오염 반경 축소)와 편집 가능한 요약문이 업계 수렴점으로 평가
  ([MacRumors](https://www.macrumors.com/2025/10/23/anthropic-automatic-memory-claude/))

### B3. MemGPT/Letta · Zep · Mem0 — 에이전트 메모리 프레임워크

- Letta: 에이전트가 자기 메모리를 스스로 편집(품질이 모델 판단에 의존),
  Mem0: 시스템 자동 추출·조정, Zep(Graphiti): **바이-템포럴 무효화**(삭제 대신 `invalid_at`
  표식 — 본 프로젝트의 supersession 설계와 동일 계보)
  ([Zep 논문](https://arxiv.org/abs/2501.13956), [비교 분석](https://vectorize.io/articles/mem0-vs-letta))
- 학계 보고: 사용자가 우기면 잘못된 기억이 굳는 아부 편향
  ([MemSyco-Bench](https://arxiv.org/pdf/2607.01071)), 장기 배포 페르소나 드리프트
  ([연구](https://arxiv.org/pdf/2605.09863))
- 교훈: 자동 추출·자기 편집 모두 오염·누락 리스크 — 삭제가 아닌 시점 무효화가 감사가능성
  면에서 우위.

### B4. GitHub Copilot — 수락률 플라이휠의 실증과 함정

- 수락률이 체감 생산성의 최선의 예측자 (2,631명 설문×IDE 계측)
  ([CACM 2024](https://cacm.acm.org/practice/taking-flight-with-copilot/), [원 논문](https://arxiv.org/pdf/2205.06537))
- 사용 3개월차 수락률 28.9% → 6개월차 34% — 사용자 숙련과 신호가 함께 좋아지는 플라이휠
  ([통계](https://gitnux.org/github-copilot-statistics/)); 수락 코드의 88%가 최종 커밋에 잔존
  ([Accenture 실험](https://github.blog/news-insights/research/research-quantifying-github-copilots-impact-in-the-enterprise-with-accenture/))
- **함정**: 초보일수록 오답도 수락 — 초보 집단에서 수락률은 품질 신호로 부적합
  ([연구](https://arxiv.org/pdf/2405.17739)) → 행동 신호는 사용자 숙련 가중이 필요.

### B5. 추천 시스템 피드백 루프 — "제안→추종→강화" 교정의 표준

- 퇴행 루프의 정식화와 처방(상시 랜덤 탐색)
  ([DeepMind AIES 2019](https://arxiv.org/abs/1902.10730));
  노출·선택·인기 편향 서베이 ([ACM TOIS](https://dl.acm.org/doi/10.1145/3564284))
- 표준 교정 IPS(역성향점수)는 작동하나 고분산 — 실무는 노출 로깅 + 채택률 보정 + 탐색
  트래픽 조합으로 수렴 ([YouTube WSDM 2019](https://arxiv.org/abs/1812.02353))
- 본 프로젝트의 suggestions 노출 기록 + 채택률 보정은 문헌 표준과 일치. **탐색 노출만 미비.**

### B6. 인간-AI 상보성 연구 — 사람 개입의 조건

- 찬성: AI 확신도 기반 혼합 판정이 각자 단독보다 우수(91.3%) — 단 **원증거를 보여줄 때만**;
  설명·확신도 노출은 과의존 유발 ([DeepMind FAccT 2026](https://arxiv.org/abs/2510.26518))
- **반대: Nature 2024 메타분석(106개 실험)** — 인간+AI 조합은 평균적으로 각자 단독의
  최선보다 나빴음. 이득은 인간이 AI보다 잘하는 과제에서만
  ([Nature Human Behaviour](https://www.nature.com/articles/s41562-024-02024-1))
- AI 설명은 성과를 못 올리고 수락 확률만 높임(과의존)
  ([CHI 2021](https://arxiv.org/pdf/2006.14779)); 방사선과에서 "AI 보조 인간"보다 케이스를
  인간 또는 AI에 통째 배정이 최적 ([NBER w31422](https://www.nber.org/papers/w31422))
- 교훈: **사람 게이트는 배치가 전부** — 사람이 우위인 판정에만.

### B7. 무통제 입력의 실패 사례

- Microsoft Tay: 트롤 입력 학습 → **16시간 만에 셧다운**
  ([사례 연구](https://ethicsunwrapped.utexas.edu/case-study/a-i-trust-tays-trespasses))
- 이루다 1.0(한국): 오염 훈련 + 개인정보 유출 → **20일 만에 중단**, 과징금 —
  게이트를 넣은 2.0은 재출시 후 무사고 운영 (게이트 유무의 대조 실험적 사례)
  ([The Conversation](https://theconversation.com/from-chatbot-to-sexbot-what-lawmakers-can-learn-from-south-koreas-ai-hate-speech-disaster-247152),
  [재출시 분석](https://pmc.ncbi.nlm.nih.gov/articles/PMC12882987/))
- Amazon: 자동 학습 채용 AI 폐기 ([CIO](https://www.cio.com/article/222427/amazons-biased-ai-recruiting-tool-gets-scrapped.html)),
  무검증 크라우드소싱 Alexa Answers의 품질 사고
  ([VentureBeat](https://venturebeat.com/ai/amazon-alexa-answers-vetting-user-questions))
- 메모리 오염 공격(2025–2026): 쿼리만으로 장기 메모리 오염(MINJA, 성공률 95%+ — 다른
  사용자에게 전파) ([MINJA](https://arxiv.org/pdf/2503.03704));
  **LLM-judge 기반 방어가 사회공학으로 우회됨** ([2026 연구](https://arxiv.org/abs/2601.05504)) —
  LLM 판정 단독이 아닌 구조적 방어(게이트·provenance·교차 검증)가 필요.

### B8. "HITL by design" 제품들의 성적표

- Humanloop(HITL 플랫폼): 2025-08 Anthropic이 팀만 영입, 플랫폼은 종료
  ([TechCrunch](https://techcrunch.com/2025/08/13/anthropic-nabs-humanloop-team-as-competition-for-enterprise-ai-talent-heats-up))
- Scale AI: "루프 안의 인간의 질"이 곧 제품 — 전문가 HITL 데이터 자체가 사업으로 성립
  ([Scale RLHF](https://scale.com/rlhf))
- 센타우로 체스: 인간+엔진 > 엔진의 원조 서사였으나, 엔진이 인간 기여분을 흡수하자 순수
  엔진이 추월 — **인간 증폭 서사에는 유통기한이 있다**
  ([Marginal Revolution](https://marginalrevolution.com/marginalrevolution/2024/02/centaur-chess-is-now-run-by-computers.html))

---

## 종합 결론

**1. 실패는 양극단에서 났다.**
- 전자동: Knowledge Vault(17%만 신뢰 가능 → 미출시), NELL(오류 자기증폭 드리프트),
  Tay(16시간)·이루다 1.0(20일) — "잘못되면 걷잡을 수 없다"의 실증
- 전수 수작업: CYC($2억·2,000인년 실패), Wikidata의 처리량 병목(Freebase의 5%만 소화)

**2. 살아남은 설계는 이 프로젝트의 방향과 일치한다.**
- 사람이 기준(온톨로지·출처 규칙)을 정의하고 AI가 그 안에서 일함 — Palantir(FY2025 $45억),
  Wikidata(유일 생존 개방 KG)
- 애매한 것만 사람에게 보내는 선택적 게이트 — 5~6% 검증으로 F1 +5pt (2025 실증)
- 사용자 행동이 품질 신호가 되는 플라이휠 — Copilot (숙련 상승과 신호 개선이 동행)
- 삭제 대신 시점 무효화(bi-temporal) — Zep/Graphiti와 동일 계보

**3. 방향을 유지하기 위한 조건 3가지.**
- **사람 배치는 사람이 우위인 지점에만** (Nature 메타분석: 아무 데나 끼우면 평균 마이너스) —
  도메인 정의·승인·성공/실패 라벨·분기 확정은 사람, 추출·병합·검색은 자동
- **행동 신호는 숙련 가중이 필요** (Copilot: 초보 수락률은 무효 신호)
- **기여가 본인 이득으로 즉시 돌아와야 지속** (Stack Overflow: 인센티브가 소비 경로와
  분리되자 고사) — 본 프로젝트는 좋은 세션 기여 → 본인이 받을 경로 제안 품질로 회귀하는
  닫힌 고리

**4. 문헌 대비 보완 과제 2가지 (추후).**
- 탐색 노출: 비추천 경로도 일정 비율 노출해 피드백 루프 퇴행을 늦추기 (DeepMind 2019 처방)
- 사용자 숙련 가중: 현재는 모든 사용자 세션이 같은 무게

**5. 설계 철학의 명문화 (plan.md 반영용).**
> 자동화는 안전망(잘못돼도 번지지 않게), 사용자 제어는 증폭기(잘할수록 좋아지게).
> LLM에게 전권을 주지 않는 이유는 편의가 아까워서가 아니라, 잘못이 걷잡을 수 없이 번지고
> 제어할 수 없기 때문이다. 이 서비스는 사용자의 도메인 경험에 최대한 의존해 그것을
> 지식화하고, 사용자가 직접 수정·정제하게 함으로써 도메인을 더 깊이 녹여낸다.
> 목표: 사용자가 잘하면 잘할수록 에이전트가 좋아지는 시스템.
