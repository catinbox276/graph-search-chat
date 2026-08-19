"""구조화 실행(run) 버저닝 — 조합(소스×도메인 버전×모델×설정)별 실행 기록·귀속.

B-full 설계 (docs/plan 협의 2026-08-14):
- doc_runs: 실행 1건 = 조합 스냅샷 + 시작/종료 시각 + active(사용자에게 반영되는 버전)
- doc_results: run별 문서 판정 결과 (corpus_docs.graph_status는 '활성 run의 캐시')
- node_evidence.run_id: 문서 증거를 run에 귀속 (세션 증거는 '-') — 같은 문서를
  두 run이 판정해도 PK(node_id,kind,ref,run_id) 충돌 없음
- 기존 데이터는 legacy run 1건으로 백필 (active='Y')
"""
import json
import uuid

from core import config, settings, versioning


def ensure_runs(cur):
    """run 스키마 생성 + node_evidence run 귀속 컬럼 + 기존 데이터 legacy 백필 (멱등)."""
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DOC_RUNS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE doc_runs (
            run_id      VARCHAR2(32) PRIMARY KEY,
            source_name VARCHAR2(100) NOT NULL,
            domain      VARCHAR2(100),
            domain_version NUMBER,
            chat_model  VARCHAR2(200),
            embed_model VARCHAR2(200),
            settings    VARCHAR2(1000),        -- body_chars 등 스냅샷 (JSON)
            active      CHAR(1) DEFAULT 'N',   -- 사용자에게 반영되는 버전 (소스당 1개)
            started     TIMESTAMP DEFAULT SYSTIMESTAMP,
            finished    TIMESTAMP)""")
        cur.execute("CREATE INDEX doc_runs_src_ix ON doc_runs (source_name)")
    # settings VARCHAR2(1000) → CLOB — 긴 엔티티 추출 프롬프트 스냅샷 수용 (멱등).
    # Oracle은 VARCHAR2→CLOB 직접 MODIFY 불가(ORA-22858) → 새 컬럼 복사 후 교체.
    cur.execute("""SELECT data_type FROM user_tab_columns
                   WHERE table_name = 'DOC_RUNS' AND column_name = 'SETTINGS'""")
    dt = cur.fetchone()
    if dt and dt[0] != "CLOB":
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name = 'DOC_RUNS' AND column_name = 'SETTINGS_C'""")
        if cur.fetchone()[0]:            # 이전 부분 실패 잔재 정리
            cur.execute("ALTER TABLE doc_runs DROP COLUMN settings_c")
        cur.execute("ALTER TABLE doc_runs ADD (settings_c CLOB)")
        cur.execute("UPDATE doc_runs SET settings_c = settings")
        cur.execute("ALTER TABLE doc_runs DROP COLUMN settings")
        cur.execute("ALTER TABLE doc_runs RENAME COLUMN settings_c TO settings")
    for col in ("ENTITY_VERSION", "CLUSTER_VERSION", "JOIN_VERSION", "DATA_VERSION"):   # 조합 참조 버전 (멱등)
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name = 'DOC_RUNS' AND column_name = :1""", [col])
        if not cur.fetchone()[0]:
            cur.execute(f"ALTER TABLE doc_runs ADD ({col} NUMBER)")
    for col in ("ENTITY_LINE", "CLUSTER_LINE"):   # 엔티티·클러스터 라인 이름 (멱등)
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name = 'DOC_RUNS' AND column_name = :1""", [col])
        if not cur.fetchone()[0]:
            cur.execute(f"ALTER TABLE doc_runs ADD ({col} VARCHAR2(100))")
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DOC_RESULTS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE doc_results (
            run_id      VARCHAR2(32) NOT NULL,
            source_name VARCHAR2(100) NOT NULL,
            src_id      VARCHAR2(200) NOT NULL,
            status      VARCHAR2(20),
            note        VARCHAR2(1000),
            judged_at   TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT doc_results_pk PRIMARY KEY (run_id, source_name, src_id),
            CONSTRAINT doc_results_run_fk FOREIGN KEY (run_id)
              REFERENCES doc_runs(run_id) ON DELETE CASCADE)""")
    # node_evidence.run_id — 문서 증거의 run 귀속 (세션·구버전은 '-')
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = 'NODE_EVIDENCE' AND column_name = 'RUN_ID'""")
    if not cur.fetchone()[0]:
        cur.execute("ALTER TABLE node_evidence ADD (run_id VARCHAR2(32) DEFAULT '-' NOT NULL)")
        # 구버전 PK는 시스템 이름(SYS_C…)일 수 있어 실제 이름을 조회해 드랍
        cur.execute("""SELECT constraint_name FROM user_constraints
                       WHERE table_name = 'NODE_EVIDENCE' AND constraint_type = 'P'""")
        r = cur.fetchone()
        if r:
            cur.execute(f'ALTER TABLE node_evidence DROP CONSTRAINT "{r[0]}" DROP INDEX')
        cur.execute("""ALTER TABLE node_evidence ADD CONSTRAINT node_evidence_pk
                       PRIMARY KEY (node_id, kind, ref, run_id)""")
    _backfill_legacy(cur)


