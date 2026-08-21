"""DB 스냅샷 복원 — core.db_dump가 만든 JSONL을 빈 DB에 되살린다.

전제: 스키마는 이 스크립트가 먼저 보장한다(ORM create_all + 각 모듈 ensure) — 새로 뜬
Oracle에 앱을 한 번도 안 띄웠어도 동작한다. 복원은 FK 부모부터(TABLES 순서),
삭제는 그 역순.

안전 규칙:
- 기본 모드는 **빈 테이블만** 복원 (기존 데이터가 있으면 그 테이블은 건너뛰고 보고).
  운영 중 DB에 실수로 덮어쓰는 사고를 구조적으로 막는다.
- `--replace`는 대상 테이블을 지우고 복원 — 확인 문구 입력 필요.
- Identity 컬럼(suggestions.id 등)은 값을 넣지 않고 재생성시킨다 (Oracle GENERATED
  ALWAYS는 명시 삽입 불가. 이 id는 외부 참조가 없다).
- 덤프에 없는 컬럼·현재 스키마에 없는 컬럼은 교집합으로 무시 — 스키마가 앞뒤로 달라도 복원된다.

사용:
    PYTHONPATH=src python -m core.db_restore <덤프디렉터리> [--dry-run|--replace] [--only t1,t2]
    PYTHONPATH=src python -m core.db_restore --selfcheck      # DB 없이 순수 로직 점검

파드로 옮겨 복원하는 흐름:
    tar xzf dbdump-2026-08-20.tgz                 # → dbdump/
    kubectl cp dbdump <ns>/<pod>:/tmp/dbdump
    kubectl exec -n <ns> <pod> -- sh -c 'cd /srv && PYTHONPATH=src python -m core.db_restore /tmp/dbdump --dry-run'
"""
import datetime
import json
import os
import sys

import oracledb

from core import config

# FK 부모 → 자식 순서. db_dump도 이 목록을 그대로 쓴다 (덤프·복원 대상의 단일 정의).
TABLES = [
    # 설정·버전 (손으로 복원 불가 — 최우선)
    "DOMAIN_REGISTRY", "DOMAIN_VERSIONS", "SOURCE_REGISTRY",
    "MAPPING_VERSIONS", "DATA_VERSIONS",
    "ENTITY_VERSIONS", "CLUSTER_VERSIONS", "COMBO_PRESETS",
    "MODEL_REGISTRY", "MCP_REGISTRY", "APP_SETTINGS", "APP_USERS",
    # 코퍼스 (원천 테이블이 사라진 소스는 재적재 불가)
    "CORPUS_DOCS",
    # 대화·run 이력
    "SESSIONS", "DOC_RUNS", "DOC_RESULTS", "RUN_LABELS",
    # 그래프 (nodes가 부모 — edges·증거·관계·제안이 참조)
    "NODES", "EDGES", "NODE_EVIDENCE", "SUGGESTIONS",
]

_TS_TYPES = ("TIMESTAMP", "DATE")


def ensure_schema() -> None:
    """복원 대상 테이블 전부 생성 (멱등) — ORM create_all + 각 모듈의 ensure.
    admin 라우터가 지연 생성하는 테이블(매핑·데이터·도메인 버전·프리셋)도 여기서 만든다."""
    from core import db, versioning
    db.init_schema()                    # ORM 전 테이블 + model_registry 컬럼 보강
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                           dsn=config.ORACLE_DSN)
    cur = con.cursor()
    from graph.graph_pipeline import ddl
    ddl(cur)                            # 그래프 DDL + role_tag 마이그레이션 + ensure_runs
    versioning.ensure(cur, versioning.ENTITY_SPEC)
    versioning.ensure(cur, versioning.CLUSTER_SPEC)
    from web.routers import admin_sources as A   # 지연 생성 테이블들 (라우터 소유)
    for fn in (A._ensure_mapping_versions, A._ensure_data_versions,
               A._ensure_domain_versions, A._ensure_combo_presets):
        fn(cur)
    con.commit()
    con.close()


def _meta(cur, table: str) -> tuple:
    """(컬럼→데이터타입, identity 컬럼 집합) — 없는 테이블이면 ({}, set())."""
    cur.execute("""SELECT column_name, data_type FROM user_tab_columns
                   WHERE table_name = :1""", [table])
    cols = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("""SELECT column_name FROM user_tab_identity_cols
                   WHERE table_name = :1""", [table])
    return cols, {r[0] for r in cur.fetchall()}


def coerce(val, data_type: str):
    """JSON 값 → Oracle 바인드 값. 시각 문자열은 datetime으로, 그 외는 그대로."""
    if val is None:
        return None
    if any(data_type.startswith(t) for t in _TS_TYPES) and isinstance(val, str):
        try:
            return datetime.datetime.fromisoformat(val)
        except ValueError:
            return None            # 형식 불명 시각은 버린다 (기본값·NULL 허용 컬럼)
    return val


