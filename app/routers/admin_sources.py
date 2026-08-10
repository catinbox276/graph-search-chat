"""도메인 시드·원천 테이블·전처리 설정·구조화 운영 (드라이런/재시도/초기화)."""
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.deps import check_admin, db
from tools import config, settings, source_registry

router = APIRouter()


class DomainIn(BaseModel):
    name: str
    tools: str = ""     # 쉼표구분 도구명 — 이 도구를 쓴 세션이 이 도메인으로 분류됨 (scope=doc이면 불필요)
    priority: int = 100  # 낮을수록 먼저 대조. 최하순위가 폴백 도메인
    extract_hint: str = ""  # 도메인별 추출 지침 — 목표·접근법 추출 프롬프트에 주입 (대화·문서 공통)
    scope: str = "both"  # 사용 목적: both(대화+문서) | chat(대화 전용) | doc(문서 전용)


@router.get("/admin/domains")
def admin_domains(request: Request):
    """관리자: 1층 도메인 닫힌 목록 조회 (시드 테이블 domain_registry)."""
    check_admin(request)
    from poc.graph_pipeline import ensure_domain_registry
    con = db()
    cur = con.cursor()
    ensure_domain_registry(cur)
    con.commit()
    cur.execute("""SELECT name, tools, priority, extract_hint, NVL(scope, 'both')
                   FROM domain_registry ORDER BY priority, name""")
    rows = [{"name": r[0], "tools": r[1] or "", "priority": r[2],
             "extract_hint": r[3] or "", "scope": r[4]}
            for r in cur.fetchall()]
    con.close()
    return {"domains": rows}


@router.post("/admin/domains")
def admin_domain_add(inp: DomainIn, request: Request):
    """관리자: 도메인 추가/수정 — 닫힌 1층 목록의 유일한 확장 통로 (사람 전용).

    등록 때 사용 목적(scope)을 명시 선택한다: both(대화+문서)/chat(대화 전용)/
    doc(문서 전용). doc 도메인은 대화 분류·폴백에 안 끼고 소스(📚) 지정으로만 쓴다.
    다음 파이프라인 실행(야간)부터 반영되고, 기존 세션 소급 재분류는 하지 않는다
    (안전 기본값). 삭제 API는 일부러 없음 — 도메인 삭제·병합은 기존 노드 재배치가
    필요한 신중한 작업이라 SQL로만.
    """
    check_admin(request)
    if not inp.name.strip():
        raise HTTPException(400, "name은 필수입니다")
    scope = inp.scope.strip().lower() or "both"
    if scope not in ("both", "chat", "doc"):
        raise HTTPException(400, "scope는 both/chat/doc 중 하나입니다")
    if scope != "doc" and not inp.tools.strip():
        raise HTTPException(400, "대화 분류에 쓰는 도메인(both/chat)은 tools(쉼표구분)가 필요합니다")
    from poc.graph_pipeline import ensure_domain_registry
    con = db()
    cur = con.cursor()
    ensure_domain_registry(cur)
    cur.execute("""MERGE INTO domain_registry d USING dual ON (d.name = :n)
                   WHEN MATCHED THEN UPDATE SET tools = :t, priority = :p,
                        extract_hint = :h, scope = :s
                   WHEN NOT MATCHED THEN INSERT (name, tools, priority, extract_hint, scope)
                   VALUES (:n, :t, :p, :h, :s)""",
                {"n": inp.name.strip(), "t": inp.tools.strip() or None, "p": inp.priority,
                 "h": inp.extract_hint.strip() or None, "s": scope})
    con.commit()
    con.close()
    note = {"doc": "문서 전용 — 소스(📚)에 지정하면 문서 구조화에 사용 (대화 분류엔 안 낌)",
            "chat": "대화 전용 — 다음 파이프라인 실행부터 신규 세션 분류에 반영",
            "both": "대화+문서 — 세션 분류와 소스 문서 구조화 양쪽에 사용"}[scope]
    return {"ok": True, "name": inp.name.strip(), "scope": scope, "note": note}


