# 인메모리 하이브리드 검색 설계 (Oracle 원본 + SQLite FTS5 + sqlite-vec)

> 작성일: 2026-08-11 (2026-08-12 개요·설명 보강)
> 대상 독자: 개발자 및 비개발 이해관계자
> 요약: **원본 데이터는 Oracle에 그대로 두고, 검색은 애플리케이션 프로세스 메모리 안의
> SQLite 색인 두 개(단어용·의미용)로 수행한다. 디스크에 파일을 남기지 않으며, Oracle에
> 추가 권한이 필요하지 않다.**

---

## 0. 개요 (비전문가용 설명)

### 0.1 검색의 두 가지 방식 — 도서관 비유

검색은 도서관에서 원하는 책을 찾는 일에 비유할 수 있다. 찾는 방식은 두 가지다.

1. **단어로 찾기** — "환불"이라는 낱말이 들어간 자료를 찾는다. 정확한 단어가 있어야 한다.
2. **의미로 찾기** — "돈을 돌려받는 방법"으로 질의해도 "환불 규정" 자료를 찾아준다. 낱말이 달라도 *의미*가 비슷하면 검색된다.

두 방식은 서로 장단점이 다르므로, **함께 사용하면 검색 품질이 크게 향상된다.** 이를 **하이브리드 검색**이라 한다.

- 단어 기반 = **렉시컬(lexical) 검색** — 본 설계에서는 `FTS5` 사용
- 의미 기반 = **시맨틱(semantic)·임베딩 검색** — 문장을 숫자 목록(**벡터**)으로 바꿔, 숫자가 가까우면 의미가 유사한 것으로 판단한다. 본 설계에서는 `sqlite-vec` 사용

> **임베딩:** 문장을 컴퓨터가 비교할 수 있도록 **숫자 목록(벡터)** 으로 변환한 것. 의미가 유사한 문장은 벡터도 가까워진다. 이 변환은 별도의 임베딩 모델이 수행한다.

### 0.2 Oracle 단독 방식이 불가능한 이유 — 3가지 제약

사내 표준 저장소는 **Oracle 19c**이며, 초기에는 저장과 검색을 Oracle 하나로 처리하고자 했다. 그러나 다음 세 가지 제약에 막혔다.

| # | 제약 | 설명 |
|---|------|------|
| ① | **Oracle 19c는 임베딩(벡터) 검색을 지원하지 않는다** | 해당 버전에는 벡터 검색 기능이 없다. |
| ② | **한국어 형태소 기능 활성화에 필요한 권한을 확보할 수 없다** | Oracle의 한국어 형태소 검색을 켜려면 관리자 권한(CTXAPP 등)이 필요하나, 이를 활성화하면 **동일 Oracle 인스턴스를 공유하는 타 프로젝트에 부작용(side effect)** 이 발생할 수 있어 확보가 불가하다. |
| ③ | **서비스 컨테이너에 파일을 남길 수 없다** | 보안 정책상 컨테이너 내부에 검색 색인 등 파일을 저장할 수 없다. |

> **형태소·토크나이저:** 한국어는 "환불을"처럼 어절이 결합되어 나타나므로, 검색을 위해 먼저 **"환불 / 을"** 과 같이 분해해야 한다. 이 분해 도구를 **토크나이저(형태소 분석기)** 라 하며, 본 설계에서는 `Kiwi`를 사용한다.

### 0.3 해결 방식

**원본(Oracle)은 그대로 유지하고, 검색 계층만 분리했다.**

- **원본 저장 = Oracle** — 데이터의 단일 진실 원천(SoT). 항상 Oracle이 기준이다.
- **검색 = 애플리케이션 메모리 내 SQLite 색인 2종**
  - 단어용 색인(FTS5) — 한국어는 사전에 `Kiwi`로 분해하여 적재
  - 의미용 색인(sqlite-vec) — 임베딩(벡터) 적재
- 두 색인의 결과를 **RRF** 로 융합하여 최종 순위를 산출한다 → **하이브리드**

이 방식으로 세 가지 제약이 모두 해소된다.

