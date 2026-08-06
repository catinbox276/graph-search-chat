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


SEED = ("datahub", "stdio", "", "mcp-server-datahub")  # command는 PATH에서 해석


def ensure(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'MCP_REGISTRY'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE mcp_registry (
            name      VARCHAR2(100) PRIMARY KEY,
            transport VARCHAR2(20) DEFAULT 'streamable_http'
                      CHECK (transport IN ('streamable_http', 'sse', 'stdio')),
            url       VARCHAR2(500),               -- http 계열 전용
            command   VARCHAR2(500),               -- stdio 전용 (실행 파일)
            enabled   CHAR(1) DEFAULT 'Y',
            created   TIMESTAMP DEFAULT SYSTIMESTAMP)""")
        cur.execute("""INSERT INTO mcp_registry (name, transport, url, command)
                       VALUES (:1, :2, :3, :4)""", list(SEED))


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
    if transport not in ("streamable_http", "sse", "stdio"):
        raise ValueError(f"transport는 streamable_http/sse/stdio 중 하나: {transport}")
    if transport in ("streamable_http", "sse"):
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("http 계열 transport는 http(s):// 주소가 필요합니다")
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