def _combo(cur, source_name: str) -> dict:
    """현재 유효 조합 스냅샷 — 도메인(기본 버전)·모델·전처리 설정."""
    cur.execute("""SELECT s.domain, NVL(v.version, 1)
                   FROM source_registry s
                   LEFT JOIN domain_versions v
                     ON v.name = s.domain AND v.is_default = 'Y'
                   WHERE s.source_name = :1""", [source_name])
    r = cur.fetchone() or (None, None)
    st = settings.get_all()
    return {"domain": r[0], "domain_version": int(r[1]) if r[1] else None,
            "chat_model": (st.get("doc_extract_model") or "").strip() or config.CHAT_MODEL,
            "embed_model": config.EMBED_MODEL,
            "settings": json.dumps({
                "body_chars": settings.get_int(st, "doc_body_chars", config.DOC_BODY_CHARS),
                "pack_tokens": settings.get_int(st, "doc_pack_tokens", config.DOC_PACK_TOKENS),
                "no_think": settings.get_int(st, "doc_no_think", config.DOC_NO_THINK),
                # 엔티티(문서→목표·접근법) 추출 프롬프트 스냅샷 — 빈값=코드 기본
                "doc_prompt": (st.get("struct_doc_prompt") or ""),
                "pack_prompt": (st.get("struct_pack_prompt") or ""),
                # 클러스터(dedup) 설정 스냅샷 — config 기본값
                "dedup": {"sim_high": config.DEDUP_SIM_HIGH,
                          "sim_threshold": config.DEDUP_SIM_THRESHOLD,
                          "short_name_chars": config.DEDUP_SHORT_NAME_CHARS,
                          "char_ratio": config.DEDUP_CHAR_RATIO,
                          "select_max": config.DEDUP_SELECT_MAX}},
                ensure_ascii=False)}


def current_run(cur, source_name: str) -> str:
    """이 소스의 활성 run을 반환 — 없으면 현재 조합으로 생성(첫 run은 자동 활성).

    조합이 바뀌어도 활성 run을 자동 교체하지 않는다(사용자 버전 선택 존중) —
    새 조합 run은 관리 API로 명시 생성한다."""
    ensure_runs(cur)
    cur.execute("SELECT run_id FROM doc_runs WHERE source_name = :1 AND active = 'Y'",
                [source_name])
    r = cur.fetchone()
    if r:
        return r[0]
    c = _combo(cur, source_name)
    rid = uuid.uuid4().hex
    cur.execute("""INSERT INTO doc_runs (run_id, source_name, domain, domain_version,
                     chat_model, embed_model, settings, active)
                   VALUES (:1, :2, :3, :4, :5, :6, :7, 'Y')""",
                [rid, source_name, c["domain"], c["domain_version"],
                 c["chat_model"], c["embed_model"], c["settings"]])
    return rid


def record_result(cur, run_id: str, source_name: str, src_id: str,
                  status: str, note: str):
    """run별 문서 판정 결과 기록 (재판정 시 갱신)."""
    cur.execute("""MERGE INTO doc_results r USING dual
                   ON (r.run_id = :rid AND r.source_name = :s AND r.src_id = :d)
                   WHEN MATCHED THEN UPDATE SET status = :st, note = :nt,
                        judged_at = SYSTIMESTAMP
                   WHEN NOT MATCHED THEN INSERT (run_id, source_name, src_id, status, note)
                   VALUES (:rid, :s, :d, :st, :nt)""",
                {"rid": run_id, "s": source_name, "d": src_id,
                 "st": status, "nt": (note or "")[:1000] or None})


def _lob(v) -> str:
    return (v.read() if hasattr(v, "read") else v) or ""


