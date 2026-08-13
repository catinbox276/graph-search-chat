"""MCP 서버 레지스트리 — mcp_registry 테이블 (ORM, 관리 페이지 /admin에서 등록).

- 주소(url)를 등록하면 에이전트가 그 MCP의 도구들을 자동 발견해 조립한다.
- transport: streamable_http / sse / stdio(command 필요).
- 도구별 활성/비활성은 agent_disabled_tools(app_settings)가 담당 — 여기는 서버 단위.
- 테이블 생성은 db.init_schema(). 구버전 마이그레이션·env 시드는 _ensure_legacy가
  프로세스당 1회 (raw SQL — 제약 교체·MERGE는 ORM 표현 밖).
"""
from sqlalchemy import text

from core import config, db
from core.models import McpRegistry

_ensured = False


def _ensure_legacy():
    """구버전 제약 마이그레이션 + 구 DataHub stdio 시드 정리 + .env 기본 시드 (멱등)."""
    global _ensured
    if _ensured:
        return
    with db.engine().begin() as con:
        # 기존 CHECK 제약에 'rest' 추가 — 구버전 무명 제약을 명명 제약으로 교체
        n = con.execute(text("""SELECT COUNT(*) FROM user_constraints
                                WHERE table_name = 'MCP_REGISTRY'
                                AND constraint_name = 'MCP_TRANSPORT_CK'""")).scalar()
        if not n:
            rows = con.execute(text("""SELECT constraint_name, search_condition
                                       FROM user_constraints
                                       WHERE table_name = 'MCP_REGISTRY'
                                       AND constraint_type = 'C'""")).fetchall()
            for name, cond in rows:
                cond = (cond or "").lower()
                if "transport" in cond and "not null" not in cond:
                    con.execute(text(f'ALTER TABLE mcp_registry DROP CONSTRAINT "{name}"'))
            con.execute(text("""ALTER TABLE mcp_registry ADD CONSTRAINT mcp_transport_ck
                                CHECK (transport IN ('streamable_http', 'sse', 'stdio', 'rest'))"""))
        # 구버전 이미지가 시드했던 DataHub stdio 행 자동 정리 (원형 그대로일 때만)
        con.execute(text("""DELETE FROM mcp_registry
                            WHERE name = 'datahub' AND transport = 'stdio'
                            AND NVL(command, ' ') = 'mcp-server-datahub'"""))
        # .env 기본 도구 서버 시드 — 없을 때만 삽입 (관리 페이지 수정·비활성은 보존)
        if config.MCP_DEFAULT_URL:
            tr = config.MCP_DEFAULT_TRANSPORT
            if tr not in ("streamable_http", "sse"):
                tr = "streamable_http"
            con.execute(text("""MERGE INTO mcp_registry m USING dual ON (m.name = :n)
                                WHEN NOT MATCHED THEN INSERT (name, transport, url)
                                VALUES (:n, :t, :u)"""),
                        {"n": config.MCP_DEFAULT_NAME, "t": tr,
                         "u": config.MCP_DEFAULT_URL})
    _ensured = True


def list_servers(enabled_only: bool = False) -> list:
    _ensure_legacy()
    with db.session() as s:
        q = s.query(McpRegistry)
        if enabled_only:
            q = q.filter_by(enabled="Y")
        return [{"name": r.name, "transport": r.transport, "url": r.url or "",
                 "command": r.command or "", "enabled": r.enabled == "Y"}
                for r in q.order_by(McpRegistry.name).all()]


def upsert(name: str, transport: str, url: str = "", command: str = "",
           enabled: bool = True):
    if transport not in ("streamable_http", "sse", "stdio"):
        raise ValueError(f"transport는 streamable_http/sse/stdio 중 하나: {transport}")
    if transport in ("streamable_http", "sse", "rest"):
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("http 계열/rest transport는 http(s):// 주소가 필요합니다")
    elif not command.strip():
        raise ValueError("stdio transport는 command(실행 파일)가 필요합니다")
    _ensure_legacy()
    with db.session() as s:
        row = s.get(McpRegistry, name.strip())
        if row:
            row.transport, row.url = transport, url.strip() or None
            row.command = command.strip() or None
            row.enabled = "Y" if enabled else "N"
        else:
            s.add(McpRegistry(name=name.strip(), transport=transport,
                              url=url.strip() or None,
                              command=command.strip() or None,
                              enabled="Y" if enabled else "N"))
