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
from core import config, model_registry
from core.session_ctx import current_session

SIM_ENTRY = config.PATH_SIM_ENTRY  # 진입점 매칭 임계값 (dedup보다 완화 — 원질문 vs 정규화 문구)

_pool = oracledb.create_pool(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN,
                             min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                             increment=config.ORACLE_POOL_INCREMENT)


def _ensure_table(cur):
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name='SUGGESTIONS'")
    if not cur.fetchone()[0]:
        cur.execute("""CREATE TABLE suggestions (
            id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ts TIMESTAMP DEFAULT SYSTIMESTAMP, problem VARCHAR2(2000),
            node_id VARCHAR2(36) NOT NULL, weight NUMBER,
            session_id VARCHAR2(36), adopted CHAR(1),
            CONSTRAINT suggestions_node_fk FOREIGN KEY (node_id)
              REFERENCES nodes(id) ON DELETE CASCADE)""")
        cur.execute("CREATE INDEX suggestions_session_ix ON suggestions (session_id)")
        cur.execute("CREATE INDEX suggestions_node_ix ON suggestions (node_id)")


def suggest_paths(problem: str) -> str:
    """과거 조직 구성원들이 비슷한 문제를 어떻게 해결했는지 지식그래프에서 조회한다.
    새 문제를 받으면 다른 도구보다 먼저 호출할 것.

    Args:
        problem: 사용자의 문제/목표를 한 문장으로
    """
    cli, emb_name = model_registry.embedding_client()
    q = np.asarray(
        cli.embeddings.create(model=emb_name, input=problem).data[0].embedding,
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
            v = np.asarray(json.loads(emb.read() if hasattr(emb, "read") else emb),
                           dtype=np.float32)
            if v.shape[0] != q.shape[0]:
                continue  # 임베딩 모델 교체로 차원이 다른 옛 벡터 → 비교 불가, 건너뜀
                          # (nodes.embedding 재백필 전까지 그 노드는 경로 제안에서 제외)
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
                       (SELECT COUNT(*) FROM node_evidence ev
                        JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                        WHERE ev.node_id = n.id AND ev.kind = 'session'
                          AND s.verdict = 'success') AS sc,
                       (SELECT COUNT(*) FROM node_evidence ev
                        JOIN sessions s ON s.id = ev.ref AND s.turn = 1
                        WHERE ev.node_id = n.id AND ev.kind = 'session'
                          AND s.verdict = 'fail') AS fc,
                       (SELECT COUNT(*) FROM node_evidence ev
                        WHERE ev.node_id = n.id AND ev.kind = 'doc') AS dc,
                       e.weight
                FROM edges e JOIN nodes n ON n.id = e.dst
                WHERE e.src = :1 AND n.layer = 3""", [gid])
            # 서열: 실전 검증(세션 성공) > 문서 근거만 > 실패 우세. 같은 단계 안에서는 보정 가중치 순
            rows = sorted(cur.fetchall(),
                          key=lambda r: (0 if r[4] > 0 and r[4] >= r[5]
                                         else 2 if r[5] > r[4] else 1, -r[7]))
            for aid, aname, cnt, reason, sc, fc, dc, _w in rows:
                cur.execute("""SELECT n4.name FROM edges e4
                               JOIN nodes n4 ON n4.id = e4.dst
                               WHERE e4.src = :1 AND n4.layer = 4""", [aid])
                tools = ", ".join(t[0].replace("tool:", "") for t in cur.fetchall())
                docs = f", 참고 문서 {dc}건" if dc else ""
                # 근거 문서 id 노출 — 답변 인용·footer 수집·read_doc 열람이 가능해진다
                ev_line = ""
                if dc:
                    cur.execute("""SELECT ref FROM node_evidence
                                   WHERE node_id = :1 AND kind = 'doc'
                                   FETCH FIRST 3 ROWS ONLY""", [aid])
                    pids = " ".join(f"[{r[0]}]" for r in cur.fetchall())
                    if pids:
                        ev_line = f"\n     근거 문서 (read_doc로 열람 가능): {pids}"
                if fc > sc:  # 실패 우세일 때만 경고 (성공이 우세하면 검증 경로)
                    out.append(f"  ⚠ 과거 실패 우세 접근 (성공 {sc}/실패 {fc}{docs}): {aname}"
                               f"\n     실패 이유: {reason}"
                               f"\n     → 상황이 다르면 시도 가능하나, 사용자에게 이 이력을 알릴 것")
                elif sc:  # 실전 세션 성공이 있어야만 '검증'
                    mixed = f", 실패 {fc}회 있음" if fc else ""
                    out.append(f"  ✅ 검증된 경로 (성공 {sc}회{mixed}{docs}): {aname}"
                               + (f"\n     사용 도구: {tools}" if tools else "") + ev_line)
                else:  # 세션 성공 없음 — 검증 아님을 명시 (대부분 문서 유래)
                    src = f"문서 {dc}건" if dc else "성공 이력 소급 취소됨"
                    out.append(f"  📄 미검증 경로 ({src}, 실전 검증 이력 없음): {aname}"
                               + (f"\n     사용 도구: {tools}" if tools else "") + ev_line)
                cur.execute("INSERT INTO suggestions (problem, node_id, weight, session_id) "
                            "VALUES (:1, :2, :3, :4)",
                            [problem[:2000], aid, cnt, current_session.get()])
        # 탐색 노출 — 상위 3개 목표에 못 든 다음 순위 목표의 대표 접근법 1개를
        # 라벨을 달아 노출. 노출 기회가 없는 경로는 채택률 보정으로도 교정 불가
        # (피드백 루프 퇴행 완화 — docs/references 2026-08-10 조사, DeepMind 2019 처방).
        # 몰래 섞지 않고 탐색임을 명시하는 게 신뢰 조건.
        if len(goals) > 3:
            _, xgid, xgname = goals[3]
            cur.execute("""SELECT n.id, n.name, e.raw_count FROM edges e
                           JOIN nodes n ON n.id = e.dst
                           WHERE e.src = :1 AND n.layer = 3
                             AND NVL(n.fail_flag, 'N') = 'N'
                           ORDER BY e.weight DESC FETCH FIRST 1 ROWS ONLY""", [xgid])
            r = cur.fetchone()
            if r:
                out.append(f"\n🔍 탐색 제안 (유사도 컷 바깥 — 관련성 낮을 수 있음): "
                           f"목표 '{xgname}'의 접근법: {r[1]}"
                           f"\n     아직 노출 기회가 적었던 경로입니다. 문제와 맞을 때만 참고.")
                cur.execute("INSERT INTO suggestions (problem, node_id, weight, session_id) "
                            "VALUES (:1, :2, :3, :4)",
                            [problem[:2000], r[0], r[2], current_session.get()])
        con.commit()
    return "\n".join(out)


if __name__ == "__main__":
    print(suggest_paths("계좌 데이터 분석하려면 어떤 테이블을 조인해야 하지?"))
    print("\n---\n")
    print(suggest_paths("사내 VPN 신청하고 싶어"))
