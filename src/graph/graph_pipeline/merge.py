"""클러스터(병합) — 노드 1개 삽입 시마다 같은 부모 밑 형제와 dedup 판정.

dedup 판정 3단 (design §5, 캘리브레이션: 같은 의도 0.81~0.98, 다른 의도 0.34~0.46 —
인접 주제 과병합이 0.7대에서 발생):
1) 코사인 ≥ SIM_HIGH(0.92) 이고 문자 가드 통과 → LLM 없이 즉시 병합
2) SIM_THRESHOLD(0.70)~ 후보들 → llm_select 1회로 같은 의도 하나 선택
3) 그 외 → 신규 노드
"""
import difflib
import json
import uuid

from core import config

from .llm import cosine, embed, llm_select

SIM_HIGH = config.DEDUP_SIM_HIGH       # 이 이상은 명백히 동일 — LLM 확인 생략하고 병합
SIM_THRESHOLD = config.DEDUP_SIM_THRESHOLD  # 후보 하한 — 이 구간은 LLM이 동일 의도 여부 확인

LAYER_KIND = {2: "목표(사용자가 이루려는 것)", 3: "접근법(문제를 푸는 방법)"}


def default_merge_cfg() -> dict:
    """클러스터(dedup) 기본 설정 — config(.env). run별 스냅샷의 폴백."""
    return {"sim_high": config.DEDUP_SIM_HIGH,
            "sim_threshold": config.DEDUP_SIM_THRESHOLD,
            "short_name_chars": config.DEDUP_SHORT_NAME_CHARS,
            "char_ratio": config.DEDUP_CHAR_RATIO,
            "select_max": config.DEDUP_SELECT_MAX,
            "select_prompt": "",   # ""=코드 기본 후보선택 프롬프트 (llm_select)
            "embed_model": ""}   # ""=기본 임베딩 (model_registry)


def _auto_merge_ok(a: str, b: str, mc: dict) -> bool:
    """임베딩 ≥HIGH 자동 병합 가드 — 짧은 이름 제외 + 문자 유사도 AND 조건.
    임베딩 코사인 단독 즉시 병합은 업계 관행에 없음 (Graphiti 3-gram Jaccard,
    Neo4j 편집거리 AND). 가드에 걸리면 병합을 버리는 게 아니라 LLM 판정으로 넘어간다."""
    if min(len(a), len(b)) < mc["short_name_chars"]:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= mc["char_ratio"]


def get_or_create(cur, layer, name, parent_id, ev_kind, ev_ref, use_embedding=True,
                  run_id="-", count=True, mc=None):
    """같은 부모 밑 형제와 2단계(임베딩→LLM) 비교 -> 병합 또는 신규. 엣지 raw_count 증가.

    ev_kind/ev_ref: 출처 증거 — 'session'+세션id 또는 'doc'+'소스명:원천id'.
    run_id: 문서 증거의 구조화 실행 귀속 (세션은 '-') — B-full 버저닝.
    count: False면 엣지 가중치를 올리지 않는다 (비활성 run 구조화 — 구조·증거만
           만들어 두고, 가중치는 활성 전환 시 증거 기반 델타로 가산)."""
    # 임베딩 엔드포인트가 죽어도 구조화는 계속 — 벡터 없으면(vec=None) dedup은 이름/LLM만
    # 사용(과병합만 줄고 진행은 됨). 무한 대기·전체 실패보다 낫다.
    mc = mc or default_merge_cfg()
    vec = None
    if use_embedding:
        try:
            vec = embed(name, mc.get("embed_model", ""))
        except Exception as e:
            print(f"[embed 실패→벡터없이 진행] {type(e).__name__}: {str(e)[:120]}", flush=True)
    node_id = None
    if parent_id:
        cur.execute("""SELECT n.id, n.name, n.embedding FROM nodes n
                       JOIN edges e ON e.dst = n.id
                       WHERE e.src = :1 AND n.layer = :2""", [parent_id, layer])
        cands = []
        for nid, nname, nemb in cur.fetchall():
            if nname == name:
                node_id = nid; break
            if vec is not None and nemb:
                # thick 모드는 같은 실행에서 방금 INSERT한 노드의 BLOB을 LOB 아닌
                # bytes로 돌려줄 수 있다 — 로케이터/bytes 둘 다 수용 (json.loads는 bytes OK).
                cand = json.loads(nemb.read() if hasattr(nemb, "read") else nemb)
                if len(cand) != len(vec):
                    continue  # 임베딩 모델 교체로 차원 다른 옛 벡터 → 비교 불가(잘못된 병합 방지)
                sim = cosine(vec, cand)
                if sim >= mc["sim_threshold"]:
                    cands.append((sim, nid, nname))
        if node_id is None and cands:
            cands.sort(reverse=True)
            top_sim, top_id, top_name = cands[0]
            if top_sim >= mc["sim_high"] and _auto_merge_ok(name, top_name, mc):
                node_id = top_id  # 고신뢰 + 문자 가드 통과 — LLM 없이 즉시 병합
            else:
                node_id = llm_select(LAYER_KIND.get(layer, "개념"), name, cands,
                                     mc["select_max"], prompt=mc.get("select_prompt", ""))
    else:
        cur.execute("SELECT id FROM nodes WHERE layer = :1 AND name = :2",
                    [layer, name])
        r = cur.fetchone()
        node_id = r[0] if r else None
    if node_id is None:
        node_id = uuid.uuid4().hex[:32]
        cur.execute(
            "INSERT INTO nodes (id, layer, name, embedding) VALUES (:1,:2,:3,:4)",
            [node_id, layer, name,
             json.dumps(vec).encode() if vec is not None else None])
    if parent_id:
        cur.execute("""MERGE INTO edges e USING dual ON (e.src=:src AND e.dst=:dst)
                       WHEN MATCHED THEN UPDATE SET raw_count = raw_count+:inc,
                            weight = weight+:inc
                       WHEN NOT MATCHED THEN INSERT (src, dst, weight, raw_count)
                       VALUES (:src, :dst, :inc, :inc)""",
                    {"src": parent_id, "dst": node_id, "inc": 1 if count else 0})
        # ponytail: weight=raw_count. 노출 대비 채택률 보정은 제안 기능이 생긴 뒤에
    # 같은 출처가 같은 노드에 두 번 기여해도 안전 (PK 중복 방지)
    cur.execute("""MERGE INTO node_evidence e USING dual
                   ON (e.node_id = :n AND e.kind = :k AND e.ref = :r AND e.run_id = :rid)
                   WHEN NOT MATCHED THEN INSERT (node_id, kind, ref, run_id)
                   VALUES (:n, :k, :r, :rid)""",
                {"n": node_id, "k": ev_kind, "r": ev_ref, "rid": run_id})
    return node_id
