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

from core import config, settings


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
                "no_think": settings.get_int(st, "doc_no_think", config.DOC_NO_THINK)},
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
