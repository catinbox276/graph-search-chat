"""버전 스토어 — 이름별 라인(name) + 버전(version) 공통 CRUD.

엔티티·클러스터 버전 관리가 공유한다(도메인은 registry 캐시 의미가 달라 자체 유지).
- 라인(name): 독립 버전 히스토리 한 줄. '새 라인' = 새 이름 v1, '버전 업' = 그 이름 MAX+1.
- 활성: is_default='Y' 한 행(테이블당 하나)이 create_run 스냅샷의 기준.
규약: 순수 (cur, spec, ...) 함수 — 커밋/롤백은 호출자 db_cursor 소관. table/컬럼은 코드
상수(사용자 입력 아님)라 f-string 조립이 안전하다.
"""
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_LINE = "기본"


def _lob(v):
    return v.read() if hasattr(v, "read") else v


@dataclass
class Spec:
    table: str
    content_cols: list                     # 순서 있는 컨텐츠 컬럼명
    ddl: str                               # 신규 생성 DDL (name/version PK 포함)
    seed: Optional[Callable] = None        # seed(cur, spec) — 비었을 때 v1 기본 라인
    migrate: Optional[Callable] = None     # migrate(cur, spec) — 기존 평면 테이블에 name 추가
    add_cols: Optional[dict] = None        # 나중에 늘어난 컨텐츠 컬럼 {이름: Oracle 타입} — 멱등 ALTER


def _exists(cur, table: str) -> bool:
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [table.upper()])
    return bool(cur.fetchone()[0])


def migrate_add_name(cur, spec: Spec):
    """기존 (version PK) 평면 테이블 → (name, version). 멱등. 기존 행은 '기본' 라인으로 승계."""
    cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                   WHERE table_name = :1 AND column_name = 'NAME'""", [spec.table.upper()])
    if cur.fetchone()[0]:
        return
    cur.execute(f"ALTER TABLE {spec.table} ADD (name VARCHAR2(100) DEFAULT '{DEFAULT_LINE}')")
    cur.execute(f"UPDATE {spec.table} SET name = '{DEFAULT_LINE}' WHERE name IS NULL")
    cur.execute("""SELECT constraint_name FROM user_constraints
                   WHERE table_name = :1 AND constraint_type = 'P'""", [spec.table.upper()])
    r = cur.fetchone()
    if r:
        cur.execute(f"ALTER TABLE {spec.table} DROP CONSTRAINT {r[0]}")
    cur.execute(f"ALTER TABLE {spec.table} ADD CONSTRAINT {spec.table}_pk "
                f"PRIMARY KEY (name, version)")


def ensure(cur, spec: Spec):
    if not _exists(cur, spec.table):
        cur.execute(spec.ddl)
    else:
        if spec.migrate:
            spec.migrate(cur, spec)
        for col, typ in (spec.add_cols or {}).items():   # 나중에 늘어난 컬럼 (멱등)
            cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                           WHERE table_name = :1 AND column_name = :2""",
                        [spec.table.upper(), col.upper()])
            if not cur.fetchone()[0]:
                cur.execute(f"ALTER TABLE {spec.table} ADD ({col} {typ})")
    if spec.seed:
        spec.seed(cur, spec)


def list_lines(cur, spec: Spec) -> list:
    """라인 목록 — 이름별 버전 수·최신·활성(기본) 버전."""
    cur.execute(f"""SELECT name, COUNT(*), MAX(version),
                           MAX(CASE WHEN is_default = 'Y' THEN version END)
                    FROM {spec.table} GROUP BY name ORDER BY name""")
    return [{"name": r[0], "versions": int(r[1]), "latest": int(r[2]),
             "default_version": int(r[3]) if r[3] is not None else None,
             "active": r[3] is not None} for r in cur.fetchall()]


