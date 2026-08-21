"""도메인 시드·원천 테이블·전처리 설정·구조화 운영 (드라이런/재시도/초기화).

읽기 규약:
- 전 엔드포인트 관리자 전용 — 라우터 레벨 Depends(check_admin) 한 줄로 강제.
- DB는 `with db_cursor() as cur:` — 정상 시 commit, 예외 시 자동 롤백·반납.
- 입력 검증은 Pydantic 모델(validator) 소관 — 핸들러 안 수동 if-검사 금지.
"""
import threading
import time
import traceback
from typing import Literal

import oracledb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from core import config, events, settings, versioning
from graph import doc_pipeline
from graph.doc_pipeline import scheduler
from graph.doc_pipeline import judge as _judge
from graph.graph_pipeline import ensure_domain_registry, EXTRACT_PROMPT, JUDGE_PROMPT
from ingestion import chunk_corpus, ingest_sources, source_registry
from web.deps import check_admin, db_cursor

router = APIRouter(dependencies=[Depends(check_admin)])


# ── 도메인 (1층 닫힌 목록) ─────────────────────────────────────

class DomainIn(BaseModel):
    name: str
    tools: str = ""     # 쉼표구분 도구명 — 이 도구를 쓴 세션이 이 도메인으로 분류됨 (scope=doc이면 불필요)
    priority: int = 100  # 낮을수록 먼저 대조. 최하순위가 폴백 도메인
    extract_hint: str = ""  # 도메인별 추출 지침 — 목표·접근법 추출 프롬프트에 주입 (대화·문서 공통)
    scope: Literal["both", "chat", "doc"] = "both"  # both(대화+문서) | chat(대화 전용) | doc(문서 전용)
    copy_from: str = ""   # 새 도메인의 추출 스키마·병합 설정을 복사할 원본 도메인 (빈값=기본값)

    @field_validator("name", "tools", "extract_hint")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("scope", mode="before")
    @classmethod
    def _scope_norm(cls, v):
        return (str(v).strip().lower() or "both")

    @model_validator(mode="after")
    def _rules(self):
        if not self.name:
            raise ValueError("name은 필수입니다")
        if self.scope != "doc" and not self.tools:
            raise ValueError("대화 분류에 쓰는 도메인(both/chat)은 tools(쉼표구분)가 필요합니다")
        return self


@router.get("/admin/domains")
def admin_domains():
    """관리자: 1층 도메인 닫힌 목록 조회 (시드 테이블 domain_registry)."""
    with db_cursor() as cur:
        ensure_domain_registry(cur)
        cur.execute("""SELECT name, tools, priority, extract_hint, NVL(scope, 'both')
                       FROM domain_registry ORDER BY priority, name""")
        rows = [{"name": r[0], "tools": r[1] or "", "priority": r[2],
                 "extract_hint": r[3] or "", "scope": r[4]}
                for r in cur.fetchall()]
    return {"domains": rows}


@router.post("/admin/domains")
def admin_domain_add(inp: DomainIn):
    """관리자: 도메인 추가/수정 — 닫힌 1층 목록의 유일한 확장 통로 (사람 전용).

    등록 때 사용 목적(scope)을 명시 선택한다. doc 도메인은 대화 분류·폴백에 안 끼고
    소스(📚) 지정으로만 쓴다. 다음 파이프라인 실행(야간)부터 반영되고, 기존 세션
    소급 재분류는 하지 않는다(안전 기본값). 삭제 API는 일부러 없음 — 도메인 삭제·병합은
    기존 노드 재배치가 필요한 신중한 작업이라 SQL로만.
    """
    with db_cursor() as cur:
        ensure_domain_registry(cur)
        cur.execute("""MERGE INTO domain_registry d USING dual ON (d.name = :n)
                       WHEN MATCHED THEN UPDATE SET tools = :t, priority = :p,
                            extract_hint = :h, scope = :s
                       WHEN NOT MATCHED THEN INSERT (name, tools, priority, extract_hint, scope)
                       VALUES (:n, :t, :p, :h, :s)""",
                    {"n": inp.name, "t": inp.tools or None, "p": inp.priority,
                     "h": inp.extract_hint or None, "s": inp.scope})
        # 도메인도 **한 벌**이다 — 버전 관리를 없앴다 (2026-08-21). domain_versions는
        # 도메인당 1행(v1)만 유지: run은 지침을 값으로 스냅샷하므로 이 행을 되짚지 않고,
        # 남겨두는 건 옛 run·구버전 리더의 폴백 통로일 뿐이다.
        _ensure_domain_versions(cur)
        cur.execute("DELETE FROM domain_versions WHERE name = :1", [inp.name])
        cur.execute("""INSERT INTO domain_versions
                         (name, version, tools, priority, extract_hint, scope, is_default)
                       VALUES (:n, 1, :t, :p, :h, :s, 'Y')""",
                    {"n": inp.name, "t": inp.tools or None, "p": inp.priority,
                     "h": inp.extract_hint or None, "s": inp.scope})
        # 도메인 = 판정 설정 한 벌 — 추출 스키마·병합 설정을 같이 만든다. 빈 값이 아니라
        # 참조할 값으로 시작한다 (copy_from 도메인 → 씨앗 → 코드 기본).
        seeded = not _has_config(cur, inp.name)
        _schema_row(cur, inp.name, inp.copy_from)
        _cluster_row(cur, inp.name, inp.copy_from)
    note = {"doc": "문서 전용 — 소스(📚)에 지정하면 문서 구조화에 사용 (대화 분류엔 안 낌)",
            "chat": "대화 전용 — 다음 파이프라인 실행부터 신규 세션 분류에 반영",
            "both": "대화+문서 — 세션 분류와 소스 문서 구조화 양쪽에 사용"}[inp.scope]
    if seeded:
        src = inp.copy_from or "기본값"
        note += f" · 추출 스키마·병합 설정을 '{src}'에서 복사해 만들었습니다 (판정 설정에서 수정)"
    return {"ok": True, "name": inp.name, "scope": inp.scope, "note": note}


# ── 원천 테이블 소스 ──────────────────────────────────────────

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

    @field_validator("source_name", "table_name", "id_column", "ts_column",
                     "content_kind", "domain")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _rules(self):
        if not (self.source_name and self.table_name and self.id_column):
            raise ValueError("source_name·table_name·id_column은 필수입니다")
        return self


def _load_source(cur, sname: str) -> dict:
    """소스 실존·테이블 허용·원천 테이블 존재를 검증하고 소스 행 반환 (적재·재적재 공통)."""
    source_registry.ensure(cur)
    source_registry.ensure_corpus(cur)
    src = next((s for s in source_registry.list_sources(cur)
                if s["source_name"] == sname), None)
    if not src:
        raise HTTPException(404, f"소스가 없습니다: {sname}")
    if not source_registry.table_allowed(src["table_name"]):
        raise HTTPException(403, f"허용되지 않은 테이블: {src['table_name']}")
    if not source_registry.table_columns(cur, src["table_name"]):
        raise HTTPException(400,
            f"원천 테이블 '{src['table_name']}'이 이 DB에 없습니다 — 테이블명을 확인하세요. "
            "(시드 blog_posts는 이관 전용이라 원천 테이블이 없어 적재 대상이 아닙니다)")
    return src


@router.get("/admin/sources")
def admin_sources():
    """관리자: 구조화 원천 테이블 목록 (source_registry)."""
    with db_cursor() as cur:
        rows = source_registry.list_sources(cur)
    return {"sources": rows}


@router.get("/admin/doc-status")
def admin_doc_status():
    """관리자: 문서 구조화 진행 현황 — 도메인 지정 소스별 상태 카운트 (UI 프로그래스용)."""
    with db_cursor() as cur:
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
    return {"sources": rows}


def _check_doc_domain(cur, name: str):
    """도메인이 닫힌 목록에 실존 + 문서 용도(both/doc)인지 — 소스 등록·프리셋 공용."""
    ensure_domain_registry(cur)
    cur.execute("SELECT NVL(scope, 'both') FROM domain_registry WHERE name = :1", [name])
    r = cur.fetchone()
    if not r:
        raise HTTPException(400, f"등록되지 않은 도메인: {name} (⚙ 관리에서 먼저 추가)")
    if r[0] == "chat":
        raise HTTPException(400, f"도메인 '{name}'은 대화 전용입니다 — "
                                 "문서 구조화에 쓰려면 용도를 '대화+문서'나 '문서 전용'으로")


@router.post("/admin/sources")
def admin_source_add(inp: SourceIn):
    """관리자: 원천 테이블 등록/수정 — 테이블·컬럼 실존을 검증하고 저장.

    다음 적재 배치부터 반영. 원천 테이블은 읽기 전용(우리는 SELECT만)이고,
    삭제 API는 domain_registry와 같은 이유로 없음(enabled='N'으로 끄는 것까지만).
    """
    with db_cursor() as cur:
        source_registry.ensure(cur)
        err = source_registry.validate(cur, inp.table_name, inp.id_column,
                                       inp.ts_column, inp.field_map)
        if err:
            raise HTTPException(400, err)
        if inp.domain:
            _check_doc_domain(cur, inp.domain)
        source_registry.upsert(cur, inp.source_name, inp.table_name,
                               inp.id_column, inp.ts_column,
                               inp.field_map, inp.content_kind, inp.enabled,
                               domain=inp.domain, url_enabled=inp.url_enabled)
        _auto_mapping_version(cur, inp)   # 매핑(id·시간·필드)이 바뀌었으면 자동 버전
    return {"ok": True, "source_name": inp.source_name,
            "note": "다음 적재 배치부터 반영 (원천 테이블은 읽기 전용)"}


def _auto_mapping_version(cur, inp):
    """등록/수정 저장 시 매핑 자동 버전 — 등록 자체가 버저닝의 시작(v1).
    최신 버전과 다르면 MAX+1 자동 생성(note='등록/수정 자동'), 같으면 no-op.
    is_default는 건드리지 않는다 — 관리자가 pin한 기본을 자동이 덮지 않게."""
    import json
    _ensure_mapping_versions(cur)
    cur.execute("SELECT COUNT(*) FROM mapping_versions WHERE source_name = :1",
                [inp.source_name])
    if not cur.fetchone()[0]:
        _seed_mapping_v1(cur, inp.source_name)   # 첫 등록 = v1(기본)
        return
    # 신규 값을 upsert와 같은 규칙으로 정규화 후, 최신 버전과 비교
    new_id = inp.id_column.upper()
    new_ts = inp.ts_column.upper() if inp.ts_column else None
    new_fm = {k: v.upper() for k, v in inp.field_map.items()}
    cur.execute("""SELECT id_column, ts_column, field_map FROM mapping_versions
                   WHERE source_name = :1
                     AND version = (SELECT MAX(version) FROM mapping_versions
                                    WHERE source_name = :1)""", [inp.source_name])
    r = cur.fetchone()
    try:   # field_map은 dict 동등 비교 — JSON 키 순서 무관
        old_fm = json.loads(_lob_str(r[2]) or "{}")
    except (json.JSONDecodeError, TypeError):
        old_fm = {}
    if (r[0] or None) == (new_id or None) and (r[1] or None) == new_ts and old_fm == new_fm:
        return   # 변경 없음
    cur.execute("""INSERT INTO mapping_versions (source_name, version, id_column, ts_column,
                     field_map, note)
                   SELECT :s, NVL(MAX(version), 0) + 1, :i, :t, :f, '등록/수정 자동'
                   FROM mapping_versions WHERE source_name = :s""",
                {"s": inp.source_name, "i": new_id, "t": new_ts,
                 "f": json.dumps(new_fm, ensure_ascii=False)})


@router.get("/admin/sources/tables")
def admin_source_tables():
    """관리자: 접속 DB의 등록 후보 테이블 목록 (Oracle 내부·우리 테이블 제외)."""
    with db_cursor() as cur:
        tables = source_registry.browse_tables(cur)
    return {"tables": tables}


@router.get("/admin/sources/tables/{tname}")
def admin_source_columns(tname: str):
    """관리자: 테이블의 컬럼 목록 — 등록 폼의 컬럼 선택용."""
    with db_cursor() as cur:
        cols = source_registry.table_columns(cur, tname)
    if not cols:
        raise HTTPException(404, f"테이블이 없습니다: {tname}")
    return {"columns": [{"name": k, "type": v} for k, v in cols.items()]}


# ── 전처리 설정 (app_settings — 재배포 없이 변경) ─────────────

# (key, 최소, 최대) — 빈값은 기본값 복귀
_PIPELINE_LIMITS = (("doc_extract_limit", 1, 100000),
                    ("doc_concurrency", 1, 256),
                    ("doc_body_chars", 0, 200000),   # 0=전체
                    ("doc_pack_tokens", 0, 30000),
                    ("doc_no_think", 0, 1),
                    ("chunk_chars", 200, 8000),
                    ("chunk_overlap", 0, 2000))


