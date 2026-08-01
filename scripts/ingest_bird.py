"""BIRD SQLite 스키마 -> DataHub 직접 emit.

sqlalchemy 소스가 SQLite의 이름 없는 FK(name=None)에서 avro 검증 실패하는
버그를 우회한다: PRAGMA로 직접 읽고 FK 이름을 생성해서 넣는다.

usage: python3 scripts/ingest_bird.py <db_name> [<db_name> ...]
"""
import sqlite3
import sys
from pathlib import Path

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    AuditStampClass,
    DatasetPropertiesClass,
    DateTypeClass,
    ForeignKeyConstraintClass,
    MySqlDDLClass,
    NumberTypeClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StatusClass,
    StringTypeClass,
    TimeTypeClass,
)

BIRD = Path(__file__).parent.parent / "data/bird_dev/dev_20240627/dev_databases"
GMS = "http://localhost:8080"

TYPE_MAP = {
    "INTEGER": NumberTypeClass, "REAL": NumberTypeClass, "NUMERIC": NumberTypeClass,
    "DATE": DateTypeClass, "DATETIME": TimeTypeClass,
}


def field_type(native: str):
    cls = TYPE_MAP.get((native or "TEXT").upper().split("(")[0], StringTypeClass)
    return SchemaFieldDataTypeClass(type=cls())


def dataset_urn(db: str, table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:sqlite,{db}.main.{table},PROD)"


def ingest_db(db: str, emitter: DatahubRestEmitter) -> int:
    con = sqlite3.connect(BIRD / db / f"{db}.sqlite")
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        cols = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        fields = [
            SchemaFieldClass(
                fieldPath=c[1], type=field_type(c[2]), nativeDataType=c[2] or "TEXT",
                nullable=not c[3], isPartOfKey=bool(c[5]),
            ) for c in cols
        ]
        fks = []
        for i, fk in enumerate(con.execute(f'PRAGMA foreign_key_list("{table}")')):
            _, _, ref_table, col, ref_col = fk[0], fk[1], fk[2], fk[3], fk[4]
            fks.append(ForeignKeyConstraintClass(
                name=f"fk_{table}_{i}",
                sourceFields=[f"urn:li:schemaField:({dataset_urn(db, table)},{col})"],
                foreignFields=[f"urn:li:schemaField:({dataset_urn(db, ref_table)},{ref_col})"],
                foreignDataset=dataset_urn(db, ref_table),
            ))
        audit = AuditStampClass(time=0, actor="urn:li:corpuser:ingest")
        urn = dataset_urn(db, table)
        for aspect in (
            DatasetPropertiesClass(name=f"{db}.main.{table}",
                                   description=f"BIRD {db} DB의 {table} 테이블"),
            StatusClass(removed=False),
            SchemaMetadataClass(
                schemaName=f"main.{table}", platform="urn:li:dataPlatform:sqlite",
                version=0, created=audit, lastModified=audit, hash="",
                platformSchema=MySqlDDLClass(tableSchema=""),
                fields=fields, foreignKeys=fks or None,
            ),
        ):
            emitter.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
    con.close()
    return len(tables)


def main():
    dbs = sys.argv[1:] or ["california_schools", "financial", "codebase_community"]
    emitter = DatahubRestEmitter(GMS)
    for db in dbs:
        n = ingest_db(db, emitter)
        print(f"{db}: 테이블 {n}개 emit")


if __name__ == "__main__":
    main()