def _default_mapping_ver(cur, source_name: str):
    try:
        cur.execute("SELECT version FROM mapping_versions WHERE source_name = :1 AND is_default = 'Y'",
                    [source_name])
        r = cur.fetchone()
        if r:
            return int(r[0])
        cur.execute("SELECT MAX(version) FROM mapping_versions WHERE source_name = :1", [source_name])
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def _default_data_ver(cur, source_name: str):
    try:
        cur.execute("SELECT version FROM data_versions WHERE source_name = :1 AND is_default = 'Y'",
                    [source_name])
        r = cur.fetchone()
        if r:
            return int(r[0])
        cur.execute("SELECT MAX(version) FROM data_versions WHERE source_name = :1", [source_name])
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def create_run(cur, source_name: str, domain_version=None, entity_line="", entity_version=None,
               cluster_line="", cluster_version=None, join_version=None, data_version=None,
               chat_model="", embed_model="", body_chars=None,
               pack_tokens=None, no_think=None, dedup=None) -> str:
    """새 조합 run 생성 (비활성) — 도메인 버전 + 엔티티·클러스터 (라인, 버전) + 매핑·데이터
    버전을 참조해 그 내용을 run에 스냅샷(재현성). 지정 안 한 항목은 현재/활성값.
    구조화는 run 지정 실행으로, 반영은 activate_run으로 명시 전환."""
    ensure_runs(cur)
    versioning.ensure(cur, versioning.ENTITY_SPEC)    # name 컬럼·기본 라인 보장
    versioning.ensure(cur, versioning.CLUSTER_SPEC)
    c = _combo(cur, source_name)
    st = json.loads(c["settings"])
    if body_chars is not None:
        st["body_chars"] = body_chars
    if pack_tokens is not None:
        st["pack_tokens"] = pack_tokens
    if no_think is not None:
        st["no_think"] = no_think
    # 엔티티 (라인, 버전) 선택 → 그 버전의 프롬프트를 스냅샷 (미지정=활성 라인)
    en_name, ev = (entity_line or None), entity_version
    if not (en_name and ev):
        en_name, ev = versioning.active(cur, "entity_versions")
    if en_name and ev is not None:
        cur.execute("SELECT doc_prompt, pack_prompt FROM entity_versions WHERE name = :1 AND version = :2",
                    [en_name, ev])
        r = cur.fetchone()
        if r:
            st["doc_prompt"], st["pack_prompt"] = _lob(r[0]), _lob(r[1])
    # 클러스터 (라인, 버전) 선택 → dedup 스냅샷 (미지정=활성 라인)
    cl_name, cv = (cluster_line or None), cluster_version
    if not (cl_name and cv):
        cl_name, cv = versioning.active(cur, "cluster_versions")
    if cl_name and cv is not None:
        cur.execute("""SELECT sim_high, sim_threshold, short_name_chars, char_ratio, select_max
                       FROM cluster_versions WHERE name = :1 AND version = :2""", [cl_name, cv])
        r = cur.fetchone()
        if r:
            st["dedup"] = {"sim_high": float(r[0]), "sim_threshold": float(r[1]),
                           "short_name_chars": int(r[2]), "char_ratio": float(r[3]),
                           "select_max": int(r[4])}
    elif dedup:   # 라인 없을 때만 raw override (하위호환)
        st["dedup"] = {**st.get("dedup", {}),
                       **{k: v for k, v in dedup.items() if v is not None}}
    # 매핑 버전(join_version 컬럼 재사용) → 원천 등록 매핑(id·시간·필드) 스냅샷. 데이터 버전 → 기록.
    jv = join_version or _default_mapping_ver(cur, source_name)
    if jv is not None:
        try:
            cur.execute("""SELECT id_column, ts_column, field_map FROM mapping_versions
                           WHERE source_name = :1 AND version = :2""", [source_name, jv])
            r = cur.fetchone()
            if r:
                st["mapping"] = {"id_column": r[0], "ts_column": r[1],
                                 "field_map": json.loads(_lob(r[2]) or "{}")}
        except Exception:
            pass
    dv = data_version or _default_data_ver(cur, source_name)
    rid = uuid.uuid4().hex
    cur.execute("""INSERT INTO doc_runs (run_id, source_name, domain, domain_version,
                     entity_line, entity_version, cluster_line, cluster_version,
                     join_version, data_version, chat_model, embed_model, settings, active)
                   VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9, :10, :11, :12, :13, 'N')""",
                [rid, source_name, c["domain"], domain_version or c["domain_version"],
                 en_name, ev, cl_name, cv, jv, dv, (chat_model or c["chat_model"]).strip(),
                 (embed_model or c["embed_model"]).strip(),
                 json.dumps(st, ensure_ascii=False)])
    return rid


