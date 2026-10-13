"""Copy a read-only SQLite backup to an EMPTY PostgreSQL cloud database.

Use DATABASE_URL in the process environment, never a command-line password.
Copies content tables, checks counts, and fixes serial sequences. Credentials
are deliberately excluded; use the owner UI to configure the official API.
"""
import argparse
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from sqlalchemy import MetaData, Table, func, inspect, select, text

os.environ["CLOUD_MODE"] = "true"
os.environ["CLOUD_WORKER"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import config
import db
import cloud_runtime
import finance_analysis
import finance_evidence

TABLES = ("uppers", "videos", "dynamics", "summaries", "finance_sources", "finance_claims", "finance_attempts", "finance_runs", "finance_evidence", "finance_evidence_jobs")


def migrate(source):
    if not db.IS_POSTGRES:
        raise ValueError("请先通过环境变量设置目标 PostgreSQL DATABASE_URL")
    source = Path(source).resolve(strict=True)
    # Creating schema with the seed list disabled leaves a genuinely empty
    # target. This tool refuses a populated target instead of merging history.
    config.UP_UIDS = []
    db.init_db()
    finance_analysis.init_store()
    finance_evidence.init_store()
    metadata = MetaData()
    metadata.reflect(db.engine, only=list(TABLES))
    counts = {}
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as sqlite:
        sqlite.row_factory = sqlite3.Row
        sqlite.execute("PRAGMA query_only=ON")
        existing = {row[0] for row in sqlite.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with db.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(82001433)"))
            for name in TABLES:
                table = metadata.tables[name]
                if connection.scalar(select(func.count()).select_from(table)):
                    raise ValueError(f"目标表 {name} 已有数据，未覆盖；请选择新的测试数据库")
            for name in TABLES:
                if name not in existing:
                    counts[name] = 0
                    continue
                table = metadata.tables[name]
                source_columns = {row[1] for row in sqlite.execute(f'PRAGMA table_info("{name}")')}
                columns = [col.name for col in table.columns if col.name in source_columns]
                projection = ",".join(f'"{column}"' for column in columns)
                cursor = sqlite.execute(f'SELECT {projection} FROM "{name}"')
                total = 0
                while batch := cursor.fetchmany(200):
                    connection.execute(table.insert(), [dict(row) for row in batch])
                    total += len(batch)
                if connection.scalar(select(func.count()).select_from(table)) != total:
                    raise ValueError(f"表 {name} 行数校验失败，事务已回滚")
                if "id" in table.columns:
                    # Names are fixed, application-owned identifiers.
                    connection.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}','id'), COALESCE((SELECT max(id) FROM {name}),1), EXISTS(SELECT 1 FROM {name}))"))
                counts[name] = total
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        import json
        print(json.dumps({"migrated": migrate(args.source)}, ensure_ascii=False))
    except Exception as error:
        print(str(error) if isinstance(error, ValueError) else f"迁移失败 ({type(error).__name__})，请检查连接和数据结构；源数据未修改", file=sys.stderr)
        raise SystemExit(1)