class PipelineSettingsIn(BaseModel):
    doc_extract_limit: str = ""   # 빈값 = 기본값 복귀 (문자열로 받아 검증)
    doc_concurrency: str = ""
    doc_body_chars: str = ""
    doc_pack_tokens: str = ""     # 0=1건씩 / N=입력 N토큰 예산으로 묶음 판정
    doc_no_think: str = ""        # 1=추론(생각) 출력 끔 (기본) / 0=켬
    doc_extract_model: str = ""   # 빈값 = 대화 모델 사용
    chunk_chars: str = ""         # 청크 크기(자)
    chunk_overlap: str = ""       # 인접 청크 겹침(자)
    struct_doc_prompt: str = ""   # 문서 판정/추출 프롬프트 override (빈값=코드 기본)
    struct_pack_prompt: str = ""  # 문서 묶음 판정 프롬프트 override (빈값=코드 기본)
    entity_extract_prompt: str = ""  # 세션(UI) 엔티티 추출 프롬프트 override
    entity_judge_prompt: str = ""    # 세션(셀프플레이) 판정 프롬프트 override

    @model_validator(mode="after")
    def _ranges(self):
        for key, lo, hi in _PIPELINE_LIMITS:
            raw = getattr(self, key).strip()
            setattr(self, key, raw)
            if not raw:
                continue
            try:
                v = int(raw)
            except ValueError:
                raise ValueError(f"{key}는 정수여야 합니다")
            if not lo <= v <= hi:
                raise ValueError(f"{key}는 {lo}~{hi} 범위여야 합니다")
        # 프롬프트 override — 길이 상한 + 필수 자리표시자(누락 시 내용이 안 들어감)
        for key in ("struct_doc_prompt", "struct_pack_prompt",
                    "entity_extract_prompt", "entity_judge_prompt"):
            if len(getattr(self, key)) > 8000:
                raise ValueError(f"{key}는 8000자 이내여야 합니다")
        if self.struct_doc_prompt.strip() and "{body}" not in self.struct_doc_prompt:
            raise ValueError("문서 프롬프트에는 {body} 자리표시자가 있어야 합니다")
        if self.struct_pack_prompt.strip() and "{docs}" not in self.struct_pack_prompt:
            raise ValueError("묶음 프롬프트에는 {docs} 자리표시자가 있어야 합니다")
        for key in ("entity_extract_prompt", "entity_judge_prompt"):
            v = getattr(self, key).strip()
            if v and ("{question}" not in v or "{answer}" not in v):
                raise ValueError(f"{key}에는 {{question}}과 {{answer}} 자리표시자가 있어야 합니다")
        return self


@router.get("/admin/pipeline-settings")
def admin_pipeline_settings():
    """관리자: 전처리(문서 구조화) 운영 설정 — 효과값 반환 (DB 없으면 .env 기본값)."""
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
            "struct_doc_prompt": st.get("struct_doc_prompt") or "",
            "struct_pack_prompt": st.get("struct_pack_prompt") or "",
            "default_doc_prompt": _judge.DOC_PROMPT,
            "default_pack_prompt": _judge.PACK_PROMPT,
            "entity_extract_prompt": st.get("entity_extract_prompt") or "",
            "entity_judge_prompt": st.get("entity_judge_prompt") or "",
            "default_extract_prompt": EXTRACT_PROMPT,
            "default_judge_prompt": JUDGE_PROMPT,
            "overridden": sorted(st.keys())}


@router.post("/admin/pipeline-settings")
def admin_pipeline_settings_set(inp: PipelineSettingsIn):
    """관리자: 전처리 설정 저장 — 다음 배치 실행부터 반영 (재배포 불필요)."""
    vals = {key: getattr(inp, key) for key, _lo, _hi in _PIPELINE_LIMITS}
    vals["doc_extract_model"] = inp.doc_extract_model.strip()
    vals["struct_doc_prompt"] = inp.struct_doc_prompt.strip()
    vals["struct_pack_prompt"] = inp.struct_pack_prompt.strip()
    vals["entity_extract_prompt"] = inp.entity_extract_prompt.strip()
    vals["entity_judge_prompt"] = inp.entity_judge_prompt.strip()
    settings.set_many(vals)
    return {"ok": True, "note": "다음 전처리 배치 실행부터 반영 (빈값은 기본값 복귀)"}


# ── 적재 / 재적재 / 구조화 운영 ────────────────────────────────

_structuring = set()  # 지금 실행 중인 구조화 소스 (중복 실행 가드)
_stop_req = set()     # 구조화 중지 요청 — 스레드가 묶음/배치 경계에서 협조적으로 멈춘다


def _guard_not_structuring(sname: str | None = None):
    """구조화 진행 중이면 리셋·재적재류 차단 — 동시 실행 시 카운트가 꼬인다."""
    if sname is not None:
        if sname in _structuring:
            raise HTTPException(409, "이 소스를 구조화 중입니다 — 끝난 뒤 다시 하세요 "
                                     "(처리 현황이 멈추면 완료). 구조화 중 실행하면 카운트가 꼬입니다")
    elif _structuring:
        raise HTTPException(409, f"구조화 중인 소스가 있습니다: {sorted(_structuring)} — "
                                 "끝난 뒤 다시 하세요 (구조화 중 초기화하면 카운트가 꼬입니다)")


class ReprocessIn(BaseModel):
    mode: Literal["errors", "reset"]  # errors=실패만 재시도 | reset=소스 전체 초기화(그래프 증거 회수)


@router.post("/admin/sources/{sname}/reprocess")
def admin_source_reprocess(sname: str, inp: ReprocessIn):
    """관리자: 소스 재처리 준비.

    errors: error 상태만 미처리로 되돌림 (다음 배치가 재시도)
    reset : 소스 전체 초기화 — 이 소스의 문서가 그래프에 올린 기여(엣지 +1, 증거)를
            먼저 회수한 뒤 상태를 리셋한다. 그냥 리셋하면 재처리 때 이중 카운트되기
            때문 (재발 소급 취소와 같은 원리). 지침·모델 변경 후 재구조화용.
    """
    _guard_not_structuring(sname)
    with db_cursor() as cur:
        if inp.mode == "errors":
            cur.execute("""UPDATE corpus_docs SET graph_status = NULL, graph_note = NULL
                           WHERE source_name = :1 AND graph_status = 'error'""", [sname])
            return {"ok": True, "reset": cur.rowcount,
                    "note": "error 문서를 미처리로 — 다음 배치가 재시도"}
        n, retracted = _reset_source(cur, sname)
    return {"ok": True, "reset": n, "evidence_retracted": retracted,
            "note": "그래프 기여 회수 완료 — 다음 배치가 처음부터 재구조화 "
                    "(고아 노드는 야간 유지보수가 정리)"}


@router.post("/admin/sources/{sname}/ingest")
def admin_source_ingest(sname: str):
    """관리자: 지금 적재 — 원천 테이블 → corpus_docs 즉시 적재 + 청킹.

    야간 배치(03:10 적재·03:15 청킹)를 기다리지 않고 소스 등록 직후 테스트하려는 용도.
    이걸 돌려야 corpus_docs가 채워지고, 그 뒤 드라이런·구조화가 처리할 문서가 생긴다.
    임베딩(03:30)·문서 그래프 구조화(03:40)는 여전히 배치나 CLI로."""
    with db_cursor() as cur:
        src = _load_source(cur, sname)
        if src["source_name"] == "blog_posts" and not src["last_ingest_ts"]:
            n = ingest_sources.migrate_blog_posts(cur, src)
            cur.execute("""UPDATE source_registry SET last_ingest_ts = SYSTIMESTAMP
                           WHERE source_name = :1""", [sname])
        else:
            n = ingest_sources.ingest_source(cur, src)
    chunk_corpus.main()  # 미청킹 신규분 청킹 (멱등 — 전체 대상이나 신규분만 처리)
    return {"ok": True, "ingested": n,
            "note": f"적재 {n}건 + 청킹 완료 — 이제 드라이런·구조화 가능. "
                    "임베딩(검색 시맨틱)·그래프 구조화는 배치나 CLI로."}


@router.post("/admin/sources/{sname}/reingest")
def admin_source_reingest(sname: str):
    """관리자: 전량 재적재 — 이 소스의 corpus_docs·chunks를 지우고 워터마크를 리셋해
    원천 테이블에서 처음부터 다시 적재 + 청킹.

    지금 적재는 워터마크 이후 신규분만 — 이미 적재분은 안 다시 넣는다. 재적재는
    원천 스키마·필드 매핑을 바꿨거나 코퍼스를 깨끗이 다시 만들 때. 그래프 구조화된
    소스면 문서 유래 기여를 먼저 회수해 이중 카운트를 막는다(대화 세션 기여는 불변)."""
    _guard_not_structuring(sname)
    with db_cursor() as cur:
        src = _load_source(cur, sname)
        _, retracted = _reset_source(cur, sname)  # 그래프 기여 회수 (검색 전용이면 0)
        cur.execute("DELETE FROM corpus_chunks WHERE source_name = :1", [sname])
        cur.execute("DELETE FROM corpus_docs WHERE source_name = :1", [sname])
        cur.execute("""UPDATE source_registry SET last_ingest_ts = NULL
                       WHERE source_name = :1""", [sname])
        src["last_ingest_ts"] = None
        n = ingest_sources.ingest_source(cur, src)
    chunk_corpus.main()
    return {"ok": True, "ingested": n, "evidence_retracted": retracted,
            "note": f"전량 재적재 {n}건 + 청킹 완료 (기여 회수 {retracted}건). "
                    "임베딩·그래프 구조화는 배치나 CLI로."}


@router.post("/admin/sources/{sname}/structure")
def admin_source_structure(sname: str, run_id: str = ""):
    """관리자: 지금 구조화 — 야간 03:40 배치를 안 기다리고 미처리 문서를 즉시 판정·그래프 반영.

    즉시 버튼은 미처리가 0이 될 때까지 끝까지 처리(drain) — '또 클릭'이 필요 없게.
    (야간 배치만 회당 실행당 건수 상한.) LLM 판정이라 오래 걸려 백그라운드 스레드로
    돌린다. 진행은 처리 현황(5초 폴링)에서 실시간으로 보인다. 도메인 지정 소스만 대상."""
    if sname in _structuring:
        raise HTTPException(409, "이미 이 소스를 구조화 중입니다 — 처리 현황에서 진행 확인")
    if scheduler.is_running():
        raise HTTPException(409, "예약 구조화가 실행 중입니다 — 처리 현황에서 확인하세요")

    # 미처리 문서가 없으면 "시작했다"고만 하고 100%에 머무는 혼란 방지 — 명확히 안내.
    with db_cursor() as cur:
        if run_id:  # run 지정: 그 run이 아직 판정 안 한 문서 기준
            cur.execute("""SELECT COUNT(*) FROM corpus_docs c
                           WHERE c.source_name = :1
                             AND NOT EXISTS (SELECT 1 FROM doc_results r
                                             WHERE r.run_id = :2
                                               AND r.source_name = c.source_name
                                               AND r.src_id = c.src_id)""", [sname, run_id])
        else:
            cur.execute("""SELECT COUNT(*) FROM corpus_docs
                       WHERE source_name = :1 AND graph_status IS NULL""", [sname])
        pending = cur.fetchone()[0]
    if not pending:
        return {"ok": True, "pending": 0,
                "note": "미처리 문서가 0건입니다 — 이미 다 구조화됨. 다시 구조화하려면 먼저: "
                        "역할 매핑을 바꿨으면 [⚠ 전량 재적재], 도메인·지침만 바꿨으면 "
                        "[⚠ 초기화 재처리]로 문서를 미처리로 되돌린 뒤 [지금 구조화]."}

    def _run():
        t0 = time.time()
        try:
            r = doc_pipeline.run_for_source(sname, drain=True, run_id=run_id,
                                            should_stop=lambda: sname in _stop_req)
            events.log("batch", source="doc-structure-now", level="info", status="ok",
                       actor=sname, duration_ms=int((time.time() - t0) * 1000),
                       summary=f"지금 구조화 [{sname}]: {r}")
        except Exception as e:
            events.log("batch", source="doc-structure-now", level="error", status="fail",
                       actor=sname, duration_ms=int((time.time() - t0) * 1000),
                       summary=f"{type(e).__name__}: {str(e)[:200]}",
                       detail=traceback.format_exc())
        finally:
            _structuring.discard(sname)
            _stop_req.discard(sname)

    _stop_req.discard(sname)   # 이전 중지 요청 잔재 제거
    _structuring.add(sname)
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True,
            "note": "구조화를 시작했습니다 — 미처리가 0이 될 때까지 끝까지 처리합니다(다시 클릭 불필요). "
                    "진행은 아래 처리 현황(5초 갱신)에서 실시간으로 올라갑니다."}


@router.post("/admin/sources/{sname}/structure/stop")
def admin_source_structure_stop(sname: str):
    """관리자: 진행 중 구조화 중지 요청 — 진행 중 묶음까지 처리하고 배치 경계에서 멈춘다.
    중지해야 [초기화 재처리]가 가능해진다 (구조화 중에는 409로 막힘)."""
    if sname not in _structuring:
        return {"ok": True, "note": "이 소스는 지금 구조화 중이 아닙니다"}
    _stop_req.add(sname)
    return {"ok": True,
            "note": "중지 요청됨 — 진행 중인 묶음까지 처리하고 멈춥니다 (수십 초 내). "
                    "처리 현황에서 멈춤을 확인한 뒤 초기화·재실행하세요."}