def list_flat(cur, spec: Spec) -> list:
    """전체 (라인, 버전) 목록 — run 폼 드롭다운용. 최신 라인·버전 순."""
    cur.execute(f"""SELECT name, version, is_default, note
                    FROM {spec.table} ORDER BY name, version DESC""")
    return [{"name": r[0], "version": int(r[1]), "is_default": r[2] == "Y",
             "note": r[3] or ""} for r in cur.fetchall()]


def list_versions(cur, spec: Spec, name: str, page: int = 1, per: int = 10) -> dict:
    """한 라인의 버전 목록 — 최신순 페이지네이션."""
    page = max(1, page)
    cur.execute(f"SELECT COUNT(*) FROM {spec.table} WHERE name = :1", [name])
    total = cur.fetchone()[0]
    cols = ", ".join(spec.content_cols)
    cur.execute(f"""SELECT version, TO_CHAR(created, 'YYYY-MM-DD HH24:MI'), note, is_default, {cols}
                    FROM {spec.table} WHERE name = :1 ORDER BY version DESC
                    OFFSET :2 ROWS FETCH NEXT {per} ROWS ONLY""", [name, (page - 1) * per])
    rows = []
    for r in cur.fetchall():
        d = {"version": int(r[0]), "created": r[1], "note": r[2] or "", "is_default": r[3] == "Y"}
        for i, c in enumerate(spec.content_cols):
            d[c] = _lob(r[4 + i])
        rows.append(d)
    return {"versions": rows, "total": total, "page": page, "pages": max(1, (total + per - 1) // per)}


def _insert(cur, spec: Spec, name: str, version_sql: str, vals: dict, binds: dict):
    cols = ", ".join(spec.content_cols)
    b = {c: vals.get(c) for c in spec.content_cols}
    b.update({"nm": name, "nt": (vals.get("note") or None)})
    b.update(binds)
    cur.execute(f"""INSERT INTO {spec.table} (name, version, note, {cols})
                    {version_sql}""", b)


def new_line(cur, spec: Spec, name: str, vals: dict) -> int:
    """새 라인 = 새 이름 v1 (기본으로 지정하지 않음)."""
    cur.execute(f"SELECT COUNT(*) FROM {spec.table} WHERE name = :1", [name])
    if cur.fetchone()[0]:
        raise ValueError(f"이미 있는 라인: {name}")
    marks = ", ".join(f":{c}" for c in spec.content_cols)
    _insert(cur, spec, name, f"VALUES (:nm, 1, :nt, {marks})", vals, {})
    return 1


def version_up(cur, spec: Spec, name: str, vals: dict) -> int:
    """버전 업 = 기존 라인에 MAX+1 (기본으로 지정하지 않음)."""
    cur.execute(f"SELECT COUNT(*) FROM {spec.table} WHERE name = :1", [name])
    if not cur.fetchone()[0]:
        raise KeyError(f"없는 라인: {name}")
    marks = ", ".join(f":{c}" for c in spec.content_cols)
    _insert(cur, spec, name,
            f"SELECT :nm, NVL(MAX(version), 0) + 1, :nt, {marks} FROM {spec.table} WHERE name = :nm",
            vals, {})
    cur.execute(f"SELECT MAX(version) FROM {spec.table} WHERE name = :1", [name])
    return int(cur.fetchone()[0])


def set_default(cur, spec: Spec, name: str, version: int):
    """활성 (name, version) 지정 — 테이블당 하나. create_run이 이 기준으로 스냅샷."""
    cur.execute(f"SELECT 1 FROM {spec.table} WHERE name = :1 AND version = :2", [name, version])
    if not cur.fetchone():
        raise KeyError(f"없는 버전: {name} v{version}")
    cur.execute(f"UPDATE {spec.table} SET is_default = 'N'")
    cur.execute(f"UPDATE {spec.table} SET is_default = 'Y' WHERE name = :1 AND version = :2",
                [name, version])


def active(cur, table: str):
    """활성 (name, version) — is_default='Y' 한 행. 없으면 (None, None).
    runs.py가 spec 없이 부를 수 있게 table 문자열만 받는다."""
    try:
        cur.execute(f"SELECT name, version FROM {table} WHERE is_default = 'Y'")
        r = cur.fetchone()
        return (r[0], int(r[1])) if r else (None, None)
    except Exception:
        return (None, None)


# ── 차원별 스펙 ──────────────────────────────────────────────

def _seed_entity(cur, spec: Spec):
    from core import settings
    cur.execute(f"SELECT COUNT(*) FROM {spec.table}")
    if cur.fetchone()[0]:
        return
    st = settings.get_all()
    cur.execute("""INSERT INTO entity_versions (name, version, doc_prompt, pack_prompt, note, is_default)
                   VALUES (:1, 1, :d, :p, '초기(현재 설정)', 'Y')""",
                {"1": DEFAULT_LINE, "d": (st.get("struct_doc_prompt") or None),
                 "p": (st.get("struct_pack_prompt") or None)})


def _seed_cluster(cur, spec: Spec):
    from core import config
    cur.execute(f"SELECT COUNT(*) FROM {spec.table}")
    if cur.fetchone()[0]:
        return
    cur.execute("""INSERT INTO cluster_versions (name, version, sim_high, sim_threshold,
                     short_name_chars, char_ratio, select_max, note, is_default)
                   VALUES (:1, 1, :sh, :st, :sn, :cr, :sm, '초기(config 기본)', 'Y')""",
                {"1": DEFAULT_LINE, "sh": config.DEDUP_SIM_HIGH, "st": config.DEDUP_SIM_THRESHOLD,
                 "sn": config.DEDUP_SHORT_NAME_CHARS, "cr": config.DEDUP_CHAR_RATIO,
                 "sm": config.DEDUP_SELECT_MAX})


ENTITY_SPEC = Spec(
    table="entity_versions",
    content_cols=["doc_prompt", "pack_prompt", "criteria", "descr", "etypes"],
    # criteria: 판정 지침 — 코드 스캐폴드의 지정 슬롯에 주입 (관리자 편집의 기본 통로)
    # descr: 이 엔티티(라인)가 뭔지 사람용 설명 — 프롬프트에 안 들어감
    # etypes: 추가 추출 엔티티 타입 정의 JSON [{"key","desc"}] — 설명이 분류 기준 (Graphiti 방식)
    ddl="""CREATE TABLE entity_versions (
        name VARCHAR2(100) NOT NULL, version NUMBER NOT NULL,
        doc_prompt CLOB, pack_prompt CLOB, criteria CLOB, descr VARCHAR2(1000),
        etypes CLOB, note VARCHAR2(500),
        is_default CHAR(1) DEFAULT 'N', created TIMESTAMP DEFAULT SYSTIMESTAMP,
        CONSTRAINT entity_versions_pk PRIMARY KEY (name, version))""",
    seed=_seed_entity, migrate=migrate_add_name,
    add_cols={"criteria": "CLOB", "descr": "VARCHAR2(1000)", "etypes": "CLOB"})

CLUSTER_SPEC = Spec(
    table="cluster_versions",
    content_cols=["sim_high", "sim_threshold", "short_name_chars", "char_ratio", "select_max",
                  "select_prompt"],   # LLM 후보선택 프롬프트 override (NULL=코드 기본)
    ddl="""CREATE TABLE cluster_versions (
        name VARCHAR2(100) NOT NULL, version NUMBER NOT NULL,
        sim_high NUMBER, sim_threshold NUMBER, short_name_chars NUMBER,
        char_ratio NUMBER, select_max NUMBER, select_prompt CLOB, note VARCHAR2(500),
        is_default CHAR(1) DEFAULT 'N', created TIMESTAMP DEFAULT SYSTIMESTAMP,
        CONSTRAINT cluster_versions_pk PRIMARY KEY (name, version))""",
    seed=_seed_cluster, migrate=migrate_add_name,
    add_cols={"select_prompt": "CLOB"})
