"""적재 문서 → 지식그래프 구조화 (docs/plan.md §3 ②③의 'LLM 그래프 구조화').

소스 관리에서 **도메인이 지정된** 소스의 corpus_docs 문서를, LLM이 그 도메인의
정의·추출 지침(domain_registry.extract_hint)을 기준으로 판정·구조화한다:

- 기준에 맞으면(fits=true): 목표·접근법을 추출해 대화와 같은 그래프에 병합
  (graph_pipeline.get_or_create 재사용 — 2단계 임베딩→LLM dedup 동일 적용)
- 기준 미달이면(fits=false): graph_status='excluded' — 그래프에 안 들어간다
  (도메인 무관 / 결말 없는 글 / 내용 빈약 — plan.md §3의 '미달 제외' 정책)
- 증거는 node_evidence에 "doc:소스명:원천id"로 남는다. sessions와 조인되지 않으므로
  성공/실패 판정 카운트에는 안 섞이고, 통행(raw_count)·출처 추적에만 기여한다.

멱등·이어하기: graph_status IS NULL 인 문서만 처리. 실행당 DOC_EXTRACT_LIMIT건
(기본 200) — 야간 반복(03:40 CronJob)으로 점진 소진한다.

usage: .venv/bin/python -m poc.doc_pipeline [--limit N]
"""
import argparse
import sys
from pathlib import Path

import oracledb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import config  # noqa: E402
from tools.blog_search import DSN, PASSWORD, USER  # noqa: E402
from poc.graph_pipeline import _llm_json, ddl, get_or_create  # noqa: E402

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


def doc_ddl(cur):
    """corpus_docs에 구조화 상태 컬럼 + node_evidence 참조 폭 확장 (멱등)."""
    for col, spec in (("GRAPH_STATUS", "graph_status VARCHAR2(20)"),
                      ("GRAPH_NOTE", "graph_note VARCHAR2(1000)")):
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name = 'CORPUS_DOCS' AND column_name = :1""", [col])
        if not cur.fetchone()[0]:
            cur.execute(f"ALTER TABLE corpus_docs ADD ({spec})")
    # 문서 증거 참조("doc:소스:id")가 세션 uuid(36자)보다 길다 → 폭 확장
    cur.execute("""SELECT data_length FROM user_tab_columns
                   WHERE table_name = 'NODE_EVIDENCE' AND column_name = 'SESSION_ID'""")
    r = cur.fetchone()
    if r and r[0] < 400:
        cur.execute("ALTER TABLE node_evidence MODIFY (session_id VARCHAR2(400))")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=config.DOC_EXTRACT_LIMIT,
                    help="이번 실행에서 처리할 최대 문서 수")
    args = ap.parse_args()

    con = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    cur = con.cursor()
    ddl(cur)      # nodes/edges/node_evidence/domain_registry 보장
    doc_ddl(cur)
    con.commit()

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

    budget = args.limit
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
        print(f"[{source_name}] 도메인 '{domain}' 기준 {len(docs)}건 구조화 시작", flush=True)
        for src_id, title, kind, body in docs:
            budget -= 1
            ref = f"doc:{source_name}:{src_id}"[:400]
            j = _llm_json(DOC_PROMPT.format(
                domain=domain, hint=hint.strip() or "(지침 없음 — 도메인명 기준으로 판정)",
                kind=kind.strip(), title=title.strip()[:300], body=(body or "")[:3000]))
            if not j:
                status, note = "error", "LLM 응답 파싱 실패"
            elif j.get("fits") and j.get("goal") and j.get("approach"):
                d = get_or_create(cur, 1, domain, None, ref, use_embedding=False)
                g = get_or_create(cur, 2, str(j["goal"])[:400], d, ref)
                get_or_create(cur, 3, str(j["approach"])[:400], g, ref)
                status, note = "done", str(j.get("reason") or "")[:1000]
            else:
                status, note = "excluded", str(j.get("reason") or "기준 미달")[:1000]
            cur.execute("""UPDATE corpus_docs SET graph_status = :1, graph_note = :2
                           WHERE source_name = :3 AND src_id = :4""",
                        [status, note or None, source_name, src_id])
            con.commit()
            stats[status] += 1
            mark = {"done": "+", "excluded": "-", "error": "!"}[status]
            print(f"  {mark} {src_id}: {status}"
                  f"{' — ' + note if status != 'done' and note else ''}", flush=True)

    cur.execute("""SELECT NVL(graph_status, '미처리'), COUNT(*) FROM corpus_docs
                   GROUP BY graph_status""")
    print(f"\n이번 실행: {stats} / 전체 현황: {dict(cur.fetchall())}")
    con.close()


if __name__ == "__main__":
    main()