# ── 엔티티·클러스터 버전 (이름별 라인 — core/versioning 공통 스토어) ─────────────
# 라인(name)마다 독립 버전 히스토리: '새 라인'=새 이름 v1, '버전 업'=그 라인 MAX+1,
# '기본'=활성 (name,version) 하나. create_run이 활성/선택 (name,version)을 스냅샷한다.
def _ensure_entity_versions(cur):
    versioning.ensure(cur, versioning.ENTITY_SPEC)


def _ensure_cluster_versions(cur):
    versioning.ensure(cur, versioning.CLUSTER_SPEC)


class EntityVersionIn(BaseModel):        # 버전 업 (라인 이름은 경로)
    criteria: str = ""     # 판정 지침 — 코드 스캐폴드 슬롯에 주입 (기본 편집 통로)
    descr: str = ""        # 이 엔티티가 뭔지 사람용 설명 (프롬프트 미포함)
    etypes: list[dict] = []  # 추가 추출 타입 [{key, desc}] — 설명이 분류 기준
    doc_prompt: str = ""   # 고급: 원문 전체 override (지정 시 criteria보다 우선)
    pack_prompt: str = ""
    note: str = ""

    @field_validator("etypes")
    @classmethod
    def _etypes(cls, v: list) -> list:
        """v2: role="chain" 행(배열 순서=체인 순서, 2~5칸) + tags=["solution"] ≤1 +
        attr 행. v1(role entry/solution 각 1행)도 수용 — 단 chain과 혼용은 모호라 금지."""
        out, seen, role_cnt, chain_cnt, sol_cnt = [], set(), {"entry": 0, "solution": 0}, 0, 0
        for t in (v or [])[:30]:   # 상한 30종 — 프롬프트 길이 보호
            key = str(t.get("key", "")).strip()
            if not key:
                continue
            if key in ("fits", "reason", "id", "relations", "verdict", "grounded"):
                raise ValueError(f"'{key}'는 판정 예약 필드라 키로 쓸 수 없습니다")
            if key in seen:
                raise ValueError(f"키 중복: {key}")
            seen.add(key)
            role = str(t.get("role", "")).strip()
            row = {"key": key, "label": str(t.get("label", "")).strip(),
                   "desc": str(t.get("desc", "")).strip()}
            if role == "chain":
                chain_cnt += 1
                tags = [s for s in (t.get("tags") or []) if s == "solution"]
                if tags:
                    sol_cnt += 1
                    if sol_cnt > 1:
                        raise ValueError("검증귀속(solution) 태그는 체인 칸 1개에만 가능합니다")
                row["role"], row["tags"] = "chain", tags   # tags 보존 — 스트립 금지
            elif role in ("entry", "solution"):
                role_cnt[role] += 1
                if role_cnt[role] > 1:
                    nm = "진입점" if role == "entry" else "추천단위"
                    raise ValueError(f"{nm} 역할은 스키마당 1개만 가능합니다")
                row["role"] = role
            else:
                row["role"] = "attr"
            out.append(row)
        if chain_cnt and (role_cnt["entry"] or role_cnt["solution"]):
            raise ValueError("체인(chain) 행과 구식 진입점/추천단위 행은 혼용할 수 없습니다")
        if chain_cnt == 1 or chain_cnt > 5:
            raise ValueError("계층 체인은 2~5칸이어야 합니다")
        return out


class EntityLineIn(EntityVersionIn):     # 새 라인 (이름 포함)
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("라인 이름을 입력하세요")
        return v.strip()


class ClusterVersionIn(BaseModel):
    sim_high: float = 0.92
    sim_threshold: float = 0.70
    short_name_chars: int = 12
    char_ratio: float = 0.40
    select_max: int = 8
    select_prompt: str = ""   # LLM 후보선택 프롬프트 override — 빈값=코드 기본
    note: str = ""

    @field_validator("select_prompt")
    @classmethod
    def _sel_prompt(cls, v: str) -> str:
        v = v.strip()
        if v and ("{name}" not in v or "{cands}" not in v):
            raise ValueError("후보선택 프롬프트에는 {name}과 {cands} 자리표시자가 있어야 합니다")
        return v


class ClusterLineIn(ClusterVersionIn):
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("라인 이름을 입력하세요")
        return v.strip()


def _validate_entity(doc_prompt: str, pack_prompt: str):
    if doc_prompt.strip() and "{body}" not in doc_prompt:
        raise HTTPException(400, "문서 프롬프트에는 {body} 자리표시자가 있어야 합니다")
    if pack_prompt.strip() and "{docs}" not in pack_prompt:
        raise HTTPException(400, "묶음 프롬프트에는 {docs} 자리표시자가 있어야 합니다")


def _entity_vals(inp) -> dict:
    import json
    return {"doc_prompt": inp.doc_prompt.strip() or None,
            "pack_prompt": inp.pack_prompt.strip() or None,
            "criteria": inp.criteria.strip() or None, "descr": inp.descr.strip() or None,
            "etypes": (json.dumps(inp.etypes, ensure_ascii=False) if inp.etypes else None),
            "note": inp.note.strip() or None}


def _cluster_vals(inp) -> dict:
    return {"sim_high": inp.sim_high, "sim_threshold": inp.sim_threshold,
            "short_name_chars": inp.short_name_chars, "char_ratio": inp.char_ratio,
            "select_max": inp.select_max, "select_prompt": inp.select_prompt or None,
            "note": inp.note.strip() or None}


# 엔티티 -----------------------------------------------------------------
# 엔티티 스키마는 **한 벌**이다 — 라인·버전 관리를 없앴다 (2026-08-20 결정).
# 이유: 버전 수·라인 목록이 화면 복잡도의 절반을 차지하는데, 실제로 쓰는 건 항상 최신 한 벌.
# 저장은 덮어쓰기(이력 없음). 이미 만들어진 run은 자기 스냅샷을 들고 있어 재현·비교가 유지된다.
# 판정 설정의 단위는 **도메인**이다 (2026-08-21 결정) — 도메인 하나가 지침·추출 스키마·
# 병합 설정을 한 벌로 들고 있다. entity_versions/cluster_versions의 name 컬럼(옛 '라인')을
# 도메인 이름으로 쓰고 version은 항상 1 — 이력은 run 스냅샷이 담당한다.
SEED_LINE = "기본"   # 옛 전역 한 벌 = 새 도메인이 복사해 시작하는 씨앗 (템플릿)


def _has_config(cur, domain: str) -> bool:
    """이 도메인에 판정 설정(추출 스키마) 행이 이미 있나 — 안내 문구용."""
    _ensure_entity_versions(cur)
    cur.execute("SELECT 1 FROM entity_versions WHERE name = :1 AND version = 1", [domain])
    return bool(cur.fetchone())


def _first_domain(cur, domain: str = "") -> str:
    """대상 도메인 — 지정값 우선, 없으면 등록된 첫 도메인(우선순위 순)."""
    if (domain or "").strip():
        return domain.strip()
    ensure_domain_registry(cur)
    cur.execute("SELECT name FROM domain_registry ORDER BY priority, name FETCH FIRST 1 ROWS ONLY")
    r = cur.fetchone()
    if not r:
        raise HTTPException(400, "등록된 도메인이 없습니다 — 도메인을 먼저 만드세요")
    return r[0]

_EMPTY_SCHEMA_VALS = {"doc_prompt": None, "pack_prompt": None, "criteria": None,
                      "descr": None, "etypes": None}


def _schema_row(cur, domain: str, copy_from: str = "") -> str:
    """도메인의 추출 스키마 행을 보장하고 도메인 이름을 반환 (version은 항상 1).

    없으면 만든다 — 빈 값이 아니라 **참조할 값**으로 시작한다: copy_from 도메인 → 옛 전역
    한 벌('기본') → 코드 기본. 새 도메인을 만들 때 빈 화면을 주면 사용자가 무엇을 채워야
    하는지 알 수 없다(오늘 kin_qna 빈 스냅샷이 그 사례)."""
    _ensure_entity_versions(cur)
    cur.execute("SELECT 1 FROM entity_versions WHERE name = :1 AND version = 1", [domain])
    if cur.fetchone():
        cur.execute("""UPDATE entity_versions SET is_default = 'Y', doc_prompt = NULL,
                       pack_prompt = NULL, note = NULL
                       WHERE name = :1 AND version = 1""", [domain])
        return domain
    src = None
    for cand in (copy_from, SEED_LINE):
        if not cand:
            continue
        cur.execute("""SELECT criteria, descr, etypes FROM entity_versions
                       WHERE name = :1 ORDER BY version DESC FETCH FIRST 1 ROWS ONLY""", [cand])
        src = cur.fetchone()
        if src:
            break
    if not src:   # 아무 행도 없으면 아무거나 하나 (첫 부팅 뒤 두 번째 도메인 등)
        cur.execute("""SELECT criteria, descr, etypes FROM entity_versions
                       ORDER BY version DESC FETCH FIRST 1 ROWS ONLY""")
        src = cur.fetchone()
    cur.setinputsizes(cr=oracledb.DB_TYPE_CLOB, et=oracledb.DB_TYPE_CLOB)
    cur.execute("""INSERT INTO entity_versions (name, version, criteria, descr, etypes, is_default)
                   VALUES (:nm, 1, :cr, :ds, :et, 'Y')""",
                {"nm": domain, "cr": _lob_str(src[0]) or None if src else None,
                 "ds": (src[1] if src else None) or None,
                 "et": _lob_str(src[2]) or None if src else None})
    return domain


class EntityPreviewIn(BaseModel):
    criteria: str = ""
    etypes: list[dict] = []
    fill_sample: bool = False   # true면 실제 문서 1건 + 도메인 지침으로 채운 완성본도 반환


@router.post("/admin/entity-preview")
def admin_entity_preview(inp: EntityPreviewIn):
    """스키마·지침으로 조립된 프롬프트 미리보기 — UI가 서버와 동일한 조립 결과를 표시
    (프론트에 조립 로직을 복제하지 않는다). fill_sample=true면 자리표시자를 실제
    값(도메인·지침·샘플 문서)으로 채운 완성본을 함께 준다 — '보자마자 아는' 미리보기."""
    schema = _judge.norm_schema(inp.etypes)
    d, p = _judge.build_prompts(schema, inp.criteria)
    se, sj = _judge.build_session_prompts(schema, inp.criteria)  # 대화(세션)에도 같은 스키마
    out = {"doc_prompt": d, "pack_prompt": p,
           "session_prompt": se, "session_judge_prompt": sj,
           "keys": {"entry": schema["entry"]["key"], "solution": schema["solution"]["key"],
                    "chain": [c["key"] for c in schema["chain"]],
                    "attrs": [a["key"] for a in schema["attrs"]]}}
    if inp.fill_sample:
        with db_cursor() as cur:
            cur.execute("""SELECT s.source_name, s.domain, NVL(s.content_kind, ' '),
                                  NVL(d.extract_hint, ' ')
                           FROM source_registry s
                           JOIN domain_registry d ON d.name = s.domain
                           WHERE s.enabled = 'Y' AND s.domain IS NOT NULL
                           FETCH FIRST 1 ROWS ONLY""")
            r = cur.fetchone()
            if r:
                sname, domain, kind, hint = r
                cur.execute("""SELECT src_id, NVL(title, ' '), body FROM corpus_docs
                               WHERE source_name = :1 FETCH FIRST 1 ROWS ONLY""", [sname])
                doc = cur.fetchone()
                if doc:
                    st = settings.get_all()
                    body_chars = settings.get_int(st, "doc_body_chars", config.DOC_BODY_CHARS)
                    body = doc[2].read() if hasattr(doc[2], "read") else (doc[2] or "")
                    out["doc_prompt_filled"] = _judge._fill(
                        d, domain=domain,
                        hint=(hint or "").strip() or "(지침 없음 — 도메인명 기준으로 판정)",
                        kind=(kind or "").strip(), title=(doc[1] or "").strip()[:300],
                        body=_judge._clip(body, body_chars))
                    out["sample"] = {"source": sname, "src_id": doc[0],
                                     "title": (doc[1] or "").strip()[:120]}
    return out


# 클러스터 ---------------------------------------------------------------
# 병합(클러스터) 설정도 **한 벌**이다 — 라인·버전 관리 삭제 (2026-08-21 결정).
# 근거: 버전 4개를 만들어놓고 run 11개가 전부 v1만 썼다. 판정 설정(도메인 지침·추출
# 스키마·병합)은 항상 같이 바뀌고 바꾸면 전량 재구조화가 필요해서 변경 단위가 한 벌이다.
# 이력은 run 스냅샷이 담당한다 (조합 전체를 얼려 재현·비교가 되는 유일한 지점).
def _select_tmpl() -> str:
    """후보선택 프롬프트 코드 기본값 — llm.py의 단일 정의를 그대로 보여준다."""
    from graph.graph_pipeline.llm import SELECT_PROMPT_TMPL
    return SELECT_PROMPT_TMPL