def _run_edge_delta(cur, run_id: str, sign: int) -> int:
    """run의 문서 증거 기여를 엣지 가중치에 가산(+1)/감산(-1).

    ref(문서)마다 그 문서가 만든 노드 집합 내부 엣지에 ±1 — _reset_source와 같은
    근사식이라 활성 전환의 가산/감산이 대칭으로 상쇄된다."""
    cur.execute("""SELECT DISTINCT ref FROM node_evidence
                   WHERE kind = 'doc' AND run_id = :1""", [run_id])
    refs = [r[0] for r in cur.fetchall()]
    op = "+" if sign > 0 else "-"
    for ref in refs:
        cur.execute("""SELECT node_id FROM node_evidence
                       WHERE kind = 'doc' AND ref = :1 AND run_id = :2""", [ref, run_id])
        nids = [r[0] for r in cur.fetchall()]
        for j in range(0, len(nids), 100):
            chunk = nids[j:j + 100]
            src_marks = ",".join(f":s{k}" for k in range(len(chunk)))
            dst_marks = ",".join(f":d{k}" for k in range(len(chunk)))
            binds = {f"s{k}": v for k, v in enumerate(chunk)}
            binds.update({f"d{k}": v for k, v in enumerate(chunk)})
            cur.execute(
                f"""UPDATE edges SET raw_count = GREATEST(raw_count {op} 1, 0),
                                     weight = GREATEST(weight {op} 1, 0)
                    WHERE src IN ({src_marks}) AND dst IN ({dst_marks})""", binds)
    return len(refs)


def activate_run(cur, run_id: str) -> dict:
    """이 run을 소스의 활성 버전으로 전환 — 사용자에게 반영되는 그래프 기여 스위칭.

    ① 기존 활성 run의 기여 감산 → ② 이 run의 기여 가산 → ③ 플래그 교체
    → ④ corpus_docs 상태 캐시를 이 run의 doc_results로 교체."""
    cur.execute("SELECT source_name, active FROM doc_runs WHERE run_id = :1", [run_id])
    r = cur.fetchone()
    if not r:
        raise ValueError(f"run이 없습니다: {run_id}")
    source_name, already = r
    if already == "Y":
        return {"source": source_name, "note": "이미 활성 run입니다", "changed": False}
    cur.execute("""SELECT run_id FROM doc_runs
                   WHERE source_name = :1 AND active = 'Y'""", [source_name])
    old = cur.fetchone()
    subtracted = _run_edge_delta(cur, old[0], -1) if old else 0
    added = _run_edge_delta(cur, run_id, +1)
    cur.execute("UPDATE doc_runs SET active = 'N' WHERE source_name = :1", [source_name])
    cur.execute("UPDATE doc_runs SET active = 'Y' WHERE run_id = :1", [run_id])
    # 상태 캐시 교체 — 이 run이 판정한 문서는 그 상태로, 나머지는 미처리로
    cur.execute("""UPDATE corpus_docs d SET (graph_status, graph_note) =
                     (SELECT r.status, r.note FROM doc_results r
                      WHERE r.run_id = :rid AND r.source_name = d.source_name
                        AND r.src_id = d.src_id)
                   WHERE d.source_name = :s""", {"rid": run_id, "s": source_name})
    return {"source": source_name, "changed": True,
            "subtracted_refs": subtracted, "added_refs": added}


def finish_run(cur, run_id: str):
    cur.execute("UPDATE doc_runs SET finished = SYSTIMESTAMP WHERE run_id = :1", [run_id])


def _backfill_legacy(cur):
    """구조화 이력이 있는데 run이 없는 소스 → legacy run 1건 생성·귀속 (1회성, 멱등)."""
    cur.execute("""SELECT DISTINCT d.source_name FROM corpus_docs d
                   WHERE d.graph_status IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM doc_runs r
                                     WHERE r.source_name = d.source_name)""")
    for (src,) in cur.fetchall():
        c = _combo(cur, src)
        rid = uuid.uuid4().hex
        cur.execute("""INSERT INTO doc_runs (run_id, source_name, domain, domain_version,
                         chat_model, embed_model, settings, active)
                       VALUES (:1, :2, :3, :4, :5, :6, :7, 'Y')""",
                    [rid, src, c["domain"], c["domain_version"],
                     c["chat_model"], c["embed_model"], c["settings"]])
        cur.execute("""INSERT INTO doc_results (run_id, source_name, src_id, status, note)
                       SELECT :1, source_name, src_id, graph_status, graph_note
                       FROM corpus_docs
                       WHERE source_name = :2 AND graph_status IS NOT NULL""", [rid, src])
        cur.execute("""UPDATE node_evidence SET run_id = :1
                       WHERE kind = 'doc' AND ref LIKE :2 AND run_id = '-'""",
                    [rid, f"{src}:%"])
