"""내 기여 — 내 세션이 만든 지식의 확인·수정·철회 (사용자 제어=증폭기)."""
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import auth
from app.deps import db
from tools import model_registry

router = APIRouter()


_SORTS = {  # 정렬 키 → 세션 목록 ORDER BY (화이트리스트 — 주입 방지)
    "recent": "s.ts DESC",
    "oldest": "s.ts ASC",
}


@router.get("/me/contributions")
def my_contributions(request: Request, q: str = "", verdict: str = "",
                     sort: str = "recent", page: int = 1, page_size: int = 10):
    """내 세션이 그래프에 만든 지식 — 세션 단위 페이지네이션 + 검색·필터·정렬.

    페이지 단위는 '세션'이다 (카드 = 세션이라 노드 단위로 자르면 카드가 쪼개짐).
    검색 q: 질문 또는 만들어진 노드 이름에 매칭. verdict: success/fail/retracted 필터.
    editable = 증거가 내 것 하나뿐인 노드만 (공유 노드는 한 사람이 못 바꿈)."""
    u = auth.require_user(request)
    uid = u.get("user")
    page = max(1, page)
    page_size = min(max(1, page_size), 50)
    order = _SORTS.get(sort, _SORTS["recent"])

    # 지식을 만든 내 세션의 필터 — 공통 WHERE (세션 목록·카운트 양쪽에 동일 적용).
    # 그래프 기여가 있는 turn=1 세션만, verdict/검색어(질문 또는 노드 이름) 조건.
    binds = {"u": uid}
    filt = ["""EXISTS (SELECT 1 FROM node_evidence ev
                       JOIN nodes n ON n.id = ev.node_id AND n.layer IN (2,3)
                       WHERE ev.kind = 'session' AND ev.ref = s.id)"""]
    if verdict in ("success", "fail", "retracted", "unknown"):
        filt.append("s.verdict = :vd")
        binds["vd"] = verdict
    if q.strip():
        filt.append("""(LOWER(DBMS_LOB.SUBSTR(s.question, 2000, 1)) LIKE :kw
                        OR EXISTS (SELECT 1 FROM node_evidence e3
                                   JOIN nodes n3 ON n3.id = e3.node_id AND n3.layer IN (2,3)
                                   WHERE e3.kind = 'session' AND e3.ref = s.id
                                     AND LOWER(n3.name) LIKE :kw))""")
        binds["kw"] = f"%{q.strip().lower()}%"
    wsql = "s.turn = 1 AND s.user_id = :u AND " + " AND ".join(filt)

    con = db()
    cur = con.cursor()
    # ① 세션 목록 (페이지네이션) — fail은 그 세션 노드에 기록된 판정 사유를 함께
    cur.execute(f"""SELECT s.id, s.verdict, TO_CHAR(s.ts,'YYYY-MM-DD HH24:MI'),
                           DBMS_LOB.SUBSTR(s.question, 200, 1),
                           (SELECT MAX(n.fail_reason) FROM node_evidence ev
                            JOIN nodes n ON n.id = ev.node_id AND n.fail_flag = 'Y'
                            WHERE ev.kind = 'session' AND ev.ref = s.id) AS reason
                    FROM sessions s WHERE {wsql}
                    ORDER BY {order}
                    OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY""",
                {**binds, "off": (page - 1) * page_size, "lim": page_size})
    _WHY = {  # 판정이 어떻게 나왔는지 사람 말로 (배지 옆 표시)
        "success": "해결 신호로 판정 (후퇴 신호 없이 마무리)",
        "retracted": f"성공했다가 {'같은 문제 재방문으로 취소'} — 같은 증상이 재발해 성공 판정 소급 취소",
        "unknown": "판정 보류 (신호가 엇갈려 그래프 미반영)",
    }
    sessions = [{"session_id": r[0], "verdict": r[1], "ts": r[2],
                 "question": (r[3] or "")[:200],
                 "why": (f"에이전트 답변의 실패 표지 — {r[4]}" if r[1] == "fail" and r[4]
                         else "에이전트가 접근 불가(찾지 못함 등)를 답변에 명시" if r[1] == "fail"
                         else _WHY.get(r[1], ""))}
                for r in cur.fetchall()]

    # 총 세션 수 (페이지 수 계산용) — 같은 필터
    cur.execute(f"SELECT COUNT(*) FROM sessions s WHERE {wsql}", binds)
    total = cur.fetchone()[0]

    # ② 이 페이지 세션들이 만든 노드 상세
    items = []
    if sessions:
        ids = [s["session_id"] for s in sessions]
        binds2 = {f"s{i}": v for i, v in enumerate(ids)}
        inlist = ", ".join(f":s{i}" for i in range(len(ids)))
        cur.execute(f"""
            SELECT ev.node_id, n.layer, n.name, NVL(n.fail_flag,'N'), n.fail_reason,
                   ev.ref,
                   (SELECT COUNT(*) FROM suggestions g WHERE g.node_id = n.id),
                   (SELECT COUNT(*) FROM node_evidence e2 WHERE e2.node_id = n.id),
                   (SELECT MAX(p.name) FROM edges e JOIN nodes p
                     ON p.id = e.src AND p.layer = 2 WHERE e.dst = n.id),
                   (SELECT COUNT(*) FROM edges e4 JOIN nodes t4
                     ON t4.id = e4.dst AND t4.layer = 4 WHERE e4.src = n.id)
            FROM node_evidence ev
            JOIN nodes n ON n.id = ev.node_id AND n.layer IN (2, 3)
            WHERE ev.kind = 'session' AND ev.ref IN ({inlist})
            ORDER BY n.layer""", binds2)
        for r in cur.fetchall():
            items.append({"node_id": r[0], "layer": r[1], "name": r[2],
                          "fail": r[3] == "Y", "fail_reason": r[4],
                          "session_id": r[5], "exposures": r[6],
                          "editable": r[7] == 1, "parent_goal": r[8],
                          "tool_cnt": r[9]})
    con.close()
    return {"sessions": sessions, "items": items, "total": total,
            "page": page, "pages": (total + page_size - 1) // page_size,
            "page_size": page_size}


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

