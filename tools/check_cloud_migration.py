"""Compare every migrated content field with the private read-only backup."""
import argparse
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from sqlalchemy import MetaData, select

os.environ["CLOUD_MODE"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import db
from migrate_cloud_data import TABLES


def verify(source):
    if not db.IS_POSTGRES:
        raise ValueError("DATABASE_URL must point to PostgreSQL")
    metadata = MetaData()
    metadata.reflect(db.engine, only=list(TABLES))
    counts = {}
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as backup, db.engine.connect() as cloud:
        backup.row_factory = sqlite3.Row
        existing = {row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in TABLES:
            if name not in existing:
                continue
            table = metadata.tables[name]
            columns = [row[1] for row in backup.execute(f'PRAGMA table_info("{name}")')]
            columns = [column for column in columns if column in table.columns]
            before = [dict(row) for row in backup.execute(f'SELECT * FROM "{name}"')]
            after = [dict(row) for row in cloud.execute(select(*[table.c[column] for column in columns])).mappings()]
            import json
            encode = lambda row: json.dumps({key: row[key] for key in columns}, sort_keys=True, ensure_ascii=False)
            if sorted(map(encode, before)) != sorted(map(encode, after)):
                raise ValueError(f"Table {name} differs; no changes were made")
            counts[name] = len(before)
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    import json
    print(json.dumps({"all_fields_match": verify(args.source)}))