def _cluster_row(cur, domain: str, copy_from: str = "") -> str:
    """도메인의 병합(dedup) 설정 행을 보장하고 도메인 이름을 반환 (version은 항상 1).
    없으면 copy_from → 씨앗('기본') → 아무 행 순으로 복사해 만든다 (빈 값으로 시작 금지)."""
    _ensure_cluster_versions(cur)
    cur.execute("SELECT 1 FROM cluster_versions WHERE name = :1 AND version = 1", [domain])
    if cur.fetchone():
        cur.execute("""UPDATE cluster_versions SET is_default = 'Y', note = NULL
                       WHERE name = :1 AND version = 1""", [domain])
        return domain
    cols = "sim_high, sim_threshold, short_name_chars, char_ratio, select_max, select_prompt"
    src = None
    for cand in ([copy_from, SEED_LINE] if copy_from else [SEED_LINE]):
        cur.execute(f"""SELECT {cols} FROM cluster_versions
                        WHERE name = :1 ORDER BY version DESC FETCH FIRST 1 ROWS ONLY""", [cand])
        src = cur.fetchone()
        if src:
            break
    if not src:
        cur.execute(f"SELECT {cols} FROM cluster_versions ORDER BY version DESC "
                    "FETCH FIRST 1 ROWS ONLY")
        src = cur.fetchone()
    d = _merge_defaults()
    vals = ([float(src[0]) if src[0] is not None else d["sim_high"],
             float(src[1]) if src[1] is not None else d["sim_threshold"],
             int(src[2]) if src[2] is not None else d["short_name_chars"],
             float(src[3]) if src[3] is not None else d["char_ratio"],
             int(src[4]) if src[4] is not None else d["select_max"],
             _lob_str(src[5]) or None] if src
            else [d["sim_high"], d["sim_threshold"], d["short_name_chars"],
                  d["char_ratio"], d["select_max"], None])
    cur.setinputsizes(sp=oracledb.DB_TYPE_CLOB)
    cur.execute("""INSERT INTO cluster_versions
                     (name, version, sim_high, sim_threshold, short_name_chars, char_ratio,
                      select_max, select_prompt, is_default)
                   VALUES (:nm, 1, :sh, :st, :sn, :cr, :sm, :sp, 'Y')""",
                {"nm": domain, "sh": vals[0], "st": vals[1], "sn": vals[2],
                 "cr": vals[3], "sm": vals[4], "sp": vals[5]})
    return domain


def _merge_defaults() -> dict:
    from graph.graph_pipeline.merge import default_merge_cfg
    return default_merge_cfg()


class JudgeConfigIn(EntityVersionIn):
    """판정 설정 한 묶음 — 도메인 지침 + 추출 스키마·지침 + 병합 설정.

    도메인·엔티티·병합을 따로 저장하면 서로 어긋난 조합이 생긴다 (2026-08-21 결정 —
    실측된 사고가 전부 그 자유도에서 나왔다: 빈 스냅샷 run, 라우터 라인 혼재, 야간 배치의
    코드 기본 폴백). 한 번에 받고 한 트랜잭션에 쓴다. 검증은 상속받은 _etypes와
    ClusterVersionIn이 그대로 한다."""
    hint: str = ""                       # 도메인 지침 (무엇을 이 도메인 지식으로 인정할지)
    dedup: ClusterVersionIn = ClusterVersionIn()


class JudgeConfigNewIn(JudgeConfigIn):
    """새 묶음 생성 — 도메인 정체성(이름·용도·도구·우선순위)까지 한 번에 받는다.
    묶음 = 도메인 1 + 엔티티 1 + 클러스터 1. 생성 통로는 이것 하나다."""
    name: str
    scope: Literal["both", "chat", "doc"] = "doc"
    tools: str = ""
    priority: int = 100
    copy_from: str = ""    # 엔티티·병합을 복사할 원본 묶음 (빈값=코드 기본)

    @field_validator("name")
    @classmethod
    def _nm(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("묶음(도메인) 이름을 입력하세요")
        return v.strip()


def _json_list(txt: str) -> list:
    import json
    try:
        v = json.loads(txt or "[]")
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


@router.get("/admin/judge-configs")
def admin_judge_configs():
    """묶음 목록 — 도메인마다 지침·엔티티·병합 요약 + 이 묶음을 쓰는 소스."""
    with db_cursor() as cur:
        ensure_domain_registry(cur)
        cur.execute("SELECT name FROM domain_registry ORDER BY priority, name")
        for (n,) in cur.fetchall():      # 행 보장 (없으면 씨앗에서 복사)
            _schema_row(cur, n)
            _cluster_row(cur, n)
        cur.execute("""SELECT r.name, NVL(r.scope, 'both'), r.tools, r.priority, r.extract_hint,
                              e.etypes, e.criteria, c.sim_high, c.sim_threshold, c.select_max
                       FROM domain_registry r
                       JOIN entity_versions e ON e.name = r.name AND e.version = 1
                       JOIN cluster_versions c ON c.name = r.name AND c.version = 1
                       ORDER BY r.priority, r.name""")
        rows = cur.fetchall()
        cur.execute("""SELECT domain, source_name FROM source_registry
                       WHERE domain IS NOT NULL AND enabled = 'Y'""")
        used = {}
        for dom, src in cur.fetchall():
            used.setdefault(dom, []).append(src)
    out = []
    for r in rows:
        sc = _judge.norm_schema(_json_list(_lob_str(r[5])))
        out.append({"domain": r[0], "scope": r[1], "tools": r[2] or "", "priority": r[3],
                    "hint": _lob_str(r[4]), "criteria": _lob_str(r[6]),
                    "chain": [c.get("label") or c["key"] for c in sc["chain"]],
                    "attrs": [a.get("label") or a["key"] for a in sc["attrs"]],
                    "sim_high": float(r[7]) if r[7] is not None else None,
                    "sim_threshold": float(r[8]) if r[8] is not None else None,
                    "select_max": int(r[9]) if r[9] is not None else None,
                    "sources": used.get(r[0], [])})
    return {"configs": out}


@router.post("/admin/judge-config")
def admin_judge_config_new(inp: JudgeConfigNewIn):
    """새 묶음 생성 — 도메인 + 엔티티 + 병합을 한 트랜잭션에 만든다.
    엔티티·병합은 copy_from에서 복사해 시작하고, 요청에 값이 오면 그 값으로 덮는다."""
    import json
    with db_cursor() as cur:
        ensure_domain_registry(cur)
        cur.execute("SELECT COUNT(*) FROM domain_registry WHERE name = :1", [inp.name])
        if cur.fetchone()[0]:
            raise HTTPException(409, f"이미 있는 묶음입니다: {inp.name}")
        cur.setinputsizes(h=oracledb.DB_TYPE_CLOB)
        cur.execute("""INSERT INTO domain_registry (name, tools, priority, extract_hint, scope)
                       VALUES (:n, :t, :p, :h, :s)""",
                    {"n": inp.name, "t": inp.tools or None, "p": inp.priority,
                     "h": inp.hint.strip() or None, "s": inp.scope})
        _ensure_domain_versions(cur)   # registry 기준으로 v1을 시드할 수 있다
        cur.setinputsizes(h=oracledb.DB_TYPE_CLOB)
        cur.execute("""MERGE INTO domain_versions v USING dual ON (v.name = :n AND v.version = 1)
                       WHEN MATCHED THEN UPDATE SET tools = :t, priority = :p,
                            extract_hint = :h, scope = :s, is_default = 'Y'
                       WHEN NOT MATCHED THEN
                         INSERT (name, version, tools, priority, extract_hint, scope, is_default)
                         VALUES (:n, 1, :t, :p, :h, :s, 'Y')""",
                    {"n": inp.name, "t": inp.tools or None, "p": inp.priority,
                     "h": inp.hint.strip() or None, "s": inp.scope})
        _schema_row(cur, inp.name, inp.copy_from)
        _cluster_row(cur, inp.name, inp.copy_from)
        # 생성 폼이 스키마·지침을 함께 보낸다 (한 페이지에서 전부 작성) — 복사본을 덮어쓴다.
        # etypes가 비어 오면 복사본을 그대로 둔다 (지침만 채운 생성도 허용).
        cur.setinputsizes(et=oracledb.DB_TYPE_CLOB, cr=oracledb.DB_TYPE_CLOB)
        cur.execute("""UPDATE entity_versions
                       SET etypes = NVL(:et, etypes), criteria = :cr, descr = :ds
                       WHERE name = :n AND version = 1""",
                    {"et": json.dumps(inp.etypes, ensure_ascii=False) if inp.etypes else None,
                     "cr": inp.criteria.strip() or None,
                     "ds": inp.descr.strip() or None, "n": inp.name})
        cur.setinputsizes(sp=oracledb.DB_TYPE_CLOB)
        cur.execute("""UPDATE cluster_versions
                       SET sim_high = :sh, sim_threshold = :st, short_name_chars = :sn,
                           char_ratio = :cr, select_max = :sm, select_prompt = :sp
                       WHERE name = :n AND version = 1""",
                    {"sh": inp.dedup.sim_high, "st": inp.dedup.sim_threshold,
                     "sn": inp.dedup.short_name_chars, "cr": inp.dedup.char_ratio,
                     "sm": inp.dedup.select_max,
                     "sp": inp.dedup.select_prompt.strip() or None, "n": inp.name})
    src = inp.copy_from or "코드 기본"
    return {"ok": True, "domain": inp.name,
            "note": f"묶음 '{inp.name}' 생성됨 (도메인+엔티티+병합) — 엔티티·병합은 '{src}' 기준"}


@router.get("/admin/judge-config")
def admin_judge_config(domain: str = ""):
    """이 도메인의 판정 설정 한 묶음 + 코드 기본값. domain 미지정=첫 도메인."""
    with db_cursor() as cur:
        dom = _first_domain(cur, domain)
        _schema_row(cur, dom)
        _cluster_row(cur, dom)
        cur.execute("""SELECT e.criteria, e.descr, e.etypes,
                              c.sim_high, c.sim_threshold, c.short_name_chars, c.char_ratio,
                              c.select_max, c.select_prompt, r.extract_hint, NVL(r.scope, 'both')
                       FROM entity_versions e
                       JOIN cluster_versions c ON c.name = e.name AND c.version = 1
                       JOIN domain_registry r ON r.name = e.name
                       WHERE e.name = :1 AND e.version = 1""", [dom])
        r = cur.fetchone()
    d = _merge_defaults()
    return {"domain": dom, "scope": r[10],
            "hint": _lob_str(r[9]), "criteria": _lob_str(r[0]), "descr": r[1] or "",
            "etypes": _lob_str(r[2]),
            "dedup": {"sim_high": float(r[3]) if r[3] is not None else d["sim_high"],
                      "sim_threshold": float(r[4]) if r[4] is not None else d["sim_threshold"],
                      "short_name_chars": int(r[5]) if r[5] is not None else d["short_name_chars"],
                      "char_ratio": float(r[6]) if r[6] is not None else d["char_ratio"],
                      "select_max": int(r[7]) if r[7] is not None else d["select_max"],
                      "select_prompt": _lob_str(r[8])},
            "defaults": {"doc_prompt": _judge.DOC_PROMPT, "pack_prompt": _judge.PACK_PROMPT,
                         "select_prompt": _select_tmpl(),
                         "dedup": {k: v for k, v in d.items() if k != "select_prompt"}}}


@router.put("/admin/judge-config")
def admin_judge_config_put(inp: JudgeConfigIn, domain: str = ""):
    """한 묶음 덮어쓰기 — 지침·스키마·병합을 한 트랜잭션에 쓴다 (부분 저장 없음)."""
    import json
    with db_cursor() as cur:
        dom = _first_domain(cur, domain)
        _schema_row(cur, dom)
        _cluster_row(cur, dom)
        cur.setinputsizes(h=oracledb.DB_TYPE_CLOB)
        cur.execute("UPDATE domain_registry SET extract_hint = :h WHERE name = :n",
                    {"h": inp.hint.strip() or None, "n": dom})
        cur.setinputsizes(h=oracledb.DB_TYPE_CLOB)
        cur.execute("""UPDATE domain_versions SET extract_hint = :h
                       WHERE name = :n AND version = 1""",
                    {"h": inp.hint.strip() or None, "n": dom})   # 옛 리더 폴백 통로 동기화
        cur.setinputsizes(et=oracledb.DB_TYPE_CLOB, cr=oracledb.DB_TYPE_CLOB)
        cur.execute("""UPDATE entity_versions
                       SET etypes = :et, criteria = :cr, descr = :ds
                       WHERE name = :n AND version = 1""",
                    {"et": json.dumps(inp.etypes, ensure_ascii=False) if inp.etypes else None,
                     "cr": inp.criteria.strip() or None,
                     "ds": inp.descr.strip() or None, "n": dom})
        cur.setinputsizes(sp=oracledb.DB_TYPE_CLOB)
        cur.execute("""UPDATE cluster_versions
                       SET sim_high = :sh, sim_threshold = :st, short_name_chars = :sn,
                           char_ratio = :cr, select_max = :sm, select_prompt = :sp
                       WHERE name = :n AND version = 1""",
                    {"sh": inp.dedup.sim_high, "st": inp.dedup.sim_threshold,
                     "sn": inp.dedup.short_name_chars, "cr": inp.dedup.char_ratio,
                     "sm": inp.dedup.select_max,
                     "sp": inp.dedup.select_prompt.strip() or None, "n": dom})
    return {"ok": True, "domain": dom,
            "note": f"'{dom}' 판정 설정 저장됨 — 이 도메인의 새 run이 이 기준으로 판정합니다"}


# ── 조합 프리셋 (헬름차트식 — 차원별 버전 선택을 이름 있는 세트로 저장, 소스에 적용) ──
def _ensure_combo_presets(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'COMBO_PRESETS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE combo_presets (
            name           VARCHAR2(100) PRIMARY KEY,
            domain         VARCHAR2(100),
            domain_version NUMBER,
            entity_line    VARCHAR2(100), entity_version  NUMBER,
            cluster_line   VARCHAR2(100), cluster_version NUMBER,
            chat_model     VARCHAR2(200), embed_model     VARCHAR2(200),
            note           VARCHAR2(500),
            created        TIMESTAMP DEFAULT SYSTIMESTAMP,
            updated        TIMESTAMP DEFAULT SYSTIMESTAMP)""")


class PresetIn(BaseModel):
    name: str
    domain: str = ""              # 소스 기본 도메인 오버라이드 (빈값=소스 도메인)
    domain_version: int | None = None
    entity_line: str = ""
    entity_version: int | None = None
    cluster_line: str = ""
    cluster_version: int | None = None
    chat_model: str = ""
    embed_model: str = ""
    note: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("프리셋 이름을 입력하세요")
        return v.strip()


class PresetApplyIn(BaseModel):
    source_name: str
    join_version: int | None = None   # 매핑 버전 (소스별 — 빈값=기본)
    data_version: int | None = None   # 데이터 버전 (소스별 — 빈값=기본)


@router.get("/admin/presets")
def admin_presets():
    """조합 프리셋 목록 — 최근 수정 순."""
    with db_cursor() as cur:
        _ensure_combo_presets(cur)
        cur.execute("""SELECT name, domain, domain_version, entity_line, entity_version,
                              cluster_line, cluster_version, chat_model, embed_model, note,
                              TO_CHAR(updated, 'YYYY-MM-DD HH24:MI')
                       FROM combo_presets ORDER BY updated DESC""")
        rows = [{"name": r[0], "domain": r[1] or "", "domain_version": r[2],
                 "entity_line": r[3] or "", "entity_version": r[4],
                 "cluster_line": r[5] or "", "cluster_version": r[6],
                 "chat_model": r[7] or "", "embed_model": r[8] or "",
                 "note": r[9] or "", "updated": r[10]} for r in cur.fetchall()]
    return {"presets": rows}


@router.post("/admin/presets")
def admin_preset_save(inp: PresetIn):
    """조합 프리셋 저장 — 같은 이름은 수정(upsert)."""
    with db_cursor() as cur:
        _ensure_combo_presets(cur)
        if inp.domain.strip():
            _check_doc_domain(cur, inp.domain.strip())
        b = {"n": inp.name, "d": inp.domain.strip() or None, "dv": inp.domain_version,
             "el": inp.entity_line.strip() or None, "ev": inp.entity_version,
             "cl": inp.cluster_line.strip() or None, "cv": inp.cluster_version,
             "cm": inp.chat_model.strip() or None, "em": inp.embed_model.strip() or None,
             "nt": inp.note.strip() or None}
        cur.execute("""MERGE INTO combo_presets p USING dual ON (p.name = :n)
                       WHEN MATCHED THEN UPDATE SET domain = :d, domain_version = :dv,
                            entity_line = :el, entity_version = :ev,
                            cluster_line = :cl, cluster_version = :cv,
                            chat_model = :cm, embed_model = :em, note = :nt,
                            updated = SYSTIMESTAMP
                       WHEN NOT MATCHED THEN INSERT
                            (name, domain, domain_version, entity_line, entity_version,
                             cluster_line, cluster_version, chat_model, embed_model, note)
                       VALUES (:n, :d, :dv, :el, :ev, :cl, :cv, :cm, :em, :nt)""", b)
    return {"ok": True, "note": f"프리셋 '{inp.name}' 저장됨 — 파이프라인에서 불러와 run 생성"}


@router.delete("/admin/presets/{name}")
def admin_preset_delete(name: str):
    with db_cursor() as cur:
        _ensure_combo_presets(cur)
        cur.execute("DELETE FROM combo_presets WHERE name = :1", [name])
        if not cur.rowcount:
            raise HTTPException(404, f"프리셋이 없습니다: {name}")
    return {"ok": True, "note": "삭제됨 — 이미 만든 run은 스냅샷이라 영향 없음"}


@router.post("/admin/presets/{name}/apply")
def admin_preset_apply(name: str, inp: PresetApplyIn):
    """프리셋을 소스에 적용 — 프리셋의 차원 선택 + 소스별 매핑·데이터 버전으로 run 생성(비활성)."""
    from graph.doc_pipeline.runs import create_run
    with db_cursor() as cur:
        _ensure_combo_presets(cur)
        cur.execute("""SELECT domain, domain_version, entity_line, entity_version,
                              cluster_line, cluster_version, chat_model, embed_model
                       FROM combo_presets WHERE name = :1""", [name])
        p = cur.fetchone()
        if not p:
            raise HTTPException(404, f"프리셋이 없습니다: {name}")
        cur.execute("SELECT COUNT(*) FROM source_registry WHERE source_name = :1",
                    [inp.source_name])
        if not cur.fetchone()[0]:
            raise HTTPException(404, f"소스가 없습니다: {inp.source_name}")
        _ensure_entity_versions(cur)     # create_run이 참조하는 버전 테이블 보장
        _ensure_cluster_versions(cur)
        _ensure_mapping_versions(cur)
        _seed_mapping_v1(cur, inp.source_name)
        _ensure_data_versions(cur)
        rid = create_run(cur, inp.source_name,
                         domain=p[0] or "", domain_version=p[1],
                         entity_line=p[2] or "", entity_version=p[3],
                         cluster_line=p[4] or "", cluster_version=p[5],
                         chat_model=p[6] or "", embed_model=p[7] or "",
                         join_version=inp.join_version, data_version=inp.data_version)
    return {"ok": True, "run_id": rid,
            "note": f"'{name}' 조합으로 run 생성됨(비활성) — 원천 테이블·구조화 탭에서 "
                    "[선택 run으로 구조화] 후 결과 확인·활성 전환"}


# ── 매핑 버전 (원천 테이블 등록의 id·시간·필드 매핑 스냅샷 — 소스별) ──────────────
def _ensure_mapping_versions(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'MAPPING_VERSIONS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE mapping_versions (
            source_name VARCHAR2(100) NOT NULL, version NUMBER NOT NULL,
            id_column VARCHAR2(128), ts_column VARCHAR2(128), field_map CLOB,
            note VARCHAR2(500), is_default CHAR(1) DEFAULT 'N',
            created TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT mapping_versions_pk PRIMARY KEY (source_name, version))""")


