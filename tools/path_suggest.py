"""그래프 기반 방향 제안 — design.md ⑤단계.

- 진입점: 사용자 문제를 임베딩해 2층(목표) 노드와 브루트포스 코사인 (노드 수십 개 규모)
- 유사 목표에서 3층(접근법)으로 가중치 내림차순 경로 제시
- 실패 표식 접근법은 이유와 함께 경고 (하드 차단 금지 — design.md §4)
- 노출 기록: suggestions 테이블 (추후 노출 대비 채택률 가중치 보정용)
"""
import json
import re

import numpy as np
import oracledb
from openai import OpenAI

from tools import config
from tools.blog_search import DSN, PASSWORD, USER
from tools.session_ctx import current_session

EMB_MODEL = config.EMBED_MODEL  # .env로 제어
SIM_ENTRY = config.PATH_SIM_ENTRY  # 진입점 매칭 임계값 (dedup보다 완화 — 원질문 vs 정규화 문구)

_llm = OpenAI(base_url=config.EMBED_URL, api_key=config.MODEL_API_KEY)
_pool = oracledb.create_pool(user=USER, password=PASSWORD, dsn=DSN,
                             min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                             increment=config.ORACLE_POOL_INCREMENT)


def _ensure_table(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name='SUGGESTIONS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE suggestions (
            ts TIMESTAMP DEFAULT SYSTIMESTAMP, problem VARCHAR2(2000),
            node_id VARCHAR2(36), weight NUMBER,
            session_id VARCHAR2(64), adopted CHAR(1))""")
    else:
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name='SUGGESTIONS' AND column_name='SESSION_ID'""")
        if not cur.fetchone()[0]:
            cur.execute("ALTER TABLE suggestions ADD (session_id VARCHAR2(64), adopted CHAR(1))")


def suggest_paths(problem: str) -> str:
    """과거 조직 구성원들이 비슷한 문제를 어떻게 해결했는지 지식그래프에서 조회한다.
    새 문제를 받으면 다른 도구보다 먼저 호출할 것.

    Args:
        problem: 사용자의 문제/목표를 한 문장으로
    """
    q = np.asarray(
        _llm.embeddings.create(model=EMB_MODEL, input=problem).data[0].embedding,
        dtype=np.float32)
    q /= np.linalg.norm(q)
    with _pool.acquire() as con:
        cur = con.cursor()
        _ensure_table(cur)
        cur.execute("SELECT id, name, embedding FROM nodes WHERE layer = 2")
        goals = []
        for gid, name, emb in cur.fetchall():
            if emb is None:
                continue
            v = np.asarray(json.loads(emb.read()), dtype=np.float32)
            sim = float(v @ q / np.linalg.norm(v))
            if sim >= SIM_ENTRY:
                goals.append((sim, gid, name))
        if not goals:
            return ("이 문제와 유사한 과거 해결 이력이 그래프에 없습니다. "
                    "새로운 유형의 문제이니 자유롭게 접근하세요 (해결하면 그래프에 새 경로로 축적됩니다).")
        goals.sort(reverse=True)
        out = ["📚 과거 조직의 유사 문제 해결 이력:"]
        for sim, gid, gname in goals[:3]:
            out.append(f"\n[유사 목표] {gname} (유사도 {sim:.2f})")
            # 성공/실패는 불리언 플래그가 아니라 세션 판정 카운트로 (poc-results 이슈2 해법)
            cur.execute("""
                SELECT n.id, n.name, e.raw_count, n.fail_reason,
                       (SELECT COUNT(DISTINCT ev.session_id) FROM node_evidence ev
                        JOIN sessions s ON s.id = ev.session_id AND s.turn = 1
                        WHERE ev.node_id = n.id AND s.verdict = 'success') AS sc,
                       (SELECT COUNT(DISTINCT ev.session_id) FROM node_evidence ev
                        JOIN sessions s ON s.id = ev.session_id AND s.turn = 1
                        WHERE ev.node_id = n.id AND s.verdict = 'fail') AS fc
                FROM edges e JOIN nodes n ON n.id = e.dst
                WHERE e.src = :1 AND n.layer = 3
                ORDER BY e.weight DESC""", [gid])  # 보정 가중치 순 (원시 빈도 아님)
            for aid, aname, cnt, reason, sc, fc in cur.fetchall():
                cur.execute("""SELECT n4.name FROM edges e4
                               JOIN nodes n4 ON n4.id = e4.dst
                               WHERE e4.src = :1 AND n4.layer = 4""", [aid])
                tools = ", ".join(t[0].replace("tool:", "") for t in cur.fetchall())
                if fc > sc:  # 실패 우세일 때만 경고 (성공이 우세하면 검증 경로)
                    out.append(f"  ⚠ 과거 실패 우세 접근 (성공 {sc}/실패 {fc}): {aname}"
                               f"\n     실패 이유: {reason}"
                               f"\n     → 상황이 다르면 시도 가능하나, 사용자에게 이 이력을 알릴 것")
                else:
                    mixed = f", 실패 {fc}회 있음" if fc else ""
                    out.append(f"  ✅ 검증된 경로 (성공 {sc}회{mixed}): {aname}"
                               + (f"\n     사용 도구: {tools}" if tools else ""))
                cur.execute("INSERT INTO suggestions (problem, node_id, weight, session_id) "
                            "VALUES (:1, :2, :3, :4)",
                            [problem[:2000], aid, cnt, current_session.get()])
        con.commit()
    return "\n".join(out)


if __name__ == "__main__":
    print(suggest_paths("계좌 데이터 분석하려면 어떤 테이블을 조인해야 하지?"))
    print("\n---\n")
    print(suggest_paths("사내 VPN 신청하고 싶어"))
