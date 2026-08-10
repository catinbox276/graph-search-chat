"""내 기여 — 내 세션이 만든 지식의 확인·수정·철회 (사용자 제어=증폭기)."""
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import auth
from app.deps import db
from tools import model_registry

router = APIRouter()


@router.get("/me/contributions")
def my_contributions(request: Request):
    """내 세션이 그래프에 만든 지식(2·3층 노드) — 사용자가 확인·수정·철회하는 재료.

    editable(표현 수정 가능) = 증거가 내 것 하나뿐인 노드 — 여럿이 기여한 공유
    노드의 문구를 한 사람이 바꾸면 남의 기여까지 바뀌므로 단독 기여만 허용."""
    u = auth.require_user(request)
    uid = u.get("user")
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT ev.node_id, n.layer, n.name, NVL(n.fail_flag,'N'), n.fail_reason,
               ev.ref, s.verdict, TO_CHAR(s.ts,'YYYY-MM-DD HH24:MI'), s.question,
               (SELECT COUNT(*) FROM suggestions g WHERE g.node_id = n.id) AS exposures,
               (SELECT COUNT(*) FROM node_evidence e2 WHERE e2.node_id = n.id) AS ev_total,
               (SELECT MAX(p.name) FROM edges e JOIN nodes p
                 ON p.id = e.src AND p.layer = 2
                 WHERE e.dst = n.id) AS parent_goal,
               (SELECT COUNT(*) FROM edges e4 JOIN nodes t4
                 ON t4.id = e4.dst AND t4.layer = 4
                 WHERE e4.src = n.id) AS tool_cnt
        FROM node_evidence ev
        JOIN nodes n ON n.id = ev.node_id AND n.layer IN (2, 3)
        JOIN sessions s ON s.id = ev.ref AND s.turn = 1
        WHERE ev.kind = 'session' AND s.user_id = :u
        ORDER BY s.ts DESC, n.layer
        FETCH FIRST 200 ROWS ONLY""", {"u": uid})
    items = []
    for r in cur.fetchall():
        q_ = r[8].read() if hasattr(r[8], "read") else (r[8] or "")
        items.append({"node_id": r[0], "layer": r[1], "name": r[2],
                      "fail": r[3] == "Y", "fail_reason": r[4],
                      "session_id": r[5], "verdict": r[6], "ts": r[7],
                      "question": q_[:200], "exposures": r[9],
                      "editable": r[10] == 1,
                      "parent_goal": r[11], "tool_cnt": r[12]})
    con.close()
    return {"items": items}


class ContribActIn(BaseModel):
    node_id: str
    action: str          # rename | retract | clear_fail
    name: str | None = None


@router.post("/me/contributions/act")
def contribution_act(inp: ContribActIn, request: Request):
    """내 기여 제어 — 사용자 제어=증폭기 원칙의 실행 지점.

    rename: 단독 기여 노드만 문구 교정 (+임베딩 재계산)
    retract: 이 노드에 대한 내 세션 증거 회수 (카운트는 조인으로 자동 감소,
             증거 0이 된 노드는 야간 유지보수가 흡수)
    clear_fail: 실패 표식 해제 — 기여자만 가능"""
    u = auth.require_user(request)
    uid = u.get("user")
    con = db()
    try:
        cur = con.cursor()
        # 소유 확인: 이 노드에 내 세션 증거가 있어야 함
        cur.execute("""SELECT COUNT(*) FROM node_evidence ev
                       JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                       WHERE ev.node_id = :n AND ev.kind = 'session'
                         AND s.user_id = :u""", {"n": inp.node_id, "u": uid})
        mine = cur.fetchone()[0]
        if not mine:
            raise HTTPException(403, "이 노드에 대한 본인 기여가 없습니다")
        if inp.action == "rename":
            name = (inp.name or "").strip()
            if not (2 <= len(name) <= 400):
                raise HTTPException(400, "문구는 2~400자여야 합니다")
            cur.execute("SELECT COUNT(*) FROM node_evidence WHERE node_id = :1",
                        [inp.node_id])
            if cur.fetchone()[0] != 1:
                raise HTTPException(409, "여럿이 기여한 노드는 문구를 바꿀 수 없습니다")
            emb = None
            try:
                cli, emb_name = model_registry.embedding_client()
                v = cli.embeddings.create(model=emb_name, input=name).data[0].embedding
                emb = json.dumps(v).encode()
            except Exception:
                pass  # 임베딩 실패해도 문구는 교정 — 벡터는 다음 병합 때 재계산 여지
            cur.execute("UPDATE nodes SET name = :1, embedding = :2 WHERE id = :3",
                        [name, emb, inp.node_id])
        elif inp.action == "retract":
            cur.execute("""DELETE FROM node_evidence
                           WHERE node_id = :n AND kind = 'session'
                             AND ref IN (SELECT id FROM sessions
                                         WHERE turn = 1 AND user_id = :u)""",
                        {"n": inp.node_id, "u": uid})
        elif inp.action == "clear_fail":
            cur.execute("""UPDATE nodes SET fail_flag = 'N', fail_reason = NULL
                           WHERE id = :1""", [inp.node_id])
        else:
            raise HTTPException(400, f"알 수 없는 액션: {inp.action}")
        con.commit()
    finally:
        con.close()
    return {"ok": True}