class SourceIn(BaseModel):
    source_name: str
    table_name: str      # 원천 테이블 (읽기 전용 — 우리는 SELECT만)
    id_column: str       # 고유 id 필드
    ts_column: str = ""  # 생성시간 필드 — 증분 워터마크 (빈값 = 전량 1회 소스)
    field_map: dict      # {역할: 컬럼} 역할=title|body|question|answer|meta|url
    content_kind: str = ""  # 문제해결/가이드 등 — 프롬프트 힌트
    domain: str = ""     # 그래프 구조화 도메인 — 지정 시 doc_pipeline이 LLM 판정·구조화 (빈값=검색만)
    enabled: bool = True
    url_enabled: bool = True  # N이면 검색·출처·문서 뷰에서 원본 링크 숨김 (즉시 반영)


@router.get("/admin/sources")
def admin_sources(request: Request):
    """관리자: 구조화 원천 테이블 목록 (source_registry)."""
    check_admin(request)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    rows = source_registry.list_sources(cur)
    con.commit()
    con.close()
    return {"sources": rows}


@router.get("/admin/doc-status")
def admin_doc_status(request: Request):
    """관리자: 문서 구조화 진행 현황 — 도메인 지정 소스별 상태 카운트 (UI 프로그래스용)."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT r.source_name, r.domain,
               COUNT(d.src_id),
               SUM(CASE WHEN d.graph_status = 'done' THEN 1 ELSE 0 END),
               SUM(CASE WHEN d.graph_status = 'excluded' THEN 1 ELSE 0 END),
               SUM(CASE WHEN d.graph_status = 'error' THEN 1 ELSE 0 END),
               SUM(CASE WHEN d.graph_status IS NULL AND d.src_id IS NOT NULL
                        THEN 1 ELSE 0 END)
        FROM source_registry r
        LEFT JOIN corpus_docs d ON d.source_name = r.source_name
        WHERE r.domain IS NOT NULL
        GROUP BY r.source_name, r.domain ORDER BY r.domain, r.source_name""")
    rows = [{"source": r[0], "domain": r[1], "total": r[2] or 0, "done": r[3] or 0,
             "excluded": r[4] or 0, "error": r[5] or 0, "pending": r[6] or 0}
            for r in cur.fetchall()]
    con.close()
    return {"sources": rows}


@router.post("/admin/sources")
def admin_source_add(inp: SourceIn, request: Request):
    """관리자: 원천 테이블 등록/수정 — 테이블·컬럼 실존을 검증하고 저장.

    다음 적재 배치부터 반영. 원천 테이블은 읽기 전용(우리는 SELECT만)이고,
    삭제 API는 domain_registry와 같은 이유로 없음(enabled='N'으로 끄는 것까지만).
    """
    check_admin(request)
    from tools import source_registry
    if not inp.source_name.strip() or not inp.table_name.strip() or not inp.id_column.strip():
        raise HTTPException(400, "source_name·table_name·id_column은 필수입니다")
    con = db()
    cur = con.cursor()
    source_registry.ensure(cur)
    err = source_registry.validate(cur, inp.table_name.strip(), inp.id_column.strip(),
                                   inp.ts_column.strip(), inp.field_map)
    if err:
        con.close()
        raise HTTPException(400, err)
    domain = inp.domain.strip()
    if domain:  # 지정 시 닫힌 도메인 목록에 실존 + 문서 용도(both/doc)여야 함
        from poc.graph_pipeline import ensure_domain_registry
        ensure_domain_registry(cur)
        cur.execute("SELECT NVL(scope, 'both') FROM domain_registry WHERE name = :1",
                    [domain])
        r = cur.fetchone()
        if not r:
            con.close()
            raise HTTPException(400, f"등록되지 않은 도메인: {domain} (⚙ 관리에서 먼저 추가)")
        if r[0] == "chat":
            con.close()
            raise HTTPException(400, f"도메인 '{domain}'은 대화 전용입니다 — "
                                     "문서 구조화에 쓰려면 용도를 '대화+문서'나 '문서 전용'으로")
    source_registry.upsert(cur, inp.source_name.strip(), inp.table_name.strip(),
                           inp.id_column.strip(), inp.ts_column.strip(),
                           inp.field_map, inp.content_kind.strip(), inp.enabled,
                           domain=domain, url_enabled=inp.url_enabled)
    con.commit()
    con.close()
    return {"ok": True, "source_name": inp.source_name.strip(),
            "note": "다음 적재 배치부터 반영 (원천 테이블은 읽기 전용)"}