def restore(dump_dir: str, replace: bool = False, dry_run: bool = False,
            only: set | None = None) -> dict:
    """덤프 디렉터리를 복원 — {테이블: 결과 문자열} 반환."""
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                           dsn=config.ORACLE_DSN)
    cur = con.cursor()
    targets = [t for t in TABLES if not only or t in only]
    if replace and not dry_run:        # 자식 → 부모 역순 삭제 (FK 위반 방지)
        for t in reversed(targets):
            if os.path.exists(os.path.join(dump_dir, t.lower() + ".jsonl")):
                cur.execute(f"DELETE FROM {t}")        # noqa: S608 — 상수 목록
        con.commit()
    out = {}
    for t in targets:
        path = os.path.join(dump_dir, t.lower() + ".jsonl")
        if not os.path.exists(path):
            out[t] = "덤프 없음"
            continue
        cols, identity = _meta(cur, t)
        if not cols:
            out[t] = "테이블 없음 (ensure_schema 먼저)"
            continue
        cur.execute(f"SELECT COUNT(*) FROM {t}")       # noqa: S608 — 상수 목록
        have = cur.fetchone()[0]
        rows = [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]
        if have and not replace:
            out[t] = f"건너뜀 (기존 {have}행 — 덤프 {len(rows)}행, --replace로 교체)"
            continue
        if not rows:
            out[t] = "0행"
            continue
        # 덤프 ∩ 현재 스키마 − identity — 스키마가 달라도 되는 만큼만 복원
        names = [c for c in rows[0] if c in cols and c not in identity]
        if not names:
            out[t] = "겹치는 컬럼 없음"
            continue
        if dry_run:
            out[t] = f"복원 예정 {len(rows)}행 · 컬럼 {len(names)}개" + \
                     (f" (기존 {have}행 삭제 후)" if have and replace else "")
            continue
        sql = (f"INSERT INTO {t} ({', '.join(names)}) "      # noqa: S608 — 컬럼도 DB 유래
               f"VALUES ({', '.join(':' + c for c in names)})")
        clob = {c: oracledb.DB_TYPE_CLOB for c in names if cols[c] == "CLOB"}
        n = 0
        for i in range(0, len(rows), 500):
            chunk = [{c: coerce(r.get(c), cols[c]) for c in names} for r in rows[i:i + 500]]
            if clob:
                cur.setinputsizes(**clob)   # 4000자 넘는 CLOB은 타입 지정이 필요
            cur.executemany(sql, chunk)
            n += len(chunk)
        con.commit()
        out[t] = f"복원 {n}행"
    con.close()
    return out


def _selfcheck() -> None:
    """DB 없이 순수 로직 점검 — 타입 변환과 목록 정합성."""
    assert coerce(None, "NUMBER") is None
    assert coerce("2026-08-20T10:05:00", "TIMESTAMP(6)") == \
        datetime.datetime(2026, 8, 20, 10, 5)
    assert coerce("깨진시각", "DATE") is None
    assert coerce("본문", "CLOB") == "본문" and coerce(3.0, "NUMBER") == 3.0
    # FK 부모가 자식보다 먼저 오는지 (대표 쌍)
    idx = {t: i for i, t in enumerate(TABLES)}
    for parent, child in (("NODES", "EDGES"), ("NODES", "NODE_EVIDENCE"),
                          ("NODES", "SUGGESTIONS"),
                          ("DOMAIN_REGISTRY", "SOURCE_REGISTRY"),
                          ("SOURCE_REGISTRY", "CORPUS_DOCS"),
                          ("DOC_RUNS", "DOC_RESULTS"), ("DOC_RUNS", "RUN_LABELS")):
        assert idx[parent] < idx[child], f"{parent}가 {child}보다 뒤에 있다"
    assert len(TABLES) == len(set(TABLES))
    print("selfcheck OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selfcheck" in args:
        _selfcheck()
        raise SystemExit(0)
    if not args or args[0].startswith("--"):
        print(__doc__)
        raise SystemExit(2)
    dump_dir, dry, rep = args[0], "--dry-run" in args, "--replace" in args
    only = None
    if "--only" in args:
        only = {t.strip().upper() for t in args[args.index("--only") + 1].split(",")}
    if rep and not dry:
        print(f"⚠ --replace: {dump_dir}에 있는 테이블의 기존 행을 전부 삭제하고 복원합니다.")
        if input("계속하려면 'replace' 입력: ").strip() != "replace":
            raise SystemExit("취소됨")
    if not dry:
        print("스키마 보장 중…", flush=True)
        ensure_schema()
    res = restore(dump_dir, replace=rep, dry_run=dry, only=only)
    print(json.dumps(res, ensure_ascii=False, indent=1))
