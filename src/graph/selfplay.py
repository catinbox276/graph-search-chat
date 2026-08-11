"""self-play 러너 — tasks.yaml의 47세션을 에이전트로 실행해 sessions 테이블에 기록.

세션 id = "<task_id>-<run번호>" (예: R1-2). 게이트 판정(verdict)은 별도 단계에서.
usage: .venv/bin/python graph/selfplay.py [--only R1,R2]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oracledb

from core import config
import yaml

from agent.agent import build_agent


def all_runs(only=None):
    tasks = yaml.safe_load(open(ROOT / "graph" / "tasks.yaml"))
    for group in ("repeat", "single", "fail"):
        for t in tasks[group]:
            if only and t["id"] not in only:
                continue
            for i, q in enumerate(t["runs"], 1):
                yield f"{t['id']}-{i}", group, q


async def main():
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    agent = await build_agent()
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    done = {r[0] for r in cur.execute("SELECT id FROM sessions")}
    runs = [r for r in all_runs(only) if r[0] not in done]  # 재실행 시 이어하기
    print(f"실행 대상 {len(runs)}세션 (완료분 {len(done)}건 스킵)")
    for n, (sid, group, q) in enumerate(runs, 1):
        t0 = time.time()
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": q}]})
            calls = [{"name": c["name"], "args": c["args"]}
                     for m in result["messages"]
                     for c in (getattr(m, "tool_calls", None) or [])]
            answer = result["messages"][-1].content
        except Exception as e:
            calls, answer = [], f"[에이전트 오류] {e}"
        cur.execute(
            "INSERT INTO sessions (id, turn, question, tool_calls, answer) "
            "VALUES (:1, 1, :2, :3, :4)",
            [sid, q, json.dumps(calls, ensure_ascii=False), answer])
        con.commit()
        print(f"[{n}/{len(runs)}] {sid} ({group}) 툴 {len(calls)}회 "
              f"{time.time()-t0:.0f}s", flush=True)
    con.close()
    print("self-play 완료")


if __name__ == "__main__":
    asyncio.run(main())