@router.get("/admin/sources/tables")
def admin_source_tables(request: Request):
    """관리자: 접속 DB의 등록 후보 테이블 목록 (Oracle 내부·우리 테이블 제외)."""
    check_admin(request)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    tables = source_registry.browse_tables(cur)
    con.close()
    return {"tables": tables}


@router.get("/admin/sources/tables/{tname}")
def admin_source_columns(tname: str, request: Request):
    """관리자: 테이블의 컬럼 목록 — 등록 폼의 컬럼 선택용."""
    check_admin(request)
    from tools import source_registry
    con = db()
    cur = con.cursor()
    cols = source_registry.table_columns(cur, tname)
    con.close()
    if not cols:
        raise HTTPException(404, f"테이블이 없습니다: {tname}")
    return {"columns": [{"name": k, "type": v} for k, v in cols.items()]}


@router.get("/admin/pipeline-settings")
def admin_pipeline_settings(request: Request):
    """관리자: 전처리(문서 구조화) 운영 설정 — 효과값 반환 (DB 없으면 .env 기본값)."""
    check_admin(request)
    from tools import settings
    st = settings.get_all()
    return {"doc_extract_limit": settings.get_int(st, "doc_extract_limit",
                                                  config.DOC_EXTRACT_LIMIT),
            "doc_concurrency": settings.get_int(st, "doc_concurrency",
                                                config.DOC_CONCURRENCY),
            "doc_body_chars": settings.get_int(st, "doc_body_chars",
                                               config.DOC_BODY_CHARS),
            "doc_pack_tokens": settings.get_int(st, "doc_pack_tokens",
                                                config.DOC_PACK_TOKENS),
            "doc_no_think": settings.get_int(st, "doc_no_think", config.DOC_NO_THINK),
            "doc_extract_model": st.get("doc_extract_model") or "",
            "chunk_chars": settings.get_int(st, "chunk_chars", config.CHUNK_CHARS),
            "chunk_overlap": settings.get_int(st, "chunk_overlap", config.CHUNK_OVERLAP),
            "overridden": sorted(st.keys())}


class PipelineSettingsIn(BaseModel):
    doc_extract_limit: str = ""   # 빈값 = 기본값 복귀 (문자열로 받아 검증)
    doc_concurrency: str = ""
    doc_body_chars: str = ""
    doc_pack_tokens: str = ""     # 0=1건씩 / N=입력 N토큰 예산으로 묶음 판정
    doc_no_think: str = ""        # 1=추론(생각) 출력 끔 (기본) / 0=켬
    doc_extract_model: str = ""   # 빈값 = 대화 모델 사용
    chunk_chars: str = ""         # 청크 크기(자)
    chunk_overlap: str = ""       # 인접 청크 겹침(자)


@router.post("/admin/pipeline-settings")
def admin_pipeline_settings_set(inp: PipelineSettingsIn, request: Request):
    """관리자: 전처리 설정 저장 — 다음 배치 실행부터 반영 (재배포 불필요)."""
    check_admin(request)
    from tools import settings
    vals = {}
    for key, raw, lo, hi in (("doc_extract_limit", inp.doc_extract_limit, 1, 100000),
                             ("doc_concurrency", inp.doc_concurrency, 1, 256),
                             ("doc_body_chars", inp.doc_body_chars, 200, 20000),
                             ("doc_pack_tokens", inp.doc_pack_tokens, 0, 30000),
                             ("doc_no_think", inp.doc_no_think, 0, 1),
                             ("chunk_chars", inp.chunk_chars, 200, 8000),
                             ("chunk_overlap", inp.chunk_overlap, 0, 2000)):
        raw = raw.strip()
        if raw:
            try:
                v = int(raw)
            except ValueError:
                raise HTTPException(400, f"{key}는 정수여야 합니다")
            if not lo <= v <= hi:
                raise HTTPException(400, f"{key}는 {lo}~{hi} 범위여야 합니다")
        vals[key] = raw
    vals["doc_extract_model"] = inp.doc_extract_model.strip()
    settings.set_many(vals)
    return {"ok": True, "note": "다음 전처리 배치 실행부터 반영 (빈값은 기본값 복귀)"}