def _seed_mapping_v1(cur, sname: str):
    """버전이 없으면 현재 source_registry 매핑을 v1(기본)으로 시드."""
    cur.execute("SELECT COUNT(*) FROM mapping_versions WHERE source_name = :1", [sname])
    if cur.fetchone()[0]:
        return
    cur.execute("SELECT id_column, ts_column, field_map FROM source_registry WHERE source_name = :1", [sname])
    r = cur.fetchone()
    if r:
        cur.execute("""INSERT INTO mapping_versions (source_name, version, id_column, ts_column,
                         field_map, note, is_default)
                       VALUES (:1, 1, :2, :3, :4, '초기(현재 등록)', 'Y')""",
                    [sname, r[0], r[1], _lob_str(r[2])])


class MappingVersionIn(BaseModel):
    note: str = ""


@router.get("/admin/sources/{sname}/mapping-versions")
def admin_mapping_versions(sname: str, page: int = 1):
    """소스 매핑(id·시간·필드) 버전 — 최신순, 10개 페이지. 없으면 현재 등록을 v1로 시드."""
    import json
    page = max(1, page)
    with db_cursor() as cur:
        _ensure_mapping_versions(cur)
        _seed_mapping_v1(cur, sname)
        cur.execute("SELECT COUNT(*) FROM mapping_versions WHERE source_name = :1", [sname])
        total = cur.fetchone()[0]
        cur.execute("""SELECT version, TO_CHAR(created, 'YYYY-MM-DD HH24:MI'),
                              id_column, ts_column, field_map, note, is_default
                       FROM mapping_versions WHERE source_name = :1
                       ORDER BY version DESC OFFSET :2 ROWS FETCH NEXT 10 ROWS ONLY""",
                    [sname, (page - 1) * 10])
        rows = []
        for r in cur.fetchall():
            try:
                fm = json.loads(_lob_str(r[4]) or "{}")
            except (json.JSONDecodeError, TypeError):
                fm = {}
            rows.append({"version": int(r[0]), "created": r[1], "id_column": r[2] or "",
                         "ts_column": r[3] or "", "field_map": fm, "note": r[5] or "",
                         "is_default": r[6] == "Y"})
    return {"versions": rows, "total": total, "page": page, "pages": max(1, (total + 9) // 10)}


@router.post("/admin/sources/{sname}/mapping-versions")
def admin_mapping_version_snapshot(sname: str, inp: MappingVersionIn):
    """현재 원천 테이블 등록 매핑(id·시간·필드)을 새 버전으로 스냅샷."""
    with db_cursor() as cur:
        _ensure_mapping_versions(cur)
        cur.execute("SELECT id_column, ts_column, field_map FROM source_registry WHERE source_name = :1", [sname])
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, f"소스가 없습니다: {sname}")
        cur.execute("""INSERT INTO mapping_versions (source_name, version, id_column, ts_column, field_map, note)
                       SELECT :s, NVL(MAX(version), 0) + 1, :i, :t, :f, :n
                       FROM mapping_versions WHERE source_name = :s""",
                    {"s": sname, "i": r[0], "t": r[1], "f": _lob_str(r[2]), "n": inp.note.strip() or None})
    return {"ok": True}


@router.post("/admin/sources/{sname}/mapping-versions/{v}/default")
def admin_mapping_version_default(sname: str, v: int):
    with db_cursor() as cur:
        _ensure_mapping_versions(cur)
        cur.execute("UPDATE mapping_versions SET is_default = 'N' WHERE source_name = :1", [sname])
        cur.execute("UPDATE mapping_versions SET is_default = 'Y' WHERE source_name = :1 AND version = :2", [sname, v])
    return {"ok": True}


@router.post("/admin/sources/{sname}/mapping-versions/{v}/apply")
def admin_mapping_version_apply(sname: str, v: int):
    """이 매핑 버전을 원천 등록에 적용 — source_registry의 id·시간·필드 매핑을 그 버전 값으로
    되돌리고 기본 버전으로 지정. 적재 배치가 실제로 읽는 값이 source_registry라, 버전 되돌리기
    (롤백)의 통로. 매핑이 바뀌면 코퍼스 조립이 달라지므로 [⚠ 전량 재적재]가 필요하다."""
    with db_cursor() as cur:
        _ensure_mapping_versions(cur)
        cur.execute("""SELECT id_column, ts_column, field_map FROM mapping_versions
                       WHERE source_name = :1 AND version = :2""", [sname, v])
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, f"없는 매핑 버전: {sname} v{v}")
        cur.execute("""UPDATE source_registry
                       SET id_column = :i, ts_column = :t, field_map = :f
                       WHERE source_name = :s""",
                    {"i": r[0], "t": r[1], "f": _lob_str(r[2]), "s": sname})
        if not cur.rowcount:
            raise HTTPException(404, f"소스가 없습니다: {sname}")
        cur.execute("UPDATE mapping_versions SET is_default = 'N' WHERE source_name = :1",
                    [sname])
        cur.execute("""UPDATE mapping_versions SET is_default = 'Y'
                       WHERE source_name = :1 AND version = :2""", [sname, v])
    return {"ok": True, "note": f"v{v} 매핑을 적용했습니다 — 조립 결과가 달라지므로 "
                                "[⚠ 전량 재적재]로 코퍼스를 다시 만드세요"}


