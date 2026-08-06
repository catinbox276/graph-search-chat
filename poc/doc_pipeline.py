"""적재 문서 → 지식그래프 구조화 (docs/plan.md §3 ②③의 'LLM 그래프 구조화').

소스 관리에서 **도메인이 지정된** 소스의 corpus_docs 문서를, LLM이 그 도메인의
정의·추출 지침(domain_registry.extract_hint)을 기준으로 판정·구조화한다:

- 기준에 맞으면(fits=true): 목표·접근법을 추출해 대화와 같은 그래프에 병합
  (graph_pipeline.get_or_create 재사용 — 2단계 임베딩→LLM dedup 동일 적용)
- 기준 미달이면(fits=false): graph_status='excluded' — 그래프에 안 들어간다
  (도메인 무관 / 결말 없는 글 / 내용 빈약 — plan.md §3의 '미달 제외' 정책)
- 증거는 node_evidence(kind='doc', ref='소스명:원천id')로 남는다. 세션 증거와 분리되므로
  성공/실패 판정 카운트에는 안 섞이고, 통행(raw_count)·출처 추적에만 기여한다.

실행 구조: LLM 판정은 동시(스레드풀, 판정만 — DB 없음), 그래프 병합은 직렬
(커서 공유 안전). 멱등·이어하기: graph_status IS NULL만 처리, 문서마다 커밋.

운영 설정(app_settings — 관리 UI에서 재배포 없이 변경, 없으면 .env 기본값):
  doc_extract_limit  실행당 처리 문서 수 (기본 DOC_EXTRACT_LIMIT=200)
  doc_concurrency    LLM 판정 동시 요청 수 (기본 DOC_CONCURRENCY=16, UI에서 최대 256)
  doc_body_chars     판정에 넣는 본문 길이 (기본 DOC_BODY_CHARS=3000)
  doc_extract_model  전처리 전용 모델명 (빈값=대화 모델. CHAT_URL 호스트에서 서빙돼야 함)

usage: .venv/bin/python -m poc.doc_pipeline [--limit N]
"""
import argparse
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import oracledb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import config, settings  # noqa: E402
from tools.blog_search import DSN, PASSWORD, USER  # noqa: E402
from poc.graph_pipeline import CHAT_MODEL, ddl, get_or_create, llm  # noqa: E402

import json  # noqa: E402
import re  # noqa: E402

DOC_PROMPT = """문서가 도메인 기준에 맞는지 판정하고, 맞으면 지식을 추출하라. JSON만 출력.

도메인: {domain}
도메인 기준·추출 지침: {hint}

문서 (유형: {kind}):
제목: {title}
{body}

출력 형식: {{"fits": true|false, "reason": "판정 근거 한 문장",
 "goal": "문서가 다루는 문제/목표 (한 문장, fits=true일 때만)",
 "approach": "핵심 해법/접근법 (한 문장, fits=true일 때만)"}}

fits=false로 판정할 것:
- 도메인과 무관한 내용
- 문제도 해법도 없는 글, 결말·결론 없이 끝나는 글
- 내용이 너무 빈약해 지식으로 일반화할 수 없는 글"""


PACK_PROMPT = """여러 문서 각각이 도메인 기준에 맞는지 판정하고, 맞으면 지식을 추출하라. JSON 배열만 출력.

도메인: {domain}
도메인 기준·추출 지침: {hint}

문서 목록 — 각 문서는 ===[문서id]=== 로 시작한다:
{docs}

출력 형식: 문서마다 1개씩, 입력 순서대로 JSON 배열.
[{{"id": "문서id", "fits": true|false, "reason": "판정 근거 한 문장",
  "goal": "문제/목표 한 문장(fits=true일 때만)", "approach": "핵심 해법 한 문장(fits=true일 때만)"}}, ...]

fits=false로 판정할 것: 도메인과 무관 / 문제도 해법도 없음 / 결말·결론 없음 / 내용이 빈약함.
각 문서는 독립적으로 판정하라 — 다른 문서의 내용이 판정에 영향을 주면 안 된다."""

PACK_MAX_DOCS = 8  # 묶음당 문서 상한 — 출력 길이·판정 품질 보호


def _gen_kwargs(no_think: bool, max_tokens: int) -> dict:
    """생성 옵션 — no_think면 추론(생각) 출력을 끄고 출력 상한을 건다.

    출력(디코드)이 배치의 병목이라 생각 토큰 제거가 최대 지렛대 (A/B 실측 7~8배,
    판정 품질 동일). Qwen3 계열 chat_template_kwargs — 다른 서빙이면 무시될 수 있음.
    """
    if not no_think:
        return {}
    return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            "max_tokens": max_tokens}