class ReprocessIn(BaseModel):
    mode: str  # errors = 실패만 재시도 | reset = 소스 전체 초기화(그래프 증거 회수 포함)


@router.post("/admin/sources/{sname}/reprocess")
def admin_source_reprocess(sname: str, inp: ReprocessIn, request: Request):
    """관리자: 소스 재처리 준비.

    errors: error 상태만 미처리로 되돌림 (다음 배치가 재시도)
    reset : 소스 전체 초기화 — 이 소스의 문서가 그래프에 올린 기여(엣지 +1, 증거)를
            먼저 회수한 뒤 상태를 리셋한다. 그냥 리셋하면 재처리 때 이중 카운트되기
            때문 (재발 소급 취소와 같은 원리). 지침·모델 변경 후 재구조화용.
    """
    check_admin(request)
    con = db()
    cur = con.cursor()
    if inp.mode == "errors":
        cur.execute("""UPDATE corpus_docs SET graph_status = NULL, graph_note = NULL
                       WHERE source_name = :1 AND graph_status = 'error'""", [sname])
        n = cur.rowcount
        con.commit()
        con.close()
        return {"ok": True, "reset": n, "note": "error 문서를 미처리로 — 다음 배치가 재시도"}
    if inp.mode != "reset":
        con.close()
        raise HTTPException(400, "mode는 errors 또는 reset")
    n, retracted = _reset_source(cur, sname)
    con.commit()
    con.close()
    return {"ok": True, "reset": n, "evidence_retracted": retracted,
            "note": "그래프 기여 회수 완료 — 다음 배치가 처음부터 재구조화 "
                    "(고아 노드는 야간 유지보수가 정리)"}


def _reset_source(cur, sname: str):
    """소스 1개의 그래프 기여(엣지 +1, 증거) 회수 후 문서 상태 리셋. commit은 호출자가."""
    # 증거 회수: 문서 ref마다 그 문서가 만든 노드 집합 내부 엣지에서 기여 -1
    cur.execute("""SELECT DISTINCT ref FROM node_evidence
                   WHERE kind = 'doc' AND ref LIKE :1""", [f"{sname}:%"])
    refs = [r[0] for r in cur.fetchall()]
    for ref in refs:
        cur.execute("""SELECT node_id FROM node_evidence
                       WHERE kind = 'doc' AND ref = :1""", [ref])
        nids = [r[0] for r in cur.fetchall()]
        for j in range(0, len(nids), 100):
            chunk = nids[j:j + 100]
            src_marks = ",".join(f":s{k}" for k in range(len(chunk)))
            dst_marks = ",".join(f":d{k}" for k in range(len(chunk)))
            binds = {f"s{k}": v for k, v in enumerate(chunk)}
            binds.update({f"d{k}": v for k, v in enumerate(chunk)})
            cur.execute(
                f"""UPDATE edges SET raw_count = GREATEST(raw_count - 1, 0),
                                     weight = GREATEST(weight - 1, 0)
                    WHERE src IN ({src_marks}) AND dst IN ({dst_marks})""", binds)
        cur.execute("DELETE FROM node_evidence WHERE kind = 'doc' AND ref = :1", [ref])
    cur.execute("""UPDATE corpus_docs SET graph_status = NULL, graph_note = NULL
                   WHERE source_name = :1 AND graph_status IS NOT NULL""", [sname])
    return cur.rowcount, len(refs)