- ① 의미 검색을 sqlite-vec가 담당한다(Oracle이 제공하지 못하던 기능).
- ② 형태소 분해를 앞단의 Kiwi가 수행하므로 Oracle의 해당 권한이 **불필요**하다.
- ③ SQLite를 **메모리(`:memory:`)** 에만 생성하므로 **디스크 파일이 발생하지 않는다.** 서비스 재기동 시 Oracle에서 다시 구성한다.

> **비유:** Oracle은 모든 자료를 보관하는 **창고**, SQLite 인메모리 색인은 필요할 때 만드는 **색인 카드**에 해당한다. 카드는 파일로 남기지 않고 메모리에만 두며, 창고 내용이 변경되면 카드를 다시 만든다. 창고(원본)는 변경하지 않는다.

### 0.4 직접 구현 대신 SQLite를 사용하는 이유 — 유지보수성

- **기존 방식:** numpy로 전체 벡터를 메모리에 적재하여 **직접 계산**했다. 그러나 문서의 **추가·수정·삭제**마다 해당 구조를 수작업으로 관리해야 하여 **복잡도가 높고 오류·메모리 누수 위험**이 있었다.
- **현재 방식:** SQLite(FTS5·sqlite-vec)가 **색인과 메모리를 자체 관리**한다. 데이터 변경 시 **재로드**만으로 반영되며, 검증된 라이브러리가 관리하므로 코드가 단순하고 안정적이다.

### 0.5 Chroma 대신 SQLite를 선택한 이유

후보는 ChromaDB와 SQLite였으며, **SQLite로 결정했다.**

- **Chroma**는 의미 검색에는 강하나 **단어 검색(한국어 형태소)이 약하다.** 본 설계는 **단어·의미를 한 계층에서** 처리하는 것이 목표이므로 부적합하다.
- **Chroma는 별도 서비스**로 운영 부담이 있다. 반면 **SQLite는 애플리케이션에 임베디드되는 라이브러리**로 경량이며, "Oracle=저장 / 메모리=검색" 구조에 부합한다.

---

## 1. 배경 / 결정 이유 (요약)

| 문제 / 제약 | 기존 방식 | 채택 방식 |
|------|------|--------------|
| Oracle 19c에 임베딩(벡터) 검색 부재 | numpy 인메모리 브루트포스 직접 계산 | **sqlite-vec** 위임 (코사인 KNN) |
| 형태소용 Oracle 권한(CTXAPP) — 활성화 시 **타 프로젝트 부작용** 우려로 확보 불가 | Oracle Text/형태소기 의존 | 앞단 **Kiwi** 분해 + 렉시컬 **FTS5(BM25)** |
| 컨테이너 파일 잔존 금지(보안) | - | SQLite **`:memory:`** (디스크 파일 없음) |
| 수작업 벡터 관리 → 추가/수정/삭제 유지보수난 | `corpus_search`의 numpy 행렬 직접 구현 | 검증된 라이브러리가 색인·메모리 관리(재로드) |

> **설계원칙 재검토:** CLAUDE.md 결정 #6("별도 검색엔진 금지, Oracle 단일")은 *Oracle Text 사용 가능*을 전제로 한다. 해당 전제(권한)가 성립하지 않으므로 재검토가 정당하다. SQLite는 서비스형 검색엔진이 아니라 **애플리케이션 임베디드 라이브러리**로서 "Oracle=저장 / 인메모리=계산" 구조에 부합하며, Chroma 등 별도 서비스보다 원칙 충돌이 적다.

---

## 2. 아키텍처

```mermaid
flowchart TB
    src["원문(문서·청크)"] -->|적재 파이프라인| kiwi["Kiwi 형태소 토큰화(원형)"]

    subgraph ORA["Oracle 19c (원본·영속, 단일 진실 원천)"]
        cols["corpus_chunks<br/>text(원문) · text_tokenized(Kiwi결과) · embedding(원문벡터)"]
    end
    kiwi --> cols
    src -.원문 임베딩.-> cols

    subgraph MEM["애플리케이션 프로세스 내 SQLite :memory: (파일 없음, 재기동 시 재생성)"]
        fts["FTS5(text_tokenized, unicode61)<br/>단어 검색 · BM25"]
        vec["vec0(embedding float[dim])<br/>의미 검색 · 코사인 KNN"]
    end
    ORA -->|기동/리로드 시 전량 로드| MEM

    q["사용자 질의"] --> qk["Kiwi 토큰화"] --> fts
    q --> qe["임베딩(벡터화)"] --> vec
    fts -->|상위 N| rrf["RRF 융합(순위 결합)"]
    vec -->|상위 N| rrf
    rrf --> out["최종 상위 K"]
```