def judge_pack(domain: str, hint: str, pack: list, model: str = "",
               body_chars: int = 3000, no_think: bool = True) -> list:
    """문서 묶음을 요청 1건으로 판정 — [(doc, verdict)] 반환.

    묶음 응답에서 누락·파싱 실패한 문서는 단건 판정으로 자동 폴백 (유실 없음).
    pack 원소: (src_id, title, kind, body). DB를 만지지 않아 스레드 병렬 안전.
    """
    if len(pack) == 1:
        d = pack[0]
        return [(d, judge_doc(domain, hint, d[2], d[1], d[3],
                              model=model, body_chars=body_chars,
                              no_think=no_think))]
    blocks = [f"===[{d[0]}]===\n제목: {(d[1] or '').strip()[:300]}\n{(d[3] or '')[:body_chars]}"
              for d in pack]
    prompt = PACK_PROMPT.format(
        domain=domain, hint=(hint or "").strip() or "(지침 없음 — 도메인명 기준으로 판정)",
        docs="\n\n".join(blocks))
    by_id = {}
    try:
        resp = llm.chat.completions.create(
            model=model or CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
            **_gen_kwargs(no_think, 1600))
        m = re.search(r"\[.*\]", resp.choices[0].message.content, re.S)
        for item in (json.loads(m.group()) if m else []):
            if isinstance(item, dict) and item.get("id") is not None:
                by_id[str(item["id"])] = item
    except Exception:
        pass  # 아래 단건 폴백이 받는다
    out = []
    for d in pack:
        j = by_id.get(str(d[0]))
        if j is None:  # 묶음 응답 누락 → 단건 재판정
            j = judge_doc(domain, hint, d[2], d[1], d[3],
                          model=model, body_chars=body_chars, no_think=no_think)
        out.append((d, j))
    return out


def judge_doc(domain: str, hint: str, kind: str, title: str, body: str,
              model: str = "", body_chars: int = 3000, no_think: bool = True) -> dict:
    """문서 1건 LLM 판정 — DB를 만지지 않아 스레드 병렬 안전. 서버 드라이런도 사용."""
    prompt = DOC_PROMPT.format(
        domain=domain, hint=(hint or "").strip() or "(지침 없음 — 도메인명 기준으로 판정)",
        kind=(kind or "").strip(), title=(title or "").strip()[:300],
        body=(body or "")[:body_chars])
    try:
        resp = llm.chat.completions.create(
            model=model or CHAT_MODEL, temperature=config.LLM_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
            **_gen_kwargs(no_think, 400))
        m = re.search(r"\{.*\}", resp.choices[0].message.content, re.S)
        return _loads_lenient(m.group()) if m else {}
    except Exception as e:  # 판정 1건 실패가 배치를 죽이지 않게
        return {"_error": str(e)[:300]}


