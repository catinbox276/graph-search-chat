"""지식그래프 뷰 데이터 — 노드·엣지·출처 증거 (provenance)."""
from fastapi import APIRouter, Request

from web import auth
from web.deps import db

router = APIRouter()


@router.get("/graph/data")
def graph_data(request: Request):
    auth.require_user(request)
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT n.id, n.layer, n.name, n.fail_reason,
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
                WHERE ev.node_id = n.id AND ev.kind = 'doc') AS dc
        FROM nodes n""")
    nodes = [{"id": r[0], "layer": r[1], "name": r[2], "fail_reason": r[3],
              "uses": r[4], "success": r[5], "fail_cnt": r[6], "docs": r[7],
              "fail": r[6] > r[5]}  # 실패 우세만 빨강 (카운트 기준)
             for r in cur.fetchall()]
    cur.execute("SELECT src, dst, raw_count FROM edges")
    edges = [{"src": r[0], "dst": r[1], "count": r[2]} for r in cur.fetchall()]
    con.close()
    return {"nodes": nodes, "edges": edges}


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