**핵심 분리 원칙:** 단어 검색용은 **Kiwi로 분해한 텍스트**, 의미 검색용은 **원문 임베딩**. 둘을 혼용하지 않는다.

---

## 3. Oracle 스키마 (원본 유지, 컬럼만 추가)

검색을 위해 Oracle에 **컬럼 두 개만** 추가한다. 새 테이블·추가 권한·Oracle Text 인덱스는 **모두 불필요**하며, 단순 문자열/숫자 저장에 해당한다.

```sql
-- corpus_chunks (문서를 분할한 검색 단위)
ALTER TABLE corpus_chunks ADD (text_tokenized CLOB);   -- Kiwi 원형 토큰(공백 조인)
-- embedding 컬럼은 기존 유지 (원문 임베딩 벡터)
```

- `text_tokenized` 예: 원문 `"환불 규정을 알려줘"` → `"환불 규정 을 알리 어 주"` (원형 기준, §6 참고)
- **사전 분해 저장 이유:** FTS5는 한국어 형태소를 인식하지 못한다. 따라서 **적재 시 Kiwi로 분해**하여 저장하고, 검색 시에도 **동일 Kiwi로 질의를 분해**하여 일치시킨다.

---

## 4. 데이터 흐름

### (a) 적재 — 문서 저장 시 (야간 배치 / 청킹 시점)
```python
from search import ko_tokenize   # 적재·질의 공용 단일 소스

def tokenize_for_search(text: str) -> str:
    # Kiwi 원형(lemma) 기준 공백 조인. 적재와 질의가 동일 함수를 사용해야 결과가 일치한다.
    return ko_tokenize.tokenize_for_search(text)

# corpus_chunks 저장 시:
#   text            = 원문 (사람이 읽는 형태)
#   text_tokenized  = tokenize_for_search(원문)   ← 단어 검색(FTS5)용
#   embedding       = embed(원문)                 ← 의미 검색(벡터)용
```

### (b) 로드 — 앱 기동 시 및 원본 변경 시 (Oracle → 메모리)
```python
import sqlite3, sqlite_vec

def build_index(rows, dim):
    con = sqlite3.connect(":memory:")           # 파일 없음 — 메모리에만 구성
    con.enable_load_extension(True); sqlite_vec.load(con)
    con.execute(f"CREATE VIRTUAL TABLE vec USING vec0(cid INTEGER PRIMARY KEY, embedding float[{dim}])")
    con.execute("CREATE VIRTUAL TABLE fts USING fts5(cid UNINDEXED, body, tokenize='unicode61')")
    for r in rows:   # r = (chunk_id, text_tokenized, embedding_floats)
        con.execute("INSERT INTO vec(cid, embedding) VALUES (?,?)",
                    (r.chunk_id, sqlite_vec.serialize_float32(r.embedding)))
        con.execute("INSERT INTO fts(cid, body) VALUES (?,?)",
                    (r.chunk_id, r.text_tokenized))
    return con
```

### (c) 질의 — 검색 시 (하이브리드)
```python
def search(con, query, embed_fn, k=10, rk=60):
    q_tok = tokenize_for_search(query)                     # 적재와 동일하게 Kiwi 분해
    q_vec = sqlite_vec.serialize_float32(embed_fn(query))  # 질의를 벡터로 변환

    lex = con.execute("SELECT cid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT 100",
                      (q_tok,)).fetchall()                 # 단어 검색 상위 100
    vec = con.execute("SELECT cid FROM vec WHERE embedding MATCH ? ORDER BY distance LIMIT 100",
                      (q_vec,)).fetchall()                 # 의미 검색 상위 100

    # RRF 융합 — 점수 크기가 아닌 '순위'로 결합하여 두 검색을 공정하게 통합
    score = {}
    for rank, (cid,) in enumerate(lex, 1): score[cid] = score.get(cid, 0) + 1/(rk+rank)
    for rank, (cid,) in enumerate(vec, 1): score[cid] = score.get(cid, 0) + 1/(rk+rank)
    return sorted(score, key=score.get, reverse=True)[:k]
```