def _loads_lenient(s: str) -> dict:
    """모델이 본문 인용 시 만드는 잘못된 이스케이프(\\한글 등) 복구 폴백."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s))


def doc_ddl(cur):
    """corpus_docs에 구조화 상태 컬럼 추가 (멱등)."""
    for col, spec in (("GRAPH_STATUS", "graph_status VARCHAR2(20)"),
                      ("GRAPH_NOTE", "graph_note VARCHAR2(1000)")):
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name = 'CORPUS_DOCS' AND column_name = :1""", [col])
        if not cur.fetchone()[0]:
            cur.execute(f"ALTER TABLE corpus_docs ADD ({spec})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="이번 실행 처리 문서 수 (0=설정값 doc_extract_limit)")
    args = ap.parse_args()

    con = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = con.cursor()
    ddl(cur)      # nodes/edges/node_evidence/domain_registry 보장
    doc_ddl(cur)
    st = settings.get_all(cur)
    con.commit()
    limit = args.limit or settings.get_int(st, "doc_extract_limit", config.DOC_EXTRACT_LIMIT)
    conc = max(1, settings.get_int(st, "doc_concurrency", config.DOC_CONCURRENCY))
    body_chars = settings.get_int(st, "doc_body_chars", config.DOC_BODY_CHARS)
    pack_tokens = settings.get_int(st, "doc_pack_tokens", config.DOC_PACK_TOKENS)
    no_think = bool(settings.get_int(st, "doc_no_think", config.DOC_NO_THINK))
    model = (st.get("doc_extract_model") or "").strip()
    print(f"설정: limit={limit} concurrency={conc} body_chars={body_chars} "
          f"pack_tokens={pack_tokens} no_think={no_think} "
          f"model={model or CHAT_MODEL}", flush=True)

    # 도메인이 지정된 소스만 대상 (미지정 = 검색 전용, 그래프화 안 함).
    # 대화 전용(scope=chat) 도메인은 제외 — 등록 API가 막지만 SQL 직접 수정 대비 2차 방어.
    cur.execute("""SELECT s.source_name, s.domain, NVL(d.extract_hint, ' ')
                   FROM source_registry s
                   JOIN domain_registry d ON d.name = s.domain
                   WHERE s.enabled = 'Y' AND s.domain IS NOT NULL
                     AND NVL(d.scope, 'both') != 'chat'""")
    sources = cur.fetchall()
    if not sources:
        print("그래프 구조화 대상 소스 없음 (소스 관리에서 도메인을 지정하면 대상이 됨)")
        return

    budget = limit
    stats = {"done": 0, "excluded": 0, "error": 0}
    for source_name, domain, hint in sources:
        if budget <= 0:
            break
        cur.execute("""SELECT src_id, NVL(title, ' '), NVL(kind, ' '), body
                       FROM corpus_docs
                       WHERE source_name = :1 AND graph_status IS NULL
                       ORDER BY src_id
                       FETCH FIRST :2 ROWS ONLY""", [source_name, budget])
        # CLOB은 fetch 직후 바로 읽는다 — SQL dbms_lob.substr는 한글에서 VARCHAR2
        # 4000바이트 한계로 ORA-06502가 나고, 로케이터를 커밋 뒤까지 들고 있지 않기 위해
        docs = [(r[0], r[1], r[2],
                 r[3].read() if hasattr(r[3], "read") else (r[3] or ""))
                for r in cur.fetchall()]
        if not docs:
            continue
        budget -= len(docs)
        # 묶음 구성: 입력 토큰 예산(문자 ≈ 토큰×2 근사)까지 문서를 묶는다.
        # 0이면 1건씩. 묶음당 상한 PACK_MAX_DOCS — 출력 길이·판정 품질 보호.
        if pack_tokens <= 0:
            packs = [[d] for d in docs]
        else:
            budget_chars = pack_tokens * 2
            packs, pk, chars = [], [], 0
            for d in docs:
                dlen = min(len(d[3] or ""), body_chars) + 400  # 제목·스캐폴드 여유
                if pk and (len(pk) >= PACK_MAX_DOCS or chars + dlen > budget_chars):
                    packs.append(pk)
                    pk, chars = [], 0
                pk.append(d)
                chars += dlen
            if pk:
                packs.append(pk)
        print(f"[{source_name}] 도메인 '{domain}' 기준 {len(docs)}건 구조화 시작 "
              f"(동시 {conc}, 묶음 {len(packs)}개)", flush=True)
        # 연속 파이프라인: 판정 요청(묶음)을 항상 conc건 서버에 걸어둔다 — 하나
        # 끝나면 즉시 다음 묶음 투입, 병합(직렬·메인 스레드)은 그 사이에 처리.
        # 청크 락스텝(최장 응답이 전체를 잡고, 병합 동안 요청 0건)을 피하는 구조.
        ex = ThreadPoolExecutor(max_workers=conc)
        it = iter(packs)
        pending = set()
        for p in packs[:conc]:
            next(it)
            pending.add(ex.submit(judge_pack, domain, hint, p, model, body_chars,
                                  no_think))
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                np_ = next(it, None)  # 병합 전에 먼저 다음 묶음 투입 — 파이프 안 끊김
                if np_ is not None:
                    pending.add(ex.submit(judge_pack, domain, hint, np_,
                                          model, body_chars, no_think))
                for (src_id, title, kind, body), j in fut.result():
                    ref = f"{source_name}:{src_id}"[:400]  # 문서 증거 (kind='doc')
                    if not j or j.get("_error"):
                        status, note = "error", (j.get("_error") if j
                                                 else "LLM 응답 파싱 실패")
                    elif j.get("fits") and j.get("goal") and j.get("approach"):
                        d = get_or_create(cur, 1, domain, None, "doc", ref,
                                          use_embedding=False)
                        g = get_or_create(cur, 2, str(j["goal"])[:400], d, "doc", ref)
                        get_or_create(cur, 3, str(j["approach"])[:400], g, "doc", ref)
                        status, note = "done", str(j.get("reason") or "")[:1000]
                    else:
                        status, note = "excluded", str(j.get("reason") or "기준 미달")[:1000]
                    cur.execute("""UPDATE corpus_docs SET graph_status = :1, graph_note = :2
                                   WHERE source_name = :3 AND src_id = :4""",
                                [status, (note or "")[:1000] or None, source_name, src_id])
                    con.commit()
                    stats[status] += 1
                    mark = {"done": "+", "excluded": "-", "error": "!"}[status]
                    print(f"  {mark} {src_id}: {status}"
                          f"{' — ' + note if status != 'done' and note else ''}", flush=True)
        ex.shutdown()

    cur.execute("""SELECT NVL(graph_status, '미처리'), COUNT(*) FROM corpus_docs
                   GROUP BY graph_status""")
    print(f"\n이번 실행: {stats} / 전체 현황: {dict(cur.fetchall())}")
    con.close()


if __name__ == "__main__":
    main()
