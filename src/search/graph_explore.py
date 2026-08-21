"""엔티티 주변 탐색 도구 — 속성값(PostgreSQL·분류 등)에서 실제 해결 사례로 잇는다.

"저장은 단순하게, 탐색은 똑똑하게" (docs/references 2026-08-20 메모리-탐색-아키텍처):
에이전트에 자유 그래프 질의가 아니라 제한된 탐색 API 하나를 준다.

타입드 관계(entity_relations)는 2026-08-21에 제거했다 — 문서당 속성이 1개씩이라
"접근법 —uses→ PostgreSQL" 같은 관계가 동시 출현과 같은 정보였다(측정: library/task
모두 문서당 1개). 대신 **계층 엣지**를 쓴다: 속성 노드(9층)는 진입점(2층) 바로 아래
붙어 있어서, 속성에서 위로 한 홉이면 그 속성이 등장한 목표·접근법이 나온다.
그게 사용자가 실제로 원하는 답("PostgreSQL 관련 노하우")이다.
"""
from search import path_suggest as _ps  # 같은 도메인 풀 재사용 (4번째 풀 금지)

# 활성 run 스코핑 — 그래프 뷰(web/routers/graph.py)와 같은 규칙
_ACTIVE_EV = ("(ev.run_id = '-' OR ev.run_id IN "
              "(SELECT run_id FROM doc_runs WHERE active = 'Y'))")


def _kind(layer, role_tag, etype):
    """노드 종류 라벨 — 타입 라벨 > 역할 태그 > 층 폴백 (v2 체인: 2..7층·속성 9층)."""
    if etype:
        return etype
    if role_tag == "entry":
        return "목표"
    if role_tag == "solution":
        return "접근법"
    return "속성" if layer == 9 else "중간단계"


def _fmt_node(name, layer, etype, role_tag=None):
    kind = _kind(layer, role_tag, etype)
    return f"{name}({kind})" if kind else name


def _evidence(cur, nid, limit=3):
    """이 노드의 근거 문서 id — read_doc 열람·답변 인용 가능."""
    cur.execute(f"""SELECT DISTINCT ref FROM node_evidence ev
                    WHERE ev.node_id = :1 AND ev.kind = 'doc' AND {_ACTIVE_EV}
                    FETCH FIRST {int(limit)} ROWS ONLY""", [nid])
    return " ".join(f"[{r[0]}]" for r in cur.fetchall())


def explore_entity(name: str) -> str:
    """기술·제품·도구·작업 이름이 등장한 과거 사례를 지식그래프에서 조회한다.
    "X 관련 노하우", "X는 어떤 작업에 쓰였나", "X 쓰는 사례"류 질문이나,
    검색 결과에 나온 핵심 엔티티의 주변을 넓힐 때 호출할 것.

    Args:
        name: 조회할 이름 (예: PostgreSQL, 분류, 이미지 분류 정확도)
    """
    q = (name or "").strip()
    if not q:
        return "조회할 이름이 비어 있습니다."
    with _ps._pool.acquire() as con:
        cur = con.cursor()
        # 1) 이름 매칭 — 정확 일치 우선, 없으면 부분 일치 (엔티티명은 고유명사라 렉시컬로 충분)
        for sql in ("""SELECT id, name, layer, entity_type, role_tag FROM nodes
                       WHERE UPPER(name) = UPPER(:1)
                         AND (layer BETWEEN 2 AND 7 OR layer = 9)
                       FETCH FIRST 3 ROWS ONLY""",
                    """SELECT id, name, layer, entity_type, role_tag FROM nodes
                       WHERE UPPER(name) LIKE '%' || UPPER(:1) || '%'
                         AND (layer BETWEEN 2 AND 7 OR layer = 9)
                       ORDER BY LENGTH(name) FETCH FIRST 3 ROWS ONLY"""):
            cur.execute(sql, [q])
            hits = cur.fetchall()
            if hits:
                break
        if not hits:
            return (f"'{q}'와 일치하는 노드가 지식그래프에 없습니다. "
                    "search_docs로 문서를 직접 검색하세요.")
        out = [f"🕸 '{q}' 주변 탐색 결과:"]
        for nid, nname, layer, etype, rtag in hits:
            out.append(f"\n[{_kind(layer, rtag, etype)}] {nname}")
            if layer == 9:
                # 속성 → 위로 한 홉: 이 값이 등장한 목표(진입점)와 그 아래 접근법
                cur.execute("""SELECT p.id, p.name, e.raw_count FROM edges e
                               JOIN nodes p ON p.id = e.src
                               WHERE e.dst = :1 ORDER BY e.raw_count DESC
                               FETCH FIRST 5 ROWS ONLY""", [nid])
                parents = cur.fetchall()
                if not parents:
                    out.append("  (이 값이 붙은 사례가 없습니다)")
                for pid, pname, _c in parents:
                    out.append(f"  · 목표: {pname}")
                    cur.execute("""SELECT n.name FROM edges e2 JOIN nodes n ON n.id = e2.dst
                                   WHERE e2.src = :1 AND n.layer BETWEEN 3 AND 7
                                   ORDER BY e2.weight DESC FETCH FIRST 3 ROWS ONLY""", [pid])
                    for (aname,) in cur.fetchall():
                        out.append(f"     → 접근법: {aname}")
                    refs = _evidence(cur, pid, 2)
                    if refs:
                        out.append(f"     근거 문서: {refs}")
                # 같은 목표에 함께 붙은 다른 속성 = "무엇과 함께 쓰였나"
                cur.execute("""SELECT n3.entity_type, n3.name, COUNT(*) c FROM edges e1
                               JOIN edges e2 ON e2.src = e1.src
                               JOIN nodes n3 ON n3.id = e2.dst AND n3.layer = 9
                               WHERE e1.dst = :1 AND e2.dst <> :1
                               GROUP BY n3.entity_type, n3.name
                               ORDER BY c DESC FETCH FIRST 5 ROWS ONLY""", [nid])
                together = [f"{r[1]}({r[0]}, {r[2]}건)" for r in cur.fetchall()]
                if together:
                    out.append("  함께 등장: " + " · ".join(together))
            else:
                # 목표·중간단계 노드 → 아래 체인(접근법)과 붙은 속성
                cur.execute("""SELECT n.name, n.layer, n.entity_type, n.role_tag, e.weight
                               FROM edges e JOIN nodes n ON n.id = e.dst
                               WHERE e.src = :1 ORDER BY e.weight DESC
                               FETCH FIRST 8 ROWS ONLY""", [nid])
                rows = cur.fetchall()
                if not rows:
                    out.append("  (연결된 하위 노드 없음)")
                for cname, clayer, cetype, crtag, _w in rows:
                    out.append(f"  · {_fmt_node(cname, clayer, cetype, crtag)}")
            refs = _evidence(cur, nid)
            if refs:
                out.append(f"  근거 문서 (read_doc로 열람 가능): {refs}")
    return "\n".join(out)


if __name__ == "__main__":
    print(explore_entity("PostgreSQL"))