# ── 데이터 신선도 버전 (소스별 적재 시점·문서수 스냅샷) ────────────────────────
def _ensure_data_versions(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DATA_VERSIONS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE data_versions (
            source_name VARCHAR2(100) NOT NULL, version NUMBER NOT NULL,
            watermark TIMESTAMP, doc_count NUMBER, note VARCHAR2(500),
            is_default CHAR(1) DEFAULT 'N', created TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT data_versions_pk PRIMARY KEY (source_name, version))""")


class DataVersionIn(BaseModel):
    note: str = ""


@router.get("/admin/sources/{sname}/data-versions")
def admin_data_versions(sname: str):
    """소스의 데이터 신선도 버전 — 적재 워터마크·문서수 스냅샷 목록."""
    with db_cursor() as cur:
        _ensure_data_versions(cur)
        cur.execute("""SELECT version, TO_CHAR(watermark, 'YYYY-MM-DD HH24:MI'), doc_count,
                              note, is_default, TO_CHAR(created, 'YYYY-MM-DD HH24:MI')
                       FROM data_versions WHERE source_name = :1 ORDER BY version DESC""", [sname])
        rows = [{"version": int(r[0]), "watermark": r[1] or "", "doc_count": int(r[2] or 0),
                 "note": r[3] or "", "is_default": r[4] == "Y", "created": r[5]}
                for r in cur.fetchall()]
    return {"versions": rows}


@router.post("/admin/sources/{sname}/data-versions")
def admin_data_version_snapshot(sname: str, inp: DataVersionIn):
    """지금 데이터 상태(적재 워터마크·문서수)를 새 버전으로 스냅샷."""
    with db_cursor() as cur:
        _ensure_data_versions(cur)
        cur.execute("SELECT last_ingest_ts FROM source_registry WHERE source_name = :1", [sname])
        r = cur.fetchone()
        wm = r[0] if r else None
        cur.execute("SELECT COUNT(*) FROM corpus_docs WHERE source_name = :1", [sname])
        cnt = cur.fetchone()[0]
        cur.execute("""INSERT INTO data_versions (source_name, version, watermark, doc_count, note)
                       SELECT :s, NVL(MAX(version), 0) + 1, :w, :c, :n
                       FROM data_versions WHERE source_name = :s""",
                    {"s": sname, "w": wm, "c": cnt, "n": inp.note.strip() or None})
    return {"ok": True, "doc_count": cnt}


@router.post("/admin/sources/{sname}/data-versions/{v}/default")
def admin_data_version_default(sname: str, v: int):
    with db_cursor() as cur:
        _ensure_data_versions(cur)
        cur.execute("UPDATE data_versions SET is_default = 'N' WHERE source_name = :1", [sname])
        cur.execute("UPDATE data_versions SET is_default = 'Y' WHERE source_name = :1 AND version = :2",
                    [sname, v])
    return {"ok": True}


# ── 예약 스케줄러 (문서 구조화 — graph/doc_pipeline/scheduler.py) ─────────────
class ScheduleIn(BaseModel):
    enabled: bool = False
    time: str = "03:40"   # HH:MM (KST)


@router.get("/admin/schedule")
def admin_schedule_get():
    """예약 상태 — 활성/시각/중지/실행중/진행 소스·건수. 처리 현황이 5초 폴링."""
    return scheduler.status()


@router.post("/admin/schedule")
def admin_schedule_set(inp: ScheduleIn):
    """예약 시각·활성 저장 (app_settings 영속)."""
    scheduler.set_schedule(inp.enabled, inp.time)
    return {"ok": True, **scheduler.status()}


@router.post("/admin/schedule/stop")
def admin_schedule_stop():
    """중지 — 실행 중이면 배치 경계에서 멈추고, 영속 플래그로 유지된다."""
    scheduler.stop()
    return {"ok": True, "note": "중지했습니다 — 실행 중이면 곧 멈춥니다. '다시 시작'까지 유지됩니다."}


@router.post("/admin/schedule/resume")
def admin_schedule_resume():
    """다시 시작 — 중지 해제. 이후 예약 시각에 다시 실행되고 수동 실행도 가능."""
    scheduler.resume()
    return {"ok": True, "note": "다시 시작했습니다 — 예약 시각에 다시 실행됩니다."}


@router.post("/admin/schedule/run-now")
def admin_schedule_run_now():
    """지금 전체 구조화 — 예약과 같은 경로(모든 대상 소스 drain). 중복·중지면 거절."""
    if not scheduler.run_all("manual"):
        raise HTTPException(409, "이미 처리 중이거나 중지 상태입니다 (중지면 '다시 시작' 먼저).")
    return {"ok": True, "note": "전체 소스 구조화를 시작했습니다 — 아래 처리 현황에서 진행 확인."}


@router.post("/admin/schedule/reset-run")
def admin_schedule_reset_run():
    """초기화 후 재처리 — 전체 문서를 미처리로 되돌리고(그래프 기여 회수) 처음부터 재구조화.
    처리 중이면 거절. 중지 상태였으면 해제하고 시작."""
    if scheduler.is_running() or _structuring:
        raise HTTPException(409, "처리 중입니다 — 먼저 중지하고 완료를 기다린 뒤 다시 시도하세요.")
    with db_cursor() as cur:
        cur.execute("SELECT source_name FROM source_registry WHERE domain IS NOT NULL")
        names = [r[0] for r in cur.fetchall()]
        per = {s: _reset_source(cur, s) for s in names}
    total = sum(n for n, _ in per.values())
    scheduler.resume()                    # 중지 상태였으면 해제
    started = scheduler.run_all("manual-reset")
    return {"ok": bool(started), "reset": total,
            "note": (f"{total}건을 미처리로 되돌리고 재처리를 시작했습니다." if started
                     else f"{total}건 초기화했으나 시작 실패 — 처리 현황을 확인하세요.")}


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
    n = cur.rowcount
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DOC_RESULTS'")
    if cur.fetchone()[0]:  # run별 결과도 리셋 (run 행은 이력으로 보존)
        cur.execute("DELETE FROM doc_results WHERE source_name = :1", [sname])
    _prune_orphans(cur)
    return n, len(refs)


def _prune_orphans(cur):
    """잔재 정리 — 증거가 하나도 안 남은 노드는 행까지 삭제 (엣지·관계·제안은 FK CASCADE).
    세션 증거가 남은 노드는 유지(대화 유래 지식 보존). 기여 0으로 내려간 빈 엣지 행도 제거 —
    초기화 후 그래프 뷰에 유령 노드·선이 남는 문제의 근본 처리."""
    cur.execute("""DELETE FROM nodes n
                   WHERE NOT EXISTS (SELECT 1 FROM node_evidence ev WHERE ev.node_id = n.id)""")
    pruned = cur.rowcount
    cur.execute("DELETE FROM edges WHERE raw_count <= 0 AND weight <= 0")
    return pruned


def _ensure_domain_versions(cur):
    """도메인 버전 테이블 + 기존 도메인의 v1 시드 (멱등).

    domain_registry는 '현재 기본 버전의 캐시' — 파이프라인(classify_domain·doc_pipeline)은
    registry만 읽으므로 코드 수정 없이 항상 기본 버전 기준으로 동작한다."""
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'DOMAIN_VERSIONS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE domain_versions (
            name         VARCHAR2(100) NOT NULL,
            version      NUMBER NOT NULL,
            tools        VARCHAR2(2000),
            priority     NUMBER DEFAULT 100,
            extract_hint VARCHAR2(2000),
            scope        VARCHAR2(10) DEFAULT 'both',
            is_default   CHAR(1) DEFAULT 'N',
            created      TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT domain_versions_pk PRIMARY KEY (name, version))""")
    # 버전이 하나도 없는 기존 도메인 → 현재 registry 값을 v1(기본)으로 시드
    cur.execute("""INSERT INTO domain_versions
                     (name, version, tools, priority, extract_hint, scope, is_default)
                   SELECT r.name, 1, r.tools, r.priority, r.extract_hint,
                          NVL(r.scope, 'both'), 'Y'
                   FROM domain_registry r
                   WHERE NOT EXISTS (SELECT 1 FROM domain_versions v WHERE v.name = r.name)""")


class ConfirmIn(BaseModel):
    confirm: str = ""  # 파괴적 삭제는 "delete" 타이핑 확인 (연결 데이터 있을 때)


@router.post("/admin/domains/{dname}/delete")
def admin_domain_delete(dname: str, inp: ConfirmIn):
    """관리자: 도메인 삭제.

    처리된 데이터가 없으면 즉시 삭제. 있으면 연결 데이터(그래프 서브트리·문서 기여)가
    전부 삭제됨을 경고하고 confirm="delete"일 때만 실행. 이 도메인을 쓰던 소스는
    domain=NULL(검색 전용)로 전환된다 — 원천 테이블 자체는 건드리지 않는다.
    """
    _guard_not_structuring()
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM domain_registry WHERE name = :1", [dname])
        if not cur.fetchone()[0]:
            raise HTTPException(404, f"도메인이 없습니다: {dname}")
        cur.execute("SELECT source_name FROM source_registry WHERE domain = :1", [dname])
        sources = [r[0] for r in cur.fetchall()]
        did, desc = _domain_subtree(cur, dname)
        if (did or sources) and inp.confirm != "delete":
            raise HTTPException(409,
                f"이 도메인과 연결된 데이터가 있습니다 — 그래프 노드 {desc}개(하위 목표·접근법 포함), "
                f"연결 소스 {len(sources)}개({', '.join(sources) or '없음'}). "
                "삭제하면 전부 사라지고 소스는 검색 전용으로 전환됩니다. "
                "계속하려면 확인란에 delete를 입력하세요.")
        for s in sources:  # 문서 기여 회수 + 문서 상태 리셋 (재등록 대비 이중 카운트 방지)
            _reset_source(cur, s)
        if did:  # 서브트리(자손 전부) 삭제 — edges·evidence·suggestions는 FK CASCADE
            cur.execute("""DELETE FROM nodes WHERE id IN (
                             SELECT dst FROM edges
                             START WITH src = :1 CONNECT BY PRIOR dst = src)
                           OR id = :1""", [did])
        cur.execute("UPDATE source_registry SET domain = NULL WHERE domain = :1", [dname])
        cur.execute("DELETE FROM domain_versions WHERE name = :1", [dname])
        cur.execute("DELETE FROM domain_registry WHERE name = :1", [dname])
    return {"ok": True, "deleted_nodes": (desc + 1) if did else 0,
            "detached_sources": sources,
            "note": ("연결 데이터까지 삭제 완료 — 소스는 검색 전용(domain 없음)으로 전환"
                     if did or sources else "연결 데이터 없음 — 도메인만 삭제")}


class SourcesDeleteIn(BaseModel):
    names: list[str]
    confirm: str = ""


@router.post("/admin/sources/delete")
def admin_sources_delete(inp: SourcesDeleteIn):
    """관리자: 선택 소스들의 파생 데이터 삭제 — 원천 테이블은 건드리지 않는다.

    삭제 대상: 이 소스로 만든 corpus_docs·청크·그래프 문서 기여(회수) + 소스 등록 자체.
    파괴적이므로 confirm="delete" 필수.
    """
    if inp.confirm != "delete":
        raise HTTPException(409,
            "선택한 소스의 청크·코퍼스·그래프 기여 등 파생 데이터가 전부 삭제됩니다 "
            "(원천 테이블은 불변). 계속하려면 확인란에 delete를 입력하세요.")
    if not inp.names:
        raise HTTPException(400, "선택된 소스가 없습니다")
    for n in inp.names:
        _guard_not_structuring(n)
    out = {}
    with db_cursor() as cur:
        for n in inp.names:
            _, retracted = _reset_source(cur, n)  # 그래프 기여 회수
            cur.execute("DELETE FROM corpus_chunks WHERE source_name = :1", [n])
            chunks = cur.rowcount
            cur.execute("DELETE FROM corpus_docs WHERE source_name = :1", [n])
            docs = cur.rowcount
            cur.execute("DELETE FROM source_registry WHERE source_name = :1", [n])
            out[n] = {"docs": docs, "chunks": chunks, "evidence_retracted": retracted}
    return {"ok": True, "deleted": out,
            "note": "파생 데이터 삭제 완료 — 원천 테이블은 그대로. 검색 인덱스는 다음 갱신 주기에 반영"}


@router.post("/admin/domains/{dname}/reset")
def admin_domain_reset(dname: str):
    """관리자: 도메인 초기화 — 이 도메인에 물린 모든 소스의 문서 구조화를 회수·리셋.
    대화 세션 기여는 건드리지 않는다 (문서 쪽만)."""
    _guard_not_structuring()
    with db_cursor() as cur:
        cur.execute("SELECT source_name FROM source_registry WHERE domain = :1", [dname])
        names = [r[0] for r in cur.fetchall()]
        if not names:
            raise HTTPException(404, f"도메인 '{dname}'에 지정된 소스가 없습니다")
        per = {s: _reset_source(cur, s) for s in names}
    return {"ok": True, "sources": {s: {"reset": n, "evidence_retracted": r}
                                    for s, (n, r) in per.items()},
            "note": "다음 배치가 처음부터 재구조화 (고아 노드는 야간 유지보수가 정리)"}


@router.post("/admin/reset-all-docs")
def admin_reset_all_docs():
    """관리자: 전체 초기화 — 도메인 지정된 모든 소스의 문서 구조화를 회수·리셋."""
    _guard_not_structuring()
    with db_cursor() as cur:
        cur.execute("SELECT source_name FROM source_registry WHERE domain IS NOT NULL")
        names = [r[0] for r in cur.fetchall()]
        per = {s: _reset_source(cur, s) for s in names}
    return {"ok": True, "sources": {s: {"reset": n, "evidence_retracted": r}
                                    for s, (n, r) in per.items()},
            "note": "다음 배치가 처음부터 재구조화 (고아 노드는 야간 유지보수가 정리)"}


# ── 구조화 실행(run) 버저닝 — B-full ──────────────────────────

