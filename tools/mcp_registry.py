"""MCP 서버 레지스트리 — Oracle mcp_registry 테이블 (관리 페이지 /admin에서 등록).

- 주소(url)를 등록하면 에이전트가 그 MCP의 도구들을 자동 발견해 조립한다.
- transport: streamable_http(사내 HTTP MCP 기본) / sse / stdio(command 필요).
- 도구별 활성/비활성은 agent_disabled_tools(app_settings)가 담당 — 여기는 서버 단위.
- DataHub 공식 MCP(stdio)가 시드 1호 — 기존 하드코딩이 레지스트리로 옮겨진 것.
"""
import oracledb

from tools import config

_pool = None


def _con():
    global _pool
    if _pool is None:
        _pool = oracledb.create_pool(user=config.ORACLE_USER,
                                     password=config.ORACLE_PASSWORD,
                                     dsn=config.ORACLE_DSN,
                                     min=config.ORACLE_POOL_MIN,
                                     max=config.ORACLE_POOL_MAX,
                                     increment=config.ORACLE_POOL_INCREMENT)
    return _pool.acquire()


def ensure(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'MCP_REGISTRY'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE mcp_registry (
            name      VARCHAR2(100) PRIMARY KEY,
            transport VARCHAR2(20) DEFAULT 'streamable_http',
            url       VARCHAR2(500),               -- http 계열·rest 전용
            command   VARCHAR2(500),               -- stdio 전용 (실행 파일)
            enabled   CHAR(1) DEFAULT 'Y',
            created   TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT mcp_transport_ck CHECK
              (transport IN ('streamable_http', 'sse', 'stdio', 'rest')))""")
        # 시드 없음 — 도구 서버는 관리 페이지 또는 MCP_DEFAULT_URL로 등록
        # (구 DataHub stdio 시드는 폐기 — 사내는 REST 어댑터로 연동)
    else:
        _migrate_rest_transport(cur)
    # .env 기본 MCP 시드 — 없을 때만 삽입 (관리 페이지에서의 수정·비활성은 보존)
    if config.MCP_DEFAULT_URL:
        cur.execute("""MERGE INTO mcp_registry m USING dual ON (m.name = :n)
                       WHEN NOT MATCHED THEN INSERT (name, transport, url)
                       VALUES (:n, 'streamable_http', :u)""",
                    {"n": config.MCP_DEFAULT_NAME, "u": config.MCP_DEFAULT_URL})


def _migrate_rest_transport(cur):
    """기존 CHECK 제약에 'rest' 추가 (멱등) — 구버전 무명 제약을 명명 제약으로 교체."""
    cur.execute("""SELECT COUNT(*) FROM user_constraints
                   WHERE table_name = 'MCP_REGISTRY'
                   AND constraint_name = 'MCP_TRANSPORT_CK'""")
    if cur.fetchone()[0]:
        return
    cur.execute("""SELECT constraint_name, search_condition FROM user_constraints
                   WHERE table_name = 'MCP_REGISTRY' AND constraint_type = 'C'""")
    for name, cond in cur.fetchall():
        cond = (cond or "").lower()
        if "transport" in cond and "not null" not in cond:
            cur.execute(f'ALTER TABLE mcp_registry DROP CONSTRAINT "{name}"')
    cur.execute("""ALTER TABLE mcp_registry ADD CONSTRAINT mcp_transport_ck CHECK
                   (transport IN ('streamable_http', 'sse', 'stdio', 'rest'))""")


def list_servers(enabled_only: bool = False) -> list:
    with _con() as con:
        cur = con.cursor()
        ensure(cur)
        con.commit()
        q = "SELECT name, transport, url, command, enabled FROM mcp_registry"
        if enabled_only:
            q += " WHERE enabled = 'Y'"
        cur.execute(q + " ORDER BY name")
        return [{"name": r[0], "transport": r[1], "url": r[2] or "",
                 "command": r[3] or "", "enabled": r[4] == "Y"}
                for r in cur.fetchall()]


def upsert(name: str, transport: str, url: str = "", command: str = "",
           enabled: bool = True):
    if transport not in ("streamable_http", "sse", "stdio", "rest"):
        raise ValueError(f"transport는 streamable_http/sse/stdio/rest 중 하나: {transport}")
    if transport in ("streamable_http", "sse", "rest"):
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("http 계열/rest transport는 http(s):// 주소가 필요합니다")
    elif not command.strip():
        raise ValueError("stdio transport는 command(실행 파일)가 필요합니다")
    with _con() as con:
        cur = con.cursor()
        ensure(cur)
        cur.execute("""MERGE INTO mcp_registry m USING dual ON (m.name = :n)
                       WHEN MATCHED THEN UPDATE SET transport = :t, url = :u,
                            command = :c, enabled = :e
                       WHEN NOT MATCHED THEN INSERT (name, transport, url, command, enabled)
                       VALUES (:n, :t, :u, :c, :e)""",
                    {"n": name.strip(), "t": transport, "u": url.strip() or None,
                     "c": command.strip() or None, "e": "Y" if enabled else "N"})
        con.commit()
