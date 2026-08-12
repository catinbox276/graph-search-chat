"""LangGraph 체크포인터의 Oracle 구현 — 멀티턴 기억의 외부화.

책임 경계 원칙(CLAUDE.md §6)에 따라 별도 저장소(Redis 등) 없이 Oracle 19c에 저장.
- 복제본 몇 개든 같은 기억을 공유 → 클러스터 모드에서 세션 고정 불필요
- 서버/파드 재시작에도 대화 기억 유지

테이블: lg_checkpoints(스레드별 상태 스냅샷), lg_writes(펜딩 쓰기)
동기 구현 + asyncio.to_thread 래핑 (astream 경로 지원).
"""
import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import oracledb
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from core import config


def _lob_bytes(v) -> bytes:
    """BLOB 값을 bytes로 — LOB 로케이터·bytes·None 모두 수용.
    thick 모드는 같은 세션에서 쓴 BLOB 등을 LOB이 아닌 bytes로 돌려줄 수 있어,
    .read()를 무방비로 부르면 'bytes' object has no attribute 'read'로 죽는다."""
    if v is None:
        return b""
    if hasattr(v, "read"):
        return bytes(v.read())
    return bytes(v)


class OracleSaver(BaseCheckpointSaver):
    def __init__(self):
        super().__init__()
        self._pool = oracledb.create_pool(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN,
                                          min=config.ORACLE_POOL_MIN, max=config.ORACLE_POOL_MAX,
                                          increment=config.ORACLE_POOL_INCREMENT)
        self._ensure_tables()

    def _ensure_tables(self):
        with self._pool.acquire() as con:
            cur = con.cursor()
            for name, ddl in (
                ("LG_CHECKPOINTS", """CREATE TABLE lg_checkpoints (
                    thread_id VARCHAR2(200), ckpt_ns VARCHAR2(200),
                    ckpt_id VARCHAR2(200), parent_id VARCHAR2(200),
                    ctype VARCHAR2(60), ckpt BLOB,
                    mtype VARCHAR2(60), metadata BLOB,
                    ts TIMESTAMP DEFAULT SYSTIMESTAMP,
                    PRIMARY KEY (thread_id, ckpt_ns, ckpt_id))"""),
                ("LG_WRITES", """CREATE TABLE lg_writes (
                    thread_id VARCHAR2(200), ckpt_ns VARCHAR2(200),
                    ckpt_id VARCHAR2(200), task_id VARCHAR2(200),
                    idx NUMBER, channel VARCHAR2(200),
                    vtype VARCHAR2(60), val BLOB,
                    PRIMARY KEY (thread_id, ckpt_ns, ckpt_id, task_id, idx))"""),
            ):
                cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name=:1", [name])
                if not cur.fetchone()[0]:
                    cur.execute(ddl)
            con.commit()

    NS_EMPTY = "~"  # Oracle은 ''를 NULL로 취급 — 빈 네임스페이스 센티널

    @classmethod
    def _cfg(cls, config):
        c = config["configurable"]
        ns = c.get("checkpoint_ns", "") or cls.NS_EMPTY
        return c["thread_id"], ns, c.get("checkpoint_id")

    def _row_to_tuple(self, cur, row) -> CheckpointTuple:
        thread_id, ns, ckpt_id, parent_id, ctype, ckpt, mtype, meta = row
        ns_out = "" if ns == self.NS_EMPTY else ns
        cur.execute("""SELECT task_id, channel, vtype, val FROM lg_writes
                       WHERE thread_id=:1 AND ckpt_ns=:2 AND ckpt_id=:3
                       ORDER BY task_id, idx""", [thread_id, ns, ckpt_id])
        # Oracle은 빈 BLOB(b"")을 NULL로 저장 — None 가드 필수
        writes = [(t, ch, self.serde.loads_typed((vt, _lob_bytes(v))))
                  for t, ch, vt, v in cur.fetchall()]
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ns_out,
                                     "checkpoint_id": ckpt_id}},
            checkpoint=self.serde.loads_typed((ctype, _lob_bytes(ckpt))),
            metadata=self.serde.loads_typed((mtype, _lob_bytes(meta))),
            parent_config=({"configurable": {"thread_id": thread_id,
                                             "checkpoint_ns": ns_out,
                                             "checkpoint_id": parent_id}}
                           if parent_id else None),
            pending_writes=writes,
        )

    def get_tuple(self, config) -> CheckpointTuple | None:
        thread_id, ns, ckpt_id = self._cfg(config)
        with self._pool.acquire() as con:
            cur = con.cursor()
            if ckpt_id:
                cur.execute("""SELECT thread_id, ckpt_ns, ckpt_id, parent_id, ctype,
                               ckpt, mtype, metadata FROM lg_checkpoints
                               WHERE thread_id=:1 AND ckpt_ns=:2 AND ckpt_id=:3""",
                            [thread_id, ns, ckpt_id])
            else:  # 최신 체크포인트 (ckpt_id는 시간순 정렬 가능한 UUID)
                cur.execute("""SELECT thread_id, ckpt_ns, ckpt_id, parent_id, ctype,
                               ckpt, mtype, metadata FROM lg_checkpoints
                               WHERE thread_id=:1 AND ckpt_ns=:2
                               ORDER BY ckpt_id DESC FETCH FIRST 1 ROWS ONLY""",
                            [thread_id, ns])
            row = cur.fetchone()
            return self._row_to_tuple(cur, row) if row else None

    def list(self, config, *, filter=None, before=None, limit=None) -> Iterator[CheckpointTuple]:
        thread_id, ns, _ = self._cfg(config) if config else (None, self.NS_EMPTY, None)
        with self._pool.acquire() as con:
            cur = con.cursor()
            q = """SELECT thread_id, ckpt_ns, ckpt_id, parent_id, ctype, ckpt, mtype, metadata
                   FROM lg_checkpoints WHERE thread_id=:t AND ckpt_ns=:n"""
            binds = {"t": thread_id, "n": ns}
            if before:
                q += " AND ckpt_id < :b"
                binds["b"] = before["configurable"]["checkpoint_id"]
            q += " ORDER BY ckpt_id DESC"
            if limit:
                q += f" FETCH FIRST {int(limit)} ROWS ONLY"
            cur.execute(q, binds)
            for row in cur.fetchall():
                yield self._row_to_tuple(cur, row)

    def put(self, config, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions):
        thread_id, ns, parent_id = self._cfg(config)
        ctype, cbytes = self.serde.dumps_typed(checkpoint)
        mtype, mbytes = self.serde.dumps_typed(metadata)
        with self._pool.acquire() as con:
            cur = con.cursor()
            cur.execute("""MERGE INTO lg_checkpoints c USING dual
                ON (c.thread_id=:t AND c.ckpt_ns=:n AND c.ckpt_id=:i)
                WHEN MATCHED THEN UPDATE SET ctype=:ct, ckpt=:cb, mtype=:mt, metadata=:mb
                WHEN NOT MATCHED THEN INSERT
                  (thread_id, ckpt_ns, ckpt_id, parent_id, ctype, ckpt, mtype, metadata)
                  VALUES (:t, :n, :i, :p, :ct, :cb, :mt, :mb)""",
                {"t": thread_id, "n": ns, "i": checkpoint["id"], "p": parent_id,
                 "ct": ctype, "cb": cbytes, "mt": mtype, "mb": mbytes})
            con.commit()
        return {"configurable": {"thread_id": thread_id,
                                 "checkpoint_ns": "" if ns == self.NS_EMPTY else ns,
                                 "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes: Sequence[tuple[str, Any]], task_id: str,
                   task_path: str = "") -> None:
        thread_id, ns, ckpt_id = self._cfg(config)
        with self._pool.acquire() as con:
            cur = con.cursor()
            for i, (channel, value) in enumerate(writes):
                vtype, vbytes = self.serde.dumps_typed(value)
                idx = WRITES_IDX_MAP.get(channel, i)
                cur.execute("""MERGE INTO lg_writes w USING dual
                    ON (w.thread_id=:t AND w.ckpt_ns=:n AND w.ckpt_id=:c
                        AND w.task_id=:k AND w.idx=:x)
                    WHEN MATCHED THEN UPDATE SET channel=:ch, vtype=:vt, val=:vb
                    WHEN NOT MATCHED THEN INSERT
                      (thread_id, ckpt_ns, ckpt_id, task_id, idx, channel, vtype, val)
                      VALUES (:t, :n, :c, :k, :x, :ch, :vt, :vb)""",
                    {"t": thread_id, "n": ns, "c": ckpt_id, "k": task_id, "x": idx,
                     "ch": channel, "vt": vtype, "vb": vbytes})
            con.commit()

    def delete_thread(self, thread_id: str) -> None:
        with self._pool.acquire() as con:
            cur = con.cursor()
            cur.execute("DELETE FROM lg_writes WHERE thread_id=:1", [thread_id])
            cur.execute("DELETE FROM lg_checkpoints WHERE thread_id=:1", [thread_id])
            con.commit()

    # --- async 래핑 (동기 구현을 스레드로) ---
    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config, *, filter=None, before=None, limit=None) -> AsyncIterator[CheckpointTuple]:
        for t in await asyncio.to_thread(
                lambda: list(self.list(config, filter=filter, before=before, limit=limit))):
            yield t

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id):
        return await asyncio.to_thread(self.delete_thread, thread_id)