def _run_compare_params(sj) -> dict:
    """run settings(JSON CLOB) → 비교 뷰용 평면 dict (한글 키 = 표의 행).
    프롬프트 전문은 안 실음('커스텀/기본' 여부만) — 비교 표는 설정 차이 파악용."""
    import json
    if hasattr(sj, "read"):
        sj = sj.read()
    try:
        d = json.loads(sj or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    p = {}
    if d.get("body_chars") is not None:
        p["본문 길이"] = str(d["body_chars"])
    if d.get("pack_tokens") is not None:
        p["묶음 토큰"] = str(d["pack_tokens"])
    p["생각 끄기"] = "켬" if d.get("no_think") else "끔"
    if d.get("lines"):   # 라우터 run — 라인별 스키마는 캔버스에서, 여기선 요약 한 줄
        p["① 추출 라인"] = ", ".join(f"{l.get('line')}·v{l.get('version')}"
                                     for l in d["lines"]) + " (라우터)"
        p["추출 프롬프트"] = "라우터(라인별)"
        sc = None
    else:
        p["추출 프롬프트"] = "원문 커스텀" if (d.get("doc_prompt") or "").strip() and not d.get("schema") \
            else ("스키마 조립" if d.get("schema") else "코드 기본")
        sc = d.get("schema")
    if isinstance(sc, dict):
        e, s = sc.get("entry") or {}, sc.get("solution") or {}
        ch = sc.get("chain") or []
        if len(ch) > 2:   # v2 계층 체인 — 2칸 초과일 때만 별도 행
            p["스키마 체인"] = " → ".join(c.get("key", "") for c in ch)
        p["스키마 진입점"] = f"{e.get('key', 'goal')} ({e.get('label') or ''})".strip()
        p["스키마 추천단위"] = f"{s.get('key', 'approach')} ({s.get('label') or ''})".strip()
        attrs = sc.get("attrs") or []
        p["스키마 속성"] = ", ".join(a.get("key", "") for a in attrs) or "(없음)"
    dd = d.get("dedup")
    if isinstance(dd, dict):
        for k, lbl in (("sim_high", "자동병합≥"), ("sim_threshold", "후보하한≥"),
                       ("short_name_chars", "짧은이름<"), ("char_ratio", "문자비율≥"),
                       ("select_max", "후보수")):
            if dd.get(k) is not None:
                p[f"클러스터 {lbl}"] = str(dd[k])
        p["클러스터 선택프롬프트"] = "커스텀" if (dd.get("select_prompt") or "").strip() else "코드 기본"
    return p


def _run_settings_display(sj) -> str:
    """run settings(JSON, 이제 CLOB)를 표에 넣을 짧은 요약으로 — 긴 엔티티 프롬프트는
    '엔티티:커스텀'으로만 표기(전문은 안 펼침)."""
    import json
    if hasattr(sj, "read"):
        sj = sj.read()
    try:
        d = json.loads(sj or "{}")
    except (json.JSONDecodeError, TypeError):
        return str(sj or "")[:80]
    parts = []
    if d.get("body_chars") is not None:
        parts.append(f"본문{d['body_chars']}")
    if d.get("pack_tokens") is not None:
        parts.append(f"묶음{d['pack_tokens']}")
    if d.get("no_think"):
        parts.append("no-think")
    if d.get("lines"):
        parts.append(f"라우터·{len(d['lines'])}라인")
    elif (d.get("doc_prompt") or "").strip() or (d.get("pack_prompt") or "").strip():
        parts.append("엔티티:커스텀")
    dd = d.get("dedup")
    if isinstance(dd, dict) and dd.get("sim_high") is not None:
        parts.append(f"클러스터 H{dd['sim_high']}/T{dd.get('sim_threshold')}")
    return " · ".join(parts)


@router.get("/admin/sources/{sname}/runs")
def admin_source_runs(sname: str):
    """관리자: 이 소스의 구조화 실행 이력 — 조합(도메인 버전·모델·설정)·시각·결과 카운트."""
    from graph.doc_pipeline.runs import ensure_runs
    with db_cursor() as cur:
        ensure_runs(cur)
        # settings가 CLOB이라 GROUP BY 불가(ORA-22848) — 카운트는 스칼라 서브쿼리로
        cur.execute("""
            SELECT r.run_id, r.domain, r.domain_version, r.chat_model, r.embed_model,
                   r.settings, r.active,
                   TO_CHAR(r.started, 'MM-DD HH24:MI'), TO_CHAR(r.finished, 'MM-DD HH24:MI'),
                   (SELECT COUNT(*) FROM doc_results d
                    WHERE d.run_id = r.run_id AND d.status = 'done'),
                   (SELECT COUNT(*) FROM doc_results d
                    WHERE d.run_id = r.run_id AND d.status = 'excluded'),
                   (SELECT COUNT(*) FROM doc_results d
                    WHERE d.run_id = r.run_id AND d.status = 'error'),
                   r.entity_version, r.cluster_version, r.join_version, r.data_version,
                   r.entity_line, r.cluster_line
            FROM doc_runs r
            WHERE r.source_name = :1
            ORDER BY r.started DESC""", [sname])
        def _run_lines(sj):   # 라우터 run의 라인 후보 [{line, version}] — settings에서
            import json as _json
            try:
                return [{"line": l.get("line"), "version": l.get("version")}
                        for l in (_json.loads(sj or "{}").get("lines") or [])]
            except (ValueError, TypeError, AttributeError):
                return []
        rows = []
        for r in cur.fetchall():
            sj = _lob_str(r[5])   # CLOB 1회 읽기 — 요약·비교 파라미터 둘 다 여기서
            rows.append({"run_id": r[0], "domain": r[1], "domain_version": r[2],
                         "chat_model": r[3], "embed_model": r[4],
                         "settings": _run_settings_display(sj),
                         "params": _run_compare_params(sj),
                         "entity_lines": _run_lines(sj),
                         "active": r[6] == "Y", "started": r[7], "finished": r[8],
                         "done": int(r[9] or 0), "excluded": int(r[10] or 0),
                         "error": int(r[11] or 0),
                         "entity_version": int(r[12]) if r[12] is not None else None,
                         "cluster_version": int(r[13]) if r[13] is not None else None,
                         "join_version": int(r[14]) if r[14] is not None else None,
                         "data_version": int(r[15]) if r[15] is not None else None,
                         "entity_line": r[16] or "", "cluster_line": r[17] or ""})
        # 라벨 (run_labels — 'active'는 시스템, 나머지는 관리자 자유)
        cur.execute("SELECT run_id, label FROM run_labels WHERE source_name = :1", [sname])
        lbl = {}
        for rid_, lb_ in cur.fetchall():
            lbl.setdefault(rid_, []).append(lb_)
        for row in rows:
            row["labels"] = sorted(lbl.get(row["run_id"], []))
        # stale 배지 (Dagster 패턴) — 활성 run의 조합이 현재 기본 버전들보다 뒤처졌는지
        from graph.doc_pipeline.runs import _default_mapping_ver, _default_data_ver
        mv = _default_mapping_ver(cur, sname)
        dv = _default_data_ver(cur, sname)
        for row in rows:
            if not row["active"]:
                continue
            stale = []
            # 판정 설정(도메인 지침·추출 스키마·병합)은 버전이 없어졌다 (2026-08-21) —
            # 값이 바뀌었는지는 run 스냅샷을 열어 비교한다. 여기서는 소스 쪽 버전만 본다.
            if mv is not None and row["join_version"] not in (None, mv):
                stale.append(f"매핑 v{row['join_version']}→v{mv}")
            if dv is not None and row["data_version"] not in (None, dv):
                stale.append(f"데이터 v{row['data_version']}→v{dv}")
            row["stale_dims"] = stale
    return {"runs": rows}


class RunCreateIn(BaseModel):
    domain_version: int | None = None  # 미지정=현재 기본 버전
    entity_line: str = ""              # 엔티티 라인 이름 — 미지정=활성 라인
    entity_version: int | None = None  # 엔티티(추출 프롬프트) 버전 — 미지정=기본
    entity_lines: list[dict] = []      # 라우터 후보 [{line, version}] — ≥2개면 라우터 run
    cluster_line: str = ""             # 클러스터 라인 이름 — 미지정=활성 라인
    cluster_version: int | None = None # 클러스터(dedup) 버전 — 미지정=기본
    join_version: int | None = None    # 테이블 조인 버전 — 미지정=기본
    data_version: int | None = None    # 데이터 신선도 버전 — 미지정=기본
    chat_model: str = ""               # 미지정=현재 전처리 모델
    embed_model: str = ""              # 미지정=기본 임베딩
    body_chars: int | None = None
    pack_tokens: int | None = None
    no_think: int | None = None
    # 클러스터(dedup) 부분 override — 미지정=현재 config 스냅샷
    sim_high: float | None = None
    sim_threshold: float | None = None
    short_name_chars: int | None = None
    char_ratio: float | None = None
    select_max: int | None = None


@router.post("/admin/sources/{sname}/runs")
def admin_source_run_create(sname: str, inp: RunCreateIn):
    """관리자: 새 조합 run 생성 (비활성) — 도메인 버전·모델·설정을 골라 버전을 만든다.
    이후 [이 run으로 구조화]→ 결과 확인 → [활성 전환]으로 반영."""
    from graph.doc_pipeline.runs import create_run
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM source_registry WHERE source_name = :1", [sname])
        if not cur.fetchone()[0]:  # 원천 테이블 실존은 불필요 — 등록 여부만 (이관 시드 소스 포함)
            raise HTTPException(404, f"소스가 없습니다: {sname}")
        _ensure_entity_versions(cur)   # 버전 테이블 보장 (create_run이 참조)
        _ensure_cluster_versions(cur)
        _ensure_mapping_versions(cur)
        _seed_mapping_v1(cur, sname)
        _ensure_data_versions(cur)
        # 라우터 후보 정리 — 순서 보존 dedup, 1개면 스칼라로 접어 기존 단일 경로 보장
        cands, seen = [], set()
        for it in inp.entity_lines or []:
            key = (str(it.get("line", "")).strip(), it.get("version"))
            if key[0] and key[1] is not None and key not in seen:
                seen.add(key)
                cands.append({"line": key[0], "version": int(key[1])})
        eline, ever = inp.entity_line, inp.entity_version
        if len(cands) == 1:
            eline, ever, cands = cands[0]["line"], cands[0]["version"], []
        try:
            rid = create_run(cur, sname, domain_version=inp.domain_version,
                             entity_line=eline, entity_version=ever,
                             entity_lines=cands or None,
                             cluster_line=inp.cluster_line, cluster_version=inp.cluster_version,
                             join_version=inp.join_version, data_version=inp.data_version,
                             chat_model=inp.chat_model, embed_model=inp.embed_model,
                             body_chars=inp.body_chars, pack_tokens=inp.pack_tokens,
                             no_think=inp.no_think,
                             dedup={"sim_high": inp.sim_high, "sim_threshold": inp.sim_threshold,
                                    "short_name_chars": inp.short_name_chars,
                                    "char_ratio": inp.char_ratio, "select_max": inp.select_max})
        except ValueError as e:   # 없는 라인 후보 등 — 조용한 누락 대신 명시 에러
            raise HTTPException(400, str(e))
    return {"ok": True, "run_id": rid,
            "note": "run 생성됨(비활성) — [이 run으로 구조화] 후 결과를 보고 활성 전환하세요"}


class RunLabelIn(BaseModel):
    label: str

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        import re as _re
        v = v.strip().lower()
        if not _re.fullmatch(r"[a-z0-9_\-.]{1,64}", v):
            raise ValueError("라벨은 영소문자·숫자·-_. 만, 64자 이내")
        if v == "active":
            raise ValueError("'active'는 시스템 라벨입니다 — [★ 활성 전환]으로만 이동")
        return v


@router.post("/admin/runs/{run_id}/labels")
def admin_run_label_set(run_id: str, inp: RunLabelIn):
    """run에 라벨 부여 — 같은 소스의 다른 run이 갖고 있으면 자동으로 옮겨온다(MLflow alias)."""
    from graph.doc_pipeline.runs import ensure_runs, set_run_label
    with db_cursor() as cur:
        ensure_runs(cur)
        try:
            prev = set_run_label(cur, run_id, inp.label)
        except ValueError as e:
            raise HTTPException(404, str(e))
    return {"ok": True, "note": (f"라벨 '{inp.label}' 부여됨 — 이전 run에서 옮겨왔습니다"
                                 if prev else f"라벨 '{inp.label}' 부여됨")}


@router.delete("/admin/runs/{run_id}/labels/{label}")
def admin_run_label_delete(run_id: str, label: str):
    from graph.doc_pipeline.runs import ensure_runs, delete_run_label
    if label == "active":
        raise HTTPException(400, "'active'는 시스템 라벨 — [★ 활성 전환]으로만 이동합니다")
    with db_cursor() as cur:
        ensure_runs(cur)
        if not delete_run_label(cur, run_id, label):
            raise HTTPException(404, f"이 run에 라벨이 없습니다: {label}")
    return {"ok": True, "note": f"라벨 '{label}' 제거됨"}


@router.get("/admin/sources/{sname}/run-docs-compare")
def admin_run_docs_compare(sname: str, runs: str, only_diff: int = 0, page: int = 1):
    """문서 단위 run 비교 — 행=문서, 열=run (Langfuse compare 패턴).
    only_diff=1이면 run 간 상태가 다른 문서만."""
    import json
    rids = [r.strip() for r in (runs or "").split(",") if r.strip()][:6]
    if len(rids) < 2:
        raise HTTPException(400, "비교할 run을 2개 이상 지정하세요")
    page = max(1, page)
    with db_cursor() as cur:
        marks = ",".join(f":r{i}" for i in range(len(rids)))
        binds = {f"r{i}": v for i, v in enumerate(rids)}
        binds["s"] = sname
        cur.execute(f"""SELECT src_id, run_id, status, note, entities
                        FROM doc_results
                        WHERE source_name = :s AND run_id IN ({marks})""", binds)
        docs = {}
        for src_id, rid, status, note, ents in cur.fetchall():
            try:
                ej = json.loads(_lob_str(ents) or "{}")
            except (json.JSONDecodeError, TypeError):
                ej = {}
            docs.setdefault(src_id, {})[rid] = {"status": status, "note": note or "",
                                                "entities": ej}
        # only_diff: run 간 상태가 하나라도 다르면 (미판정 run은 '—' 취급)
        items = sorted(docs.items())
        if only_diff:
            items = [(k, v) for k, v in items
                     if len({(v.get(r) or {}).get("status", "—") for r in rids}) > 1]
        total = len(items)
        pg = items[(page - 1) * 20:(page - 1) * 20 + 20]
        ids = [k for k, _ in pg]
        titles = {}
        if ids:
            tmarks = ",".join(f":t{i}" for i in range(len(ids)))
            tbinds = {f"t{i}": v for i, v in enumerate(ids)}
            tbinds["s"] = sname
            cur.execute(f"""SELECT src_id, NVL(title, ' ') FROM corpus_docs
                            WHERE source_name = :s AND src_id IN ({tmarks})""", tbinds)
            titles = {r[0]: (r[1] or "").strip()[:100] for r in cur.fetchall()}
    return {"runs": rids,
            "docs": [{"src_id": k, "title": titles.get(k, ""),
                      "by_run": {r: v.get(r) for r in rids}} for k, v in pg],
            "total": total, "page": page, "pages": max(1, (total + 19) // 20)}


@router.post("/admin/runs/{run_id}/activate")
def admin_run_activate(run_id: str):
    """관리자: run 활성 전환 — 기존 활성 run의 그래프 기여를 감산하고 이 run을 가산.
    경로 제안·그래프 뷰·처리 현황이 즉시 이 run 기준으로 바뀐다."""
    from graph.doc_pipeline.runs import activate_run
    with db_cursor() as cur:
        cur.execute("SELECT source_name FROM doc_runs WHERE run_id = :1", [run_id])
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, f"run이 없습니다: {run_id}")
        _guard_not_structuring(r[0])
        out = activate_run(cur, run_id)
    return {"ok": True, **out,
            "note": ("이 run이 활성 버전 — 사용자 노출(경로 제안·그래프)이 전환됨"
                     if out.get("changed") else out.get("note", ""))}


@router.delete("/admin/runs/{run_id}")
def admin_run_delete(run_id: str):
    """관리자: run 삭제 — 그 run의 결과·증거·관계를 통째로 제거 (다중 run 시대의 초기화 단위).
    비활성 run만 가능 (활성 run 기여는 활성 전환 시점에 감산/가산되므로, 먼저 다른 run을
    활성으로 전환). 비활성 run은 엣지 가중치 기여가 없어(count=False 또는 전환 시 감산됨)
    단순 삭제로 충분하다. 삭제 후 증거가 없어진 고아 노드·빈 엣지도 정리."""
    with db_cursor() as cur:
        cur.execute("SELECT source_name, active FROM doc_runs WHERE run_id = :1", [run_id])
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, f"run이 없습니다: {run_id}")
        sname, active = r
        if active == "Y":
            raise HTTPException(409, "활성 run은 삭제할 수 없습니다 — 다른 run을 활성으로 "
                                     "전환하거나 [초기화 재처리]로 기여를 회수한 뒤 삭제하세요")
        _guard_not_structuring(sname)
        cur.execute("DELETE FROM node_evidence WHERE run_id = :1", [run_id])
        ev = cur.rowcount
        cur.execute("DELETE FROM run_labels WHERE run_id = :1", [run_id])
        cur.execute("DELETE FROM doc_runs WHERE run_id = :1", [run_id])  # doc_results는 FK CASCADE
        pruned = _prune_orphans(cur)
    return {"ok": True, "evidence": ev, "orphans_pruned": pruned,
            "note": f"run 삭제됨 — 증거 {ev}건 회수, 고아 노드 {pruned}개 정리"}


@router.get("/admin/sources/{sname}/docs")
def admin_source_docs(sname: str, status: str = "", page: int = 1):
    """관리자: 문서별 구조화 결과 목록 — 반영/제외/오류/미처리 필터, 20개 페이지."""
    page = max(1, page)
    where, binds = "d.source_name = :s", {"s": sname}
    if status == "pending":
        where += " AND d.graph_status IS NULL"
    elif status in ("done", "excluded", "error"):
        where += " AND d.graph_status = :st"
        binds["st"] = status
    from graph.doc_pipeline.runs import ensure_runs
    import json
    with db_cursor() as cur:
        ensure_runs(cur)
        cur.execute(f"SELECT COUNT(*) FROM corpus_docs d WHERE {where}", binds)
        total = cur.fetchone()[0]
        cur.execute(f"""SELECT d.src_id, NVL(d.title, ' '), d.graph_status, d.graph_note,
                               dr.entities
                        FROM corpus_docs d
                        LEFT JOIN doc_runs r
                          ON r.source_name = d.source_name AND r.active = 'Y'
                        LEFT JOIN doc_results dr
                          ON dr.run_id = r.run_id AND dr.source_name = d.source_name
                         AND dr.src_id = d.src_id
                        WHERE {where}
                        ORDER BY d.src_id
                        OFFSET :off ROWS FETCH NEXT 20 ROWS ONLY""",
                    {**binds, "off": (page - 1) * 20})
        rows = []
        for r in cur.fetchall():
            try:
                ents = json.loads(_lob_str(r[4]) or "{}")
            except (json.JSONDecodeError, TypeError):
                ents = {}
            rows.append({"src_id": r[0], "title": (r[1] or "").strip()[:120],
                         "status": r[2] or "pending", "note": r[3] or "",
                         "entities": ents})
    return {"docs": rows, "total": total, "page": page,
            "pages": max(1, (total + 19) // 20)}


def _lob_str(v) -> str:
    v = v.read() if hasattr(v, "read") else v
    return "" if v is None else str(v)


@router.get("/admin/sources/{sname}/docs/{src_id}/original")
def admin_source_doc_original(sname: str, src_id: str):
    """관리자: 문서 원본 — 원천 테이블의 역할별 컬럼 값을 조립 전 그대로 반환한다.
    컬럼(역할)이 2개 이상이면 각각 구분해 볼 수 있게 리스트로 준다.
    원천 테이블이 이 DB에 없으면(마이그레이션으로 corpus_docs만 있는 경우 등) 조립된
    코퍼스 문서로 폴백한다 (from_corpus=True)."""
    import json
    with db_cursor() as cur:
        cur.execute("""SELECT table_name, id_column, field_map, NVL(url_enabled, 'Y')
                       FROM source_registry WHERE source_name = :1""", [sname])
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"소스를 찾을 수 없습니다: {sname}")
        tbl, idc, fmap_json, url_en = row
        fmap = json.loads(fmap_json)   # {역할: 컬럼명(대문자)}
        roles = list(fmap)
        cols = None
        if source_registry.table_allowed(tbl):
            try:  # 원천 테이블 직접 조회 — 역할별 컬럼 원본. 없으면(ORA-00942) 폴백.
                sel = ", ".join(ingest_sources._ident(fmap[r]) for r in roles)
                cur.execute(f"SELECT {sel} FROM {ingest_sources._ident(tbl)} "
                            f"WHERE {ingest_sources._ident(idc)} = :1", [src_id])
                vals = cur.fetchone()
                if vals is not None:
                    cols = [{"role": r, "column": fmap[r], "value": _lob_str(vals[i])[:20000]}
                            for i, r in enumerate(roles)
                            if not (r == "url" and url_en != "Y")]
            except Exception:
                cols = None  # 원천 테이블 부재·접근 불가 → 코퍼스 폴백
        from_corpus = False
        if not cols:
            from_corpus = True   # 폴백: 조립된 코퍼스 문서(항상 존재)
            cur.execute("""SELECT title, body, url FROM corpus_docs
                           WHERE source_name = :1 AND src_id = :2""", [sname, src_id])
            cd = cur.fetchone()
            if cd is None:
                raise HTTPException(404, f"문서를 찾을 수 없습니다: {src_id}")
            title, body, url = _lob_str(cd[0]), _lob_str(cd[1]), _lob_str(cd[2])
            cols = []
            if title:
                cols.append({"role": "title", "column": "제목", "value": title})
            cols.append({"role": "body", "column": "조립본(질문·답변·태그 포함)",
                         "value": body[:20000]})
            if url and url_en == "Y":
                cols.append({"role": "url", "column": "링크", "value": url})
    return {"source": sname, "src_id": src_id, "columns": cols,
            "from_corpus": from_corpus}


# ── 드라이런 ──────────────────────────────────────────────────

class DryrunIn(BaseModel):
    n: int = 3  # 판정해볼 문서 수 (최대 5 — 그래프에 반영하지 않음)


@router.post("/admin/sources/{sname}/dryrun")
def admin_source_dryrun(sname: str, inp: DryrunIn):
    """관리자: 드라이런 — 미처리 문서 N건을 판정만 해보고 결과를 보여준다.

    그래프·상태에 아무것도 쓰지 않는다. 새 소스·새 추출 지침을 튜닝할 때
    'excluded가 얼마나 나오나'를 배치 전에 확인하는 용도.
    """
    n = max(1, min(inp.n, 5))
    with db_cursor() as cur:
        cur.execute("""SELECT s.domain, NVL(d.extract_hint, ' ')
                       FROM source_registry s
                       JOIN domain_registry d ON d.name = s.domain
                       WHERE s.source_name = :1 AND s.domain IS NOT NULL""", [sname])
        r = cur.fetchone()
        if not r:
            raise HTTPException(400, "이 소스에 그래프 도메인이 지정되어 있지 않습니다")
        domain, hint = r[0], r[1]
        cur.execute("""SELECT src_id, NVL(title, ' '), NVL(kind, ' '), body
                       FROM corpus_docs
                       WHERE source_name = :1 AND graph_status IS NULL
                       FETCH FIRST :2 ROWS ONLY""", [sname, n])
        docs = [(row[0], row[1], row[2],
                 row[3].read() if hasattr(row[3], "read") else (row[3] or ""))
                for row in cur.fetchall()]
    if not docs:
        return {"domain": domain, "results": [], "note": "미처리 문서가 없습니다"}
    # 이 소스의 **도메인** 판정 설정으로 조립 — run이 실제로 쓸 프롬프트와 동일 기준
    doc_prompt, schema = "", _judge.DEFAULT_SCHEMA
    with db_cursor() as cur:
        en, ev = _schema_row(cur, domain), 1
        if en and ev is not None:
            cur.execute("""SELECT doc_prompt, criteria, etypes FROM entity_versions
                           WHERE name = :1 AND version = :2""", [en, ev])
            r2 = cur.fetchone()
            if r2:
                import json as _json
                raw, crit = _lob_str(r2[0]), _lob_str(r2[1])
                try:
                    schema = _judge.norm_schema(_json.loads(_lob_str(r2[2]) or "[]"))
                except (_json.JSONDecodeError, TypeError):
                    schema = _judge.DEFAULT_SCHEMA
                doc_prompt = raw.strip() or _judge.build_doc_prompt(schema, crit)
    # LLM 판정은 커넥션 반납 후 — 판정 1건에 수 초라 커넥션을 잡고 있지 않는다
    st = settings.get_all()
    body_chars = settings.get_int(st, "doc_body_chars", config.DOC_BODY_CHARS)
    model = (st.get("doc_extract_model") or "").strip()
    if not doc_prompt:   # 활성 버전이 비었으면 앱설정 원문 override → 코드 기본
        doc_prompt = (st.get("struct_doc_prompt") or "").strip()
    keys = [(c["key"], c.get("label") or c["key"]) for c in _judge.chain_view(schema)[0]] + \
           [(a["key"], a.get("label") or a["key"]) for a in schema["attrs"]]
    out = []
    for src_id, title, kind, body in docs:
        j = doc_pipeline.judge_doc(domain, hint, kind, title, body,
                                   model=model, body_chars=body_chars, doc_prompt=doc_prompt)
        # 속성은 배열로 올 수 있다 (키워드 여러 개) — 표시용으로 한 줄로 합친다
        extracted = {label: (" · ".join(str(x) for x in j[k] if x) if isinstance(j.get(k), list)
                             else str(j.get(k) or ""))
                     for k, label in keys if j.get(k)}
        out.append({"src_id": src_id, "title": title.strip()[:120],
                    "fits": bool(j.get("fits")), "reason": j.get("reason") or
                    j.get("_error") or "파싱 실패",
                    "extracted": extracted})
    return {"domain": domain, "results": out,
            "note": "활성 엔티티 버전의 스키마로 판정만 수행 — 그래프·상태에 반영 안 됨"}