@router.post("/admin/domains/{dname}/reset")
def admin_domain_reset(dname: str, request: Request):
    """관리자: 도메인 초기화 — 이 도메인에 물린 모든 소스의 문서 구조화를 회수·리셋.
    대화 세션 기여는 건드리지 않는다 (문서 쪽만)."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    cur.execute("SELECT source_name FROM source_registry WHERE domain = :1", [dname])
    names = [r[0] for r in cur.fetchall()]
    if not names:
        con.close()
        raise HTTPException(404, f"도메인 '{dname}'에 지정된 소스가 없습니다")
    per = {s: _reset_source(cur, s) for s in names}
    con.commit()
    con.close()
    return {"ok": True, "sources": {s: {"reset": n, "evidence_retracted": r}
                                    for s, (n, r) in per.items()},
            "note": "다음 배치가 처음부터 재구조화 (고아 노드는 야간 유지보수가 정리)"}


@router.post("/admin/reset-all-docs")
def admin_reset_all_docs(request: Request):
    """관리자: 전체 초기화 — 도메인 지정된 모든 소스의 문서 구조화를 회수·리셋."""
    check_admin(request)
    con = db()
    cur = con.cursor()
    cur.execute("SELECT source_name FROM source_registry WHERE domain IS NOT NULL")
    names = [r[0] for r in cur.fetchall()]
    per = {s: _reset_source(cur, s) for s in names}
    con.commit()
    con.close()
    return {"ok": True, "sources": {s: {"reset": n, "evidence_retracted": r}
                                    for s, (n, r) in per.items()},
            "note": "다음 배치가 처음부터 재구조화 (고아 노드는 야간 유지보수가 정리)"}


class DryrunIn(BaseModel):
    n: int = 3  # 판정해볼 문서 수 (최대 5 — 그래프에 반영하지 않음)


@router.post("/admin/sources/{sname}/dryrun")
def admin_source_dryrun(sname: str, inp: DryrunIn, request: Request):
    """관리자: 드라이런 — 미처리 문서 N건을 판정만 해보고 결과를 보여준다.

    그래프·상태에 아무것도 쓰지 않는다. 새 소스·새 추출 지침을 튜닝할 때
    'excluded가 얼마나 나오나'를 배치 전에 확인하는 용도.
    """
    check_admin(request)
    n = max(1, min(inp.n, 5))
    con = db()
    cur = con.cursor()
    cur.execute("""SELECT s.domain, NVL(d.extract_hint, ' ')
                   FROM source_registry s
                   JOIN domain_registry d ON d.name = s.domain
                   WHERE s.source_name = :1 AND s.domain IS NOT NULL""", [sname])
    r = cur.fetchone()
    if not r:
        con.close()
        raise HTTPException(400, "이 소스에 그래프 도메인이 지정되어 있지 않습니다")
    domain, hint = r[0], r[1]
    cur.execute("""SELECT src_id, NVL(title, ' '), NVL(kind, ' '), body
                   FROM corpus_docs
                   WHERE source_name = :1 AND graph_status IS NULL
                   FETCH FIRST :2 ROWS ONLY""", [sname, n])
    docs = [(row[0], row[1], row[2],
             row[3].read() if hasattr(row[3], "read") else (row[3] or ""))
            for row in cur.fetchall()]
    from tools import settings
    st = settings.get_all()
    con.close()
    if not docs:
        return {"domain": domain, "results": [], "note": "미처리 문서가 없습니다"}
    from poc.doc_pipeline import judge_doc
    body_chars = settings.get_int(st, "doc_body_chars", config.DOC_BODY_CHARS)
    model = (st.get("doc_extract_model") or "").strip()
    out = []
    for src_id, title, kind, body in docs:
        j = judge_doc(domain, hint, kind, title, body,
                      model=model, body_chars=body_chars)
        out.append({"src_id": src_id, "title": title.strip()[:120],
                    "fits": bool(j.get("fits")), "reason": j.get("reason") or
                    j.get("_error") or "파싱 실패",
                    "goal": j.get("goal") or "", "approach": j.get("approach") or ""})
    return {"domain": domain, "results": out,
            "note": "판정만 수행 — 그래프·상태에 반영 안 됨"}

