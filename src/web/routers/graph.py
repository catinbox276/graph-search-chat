"""지식그래프 뷰 데이터 — 노드·엣지·출처 증거 (provenance)."""
from fastapi import APIRouter, Request

from core import config
from web import auth
from web.deps import db

router = APIRouter()


@router.get("/graph/search")
def graph_search(q: str, request: Request, n: int = 40):
    """그래프 뷰 노드 검색 — 문서 검색과 같은 하이브리드(Kiwi FTS5 BM25 + 임베딩
    코사인, RRF 융합). 임베딩 미서빙 시 렉시컬 단독 폴백. 반환: 노드 id 순위 목록."""
    auth.require_user(request)
    from search import inmemory_index as ix
    ix.ensure_fresh()
    n = max(1, min(n, 500))
    sem = ix.node_semantic(q, n, config.PATH_SIM_ENTRY)
    lex = ix.node_lexical(q, n)
    rrf = {}
    for ids in (sem, lex):
        for r, nid in enumerate(ids):
            rrf[nid] = rrf.get(nid, 0.0) + 1.0 / (config.RRF_K + r + 1)
    return {"ids": sorted(rrf, key=rrf.get, reverse=True)}


@router.get("/graph/data")
def graph_data(request: Request, relations_run: str = ""):
    """relations_run: 특정 run의 관계까지 미리보기(관리자 검토용) — 기본은 활성 run만."""
    auth.require_user(request)
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT n.id, n.layer, n.name, n.fail_reason, n.entity_type,
               (SELECT COUNT(*) FROM node_evidence ev
                WHERE ev.node_id = n.id) AS ev_cnt,
               (SELECT COUNT(*) FROM node_evidence ev
                JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                WHERE ev.node_id = n.id AND ev.kind = 'session'
                  AND s.verdict = 'success') AS sc,
               (SELECT COUNT(*) FROM node_evidence ev
                JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                WHERE ev.node_id = n.id AND ev.kind = 'session'
                  AND s.verdict = 'fail') AS fc,
               (SELECT COUNT(*) FROM node_evidence ev
                WHERE ev.node_id = n.id AND ev.kind = 'doc'
                  AND ev.run_id IN (SELECT run_id FROM doc_runs WHERE active = 'Y')) AS dc
        FROM nodes n""")
    nodes = [{"id": r[0], "layer": r[1], "name": r[2], "fail_reason": r[3],
              "etype": r[4] or "",
              "uses": r[5], "success": r[6], "fail_cnt": r[7], "docs": r[8],
              "fail": r[7] > r[6]}  # 실패 우세만 빨강 (카운트 기준)
             for r in cur.fetchall()]
    cur.execute("SELECT src, dst, raw_count FROM edges")
    edges = [{"src": r[0], "dst": r[1], "count": r[2]} for r in cur.fetchall()]
    # 타입드 관계 (존재 기반) — 활성 run 스코핑으로만 노출
    relations = []
    try:
        cur.execute("""SELECT er.src, er.dst, er.rtype, COUNT(DISTINCT er.ref)
                       FROM entity_relations er
                       WHERE er.run_id = '-'
                          OR er.run_id IN (SELECT run_id FROM doc_runs WHERE active = 'Y')
                          OR er.run_id = :rr
                       GROUP BY er.src, er.dst, er.rtype""",
                    {"rr": relations_run or "-"})
        relations = [{"src": r[0], "dst": r[1], "rtype": r[2], "count": r[3]}
                     for r in cur.fetchall()]
    except Exception:
        pass   # 테이블 미생성(구버전 DB) — 관계는 부가 정보
    con.close()
    return {"nodes": nodes, "edges": edges, "relations": relations}


@router.get("/graph/node/{nid}/evidence")
def graph_node_evidence(nid: str, request: Request):
    """노드의 출처 증거 — 어느 세션/문서에서 왔는지 (provenance 가시화)."""
    auth.require_user(request)
    con = db()
    cur = con.cursor()
    cur.execute("""SELECT ev.kind, ev.ref, s.verdict
                   FROM node_evidence ev
                   LEFT JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                   WHERE ev.node_id = :1
                   ORDER BY ev.kind, ev.ref
                   FETCH FIRST 30 ROWS ONLY""", [nid])
    rows = cur.fetchall()
    out = []
    for kind, ref, verdict in rows:
        item = {"kind": kind, "ref": ref, "verdict": verdict}
        if kind == "doc":  # 문서 증거는 제목까지 (ref = 소스명:원천id)
            src, _, sid_ = ref.partition(":")
            cur.execute("""SELECT title FROM corpus_docs
                           WHERE source_name = :1 AND src_id = :2""", [src, sid_])
            r = cur.fetchone()
            item["title"] = r[0] if r else None
        out.append(item)
    cur.execute("SELECT COUNT(*) FROM node_evidence WHERE node_id = :1", [nid])
    total = cur.fetchone()[0]
    con.close()
    return {"evidence": out, "total": total}

