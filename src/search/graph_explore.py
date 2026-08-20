"""엔티티 관계망 탐색 도구 — 추출된 타입드 관계(entity_relations)를 답변 시점에 활용.

"저장은 단순하게, 탐색은 똑똑하게" (docs/references 2026-08-20 메모리-탐색-아키텍처):
에이전트에 자유 그래프 질의가 아니라 제한된 탐색 API 하나를 준다 —
이름으로 노드를 찾고, 활성 run 스코핑된 관계와 근거 문서 id를 돌려준다.
관계는 ①추출 스키마(rtypes)로 문서에서 추출된 것 — /graph 뷰의 보라 점선과 같은 데이터.
"""
from search import path_suggest as _ps  # 같은 도메인 풀 재사용 (4번째 풀 금지)

# 활성 run 스코핑 — 그래프 뷰(web/routers/graph.py)와 같은 규칙
_ACTIVE = "(er.run_id = '-' OR er.run_id IN (SELECT run_id FROM doc_runs WHERE active = 'Y'))"


def _kind(layer, role_tag, etype):
    """노드 종류 라벨 — 타입 라벨 > 역할 태그 > 층 폴백 (v2 체인: 2..7층·속성 9층)."""
    if etype:
        return etype
    if role_tag == "entry":
        return "목표"
    if role_tag == "solution":
        return "접근법"
    return "엔티티" if layer == 9 else "중간단계"


def _fmt_node(name, layer, etype, role_tag=None):
    kind = _kind(layer, role_tag, etype)
    return f"{name}({kind})" if kind else name


def explore_entity(name: str) -> str:
    """기술·제품·도구·개념 이름의 연관 관계망을 지식그래프에서 조회한다.
    "X는 무엇과 함께 쓰이나", "X 관련해 어떤 목표/작업이 있었나"류 질문이나,
    검색 결과에 나온 핵심 엔티티의 주변을 넓힐 때 호출할 것.

    Args:
        name: 조회할 엔티티/목표/접근법 이름 (예: Keras, 이미지 분류)
    """
    q = (name or "").strip()
    if not q:
        return "조회할 이름이 비어 있습니다."
    with _ps._pool.acquire() as con:
        cur = con.cursor()
        # 1) 이름 매칭 — 정확 일치 우선, 없으면 부분 일치 (렉시컬로 충분: 엔티티명은 고유명사)
        cur.execute("""SELECT id, name, layer, entity_type, role_tag FROM nodes
                       WHERE UPPER(name) = UPPER(:1)
                         AND (layer BETWEEN 2 AND 7 OR layer = 9)
                       FETCH FIRST 3 ROWS ONLY""", [q])
        hits = cur.fetchall()
        if not hits:
            cur.execute("""SELECT id, name, layer, entity_type, role_tag FROM nodes
                           WHERE UPPER(name) LIKE '%' || UPPER(:1) || '%'
                             AND (layer BETWEEN 2 AND 7 OR layer = 9)
                           ORDER BY LENGTH(name) FETCH FIRST 3 ROWS ONLY""", [q])
            hits = cur.fetchall()
        if not hits:
            return (f"'{q}'와 일치하는 노드가 지식그래프에 없습니다. "
                    "search_docs로 문서를 직접 검색하세요.")
        out = [f"🕸 '{q}' 관계망 탐색 결과:"]
        for nid, nname, layer, etype, rtag in hits:
            out.append(f"\n[{_kind(layer, rtag, None) if not etype else '엔티티'}]"
                       f" {_fmt_node(nname, layer, etype, rtag)}")
            # 2) 타입드 관계 — 나가는/들어오는 양방향, 활성 run 스코핑, 근거 문서 수 집계
            cur.execute(f"""SELECT er.rtype, n2.name, n2.layer, n2.entity_type, n2.role_tag,
                                   COUNT(DISTINCT er.ref)
                            FROM entity_relations er JOIN nodes n2 ON n2.id = er.dst
                            WHERE er.src = :1 AND {_ACTIVE}
                            GROUP BY er.rtype, n2.name, n2.layer, n2.entity_type,
                                     n2.role_tag""", [nid])
            rels = [f"  - {nname} —{r[0]}→ {_fmt_node(r[1], r[2], r[3], r[4])} (문서 {r[5]}건)"
                    for r in cur.fetchall()]
            cur.execute(f"""SELECT er.rtype, n2.name, n2.layer, n2.entity_type, n2.role_tag,
                                   COUNT(DISTINCT er.ref)
                            FROM entity_relations er JOIN nodes n2 ON n2.id = er.src
                            WHERE er.dst = :1 AND {_ACTIVE}
                            GROUP BY er.rtype, n2.name, n2.layer, n2.entity_type,
                                     n2.role_tag""", [nid])
            rels += [f"  - {_fmt_node(r[1], r[2], r[3], r[4])} —{r[0]}→ {nname} (문서 {r[5]}건)"
                     for r in cur.fetchall()]
            out += rels if rels else ["  (연결된 관계 없음)"]
            # 3) 이 노드의 근거 문서 id — read_doc 열람·답변 인용 가능
            cur.execute("""SELECT DISTINCT ref FROM node_evidence ev
                           WHERE ev.node_id = :1 AND ev.kind = 'doc'
                             AND (ev.run_id = '-' OR ev.run_id IN
                                  (SELECT run_id FROM doc_runs WHERE active = 'Y'))
                           FETCH FIRST 3 ROWS ONLY""", [nid])
            refs = " ".join(f"[{r[0]}]" for r in cur.fetchall())
            if refs:
                out.append(f"  근거 문서 (read_doc로 열람 가능): {refs}")
    return "\n".join(out)


if __name__ == "__main__":
    print(explore_entity("Keras"))