> **RRF(Reciprocal Rank Fusion):** 두 검색이 매긴 **순위**만으로 결합하는 방식. 점수 스케일이 서로 달라도 공정하게 통합되며, 두 검색 모두 상위에 올린 문서일수록 높은 점수를 받는다.

---

## 5. 동기화 — 원본(Oracle)과 복사본(메모리) 정합

메모리 색인은 원본에서 파생된 복사본이므로, 원본 변경 시 갱신한다. 현재 규모에서는 **버전 확인 후 전체 재로드**가 가장 단순하고 안전하다.

- Oracle에 인덱스 버전 값을 1개 두고, 각 복제본이 주기적으로 확인하여 변경 시 전체 재빌드
- 복제본마다 독립된 메모리 사본을 보유하므로 각자 재로드
- **쓰기는 항상 Oracle 우선**(원본이 기준), 메모리는 재로드로만 반영
- 증분 동기화는 본 PoC 범위 밖(현재는 전체 재로드로 충분)

---

## 6. 준수 조건 (위반 시 검색 정합성 저하)

- 단어 색인(FTS5)과 임베딩(벡터)은 **분리** — 혼용 금지
- **적재 Kiwi = 질의 Kiwi** (동일 버전·동일 함수) — 불일치 시 분해 결과가 달라 매칭 실패
- FTS5 토크나이저는 `unicode61` 사용
- 표면형/원형 통일(**원형 권장**)

---

## 7. 도구 선택 근거

- **형태소 분석기 = Kiwi(`kiwipiepy`)**: `pip install` 한 줄로 설치, **Java·외부사전 불필요**(KoNLPy/Okt는 Java 필요, mecab-ko는 C 사전 설치 부담). 정확도·속도가 우수하며 컨테이너 친화적이다.
- **단어 검색 = SQLite FTS5**: BM25(관련도 점수) 내장. Oracle Text/CTXAPP 권한을 대체한다.
- **의미 검색 = sqlite-vec**: `:memory:`에서 코사인 KNN 수행(현 규모에서는 전수 비교로 충분). Chroma는 단어 검색이 약해 "단일 계층에서 단어+의미 처리" 목표에 부적합하다.
- **저장소 = Oracle 유지(원본·기준)**: 영속성·트랜잭션·기존 파이프라인 재사용. 검색 계층만 메모리로 분리한다.

---

## 8. 조건 / 열린 질문

- 임베딩 모델 미서빙 시 **단어 검색(FTS5) 단독 폴백** 동작(검색 중단 방지)
- 데이터가 **RAM 수용 범위** 이내여야 함(파드 메모리·로드 시간 모니터링)
- 검색 진입점(`search_docs`/`read_doc`) 인터페이스 유지 → 에이전트·앱 상위 코드 무변경
- 청크 best-hit → 문서 단위 집계 로직 유지
- 열린 질문: `nodes.embedding` dedup의 동일 벡터 경로 재사용 여부(별도 검토)

---

## 9. 구현 위치

| 역할 | 파일 |
|------|------|
| 한국어 토큰화(적재·질의 공용) | `src/search/ko_tokenize.py` (`tokenize_for_search`) |
| 인메모리 인덱스 빌드·검색·리로드 | `src/search/inmemory_index.py` (`build_index`/`lexical`/`semantic`/`ensure_fresh`) |
| 검색 진입점(하이브리드·RRF·문서 집계) | `src/search/corpus_search.py` |
| 청킹(원문→청크, Kiwi 토큰 저장) | `src/ingestion/chunk_corpus.py` |
| 임베딩 백필(청크→벡터) | `src/ingestion/embed_corpus.py` |

> 요약: **Oracle이 원본(기준), SQLite 인메모리가 검색을 담당한다.** 파일 미잔존(보안), Oracle 추가 권한 불요(부작용 회피), 단어·의미 단일 계층 처리(하이브리드), 라이브러리 기반 관리(추가·수정·삭제 용이) — 이 네 가지가 본 설계의 근거다.
