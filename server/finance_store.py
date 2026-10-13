"""Keep the existing finance SQL contract on SQLite and PostgreSQL.

SQL is application-owned, values are always bound. PostgreSQL uses SQLAlchemy's
transaction/pool and RETURNING for IDs; SQLite keeps its original driver.
"""
import re
import sqlite3
from contextlib import contextmanager

from sqlalchemy import text

import config
import db

_ID_TABLES = {"finance_sources", "finance_claims", "finance_runs", "finance_evidence", "finance_evidence_jobs"}


def bind_sql(sql, values):
    # Ignore question marks in SQL string literals and quoted identifiers.
    parts, quote, i, parameter = [], None, 0, 0
    while i < len(sql):
        char = sql[i]
        if quote:
            parts.append(char)
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    parts.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            parts.append(char)
        elif char == "?":
            parts.append(f":p{parameter}")
            parameter += 1
        else:
            parts.append(char)
        i += 1
    if parameter != len(values):
        raise ValueError("SQL parameter count does not match")
    return "".join(parts), {f"p{n}": value for n, value in enumerate(values)}


class Result:
    def __init__(self, result, returning_id=False):
        self.lastrowid = result.scalar_one() if returning_id else None
        self.rows = result.mappings() if result.returns_rows and not returning_id else None

    def fetchone(self):
        return self.rows.fetchone() if self.rows is not None else None

    def fetchall(self):
        return self.rows.fetchall() if self.rows is not None else []

    def __iter__(self):
        return iter(self.rows) if self.rows is not None else iter(())


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, values=()):
        # These six legacy tables use implicit SQLite integer IDs. SERIAL is
        # the corresponding PostgreSQL schema; external values stay bound.
        if sql.lstrip().upper().startswith("CREATE TABLE"):
            sql = re.sub(r"\bid INTEGER PRIMARY KEY\b", "id SERIAL PRIMARY KEY", sql)
        insert = re.match(r"\s*INSERT INTO (\w+)\s*\(", sql, re.IGNORECASE)
        returning = bool(insert and insert.group(1) in _ID_TABLES and "RETURNING" not in sql.upper())
        if returning:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        statement, parameters = bind_sql(sql, values)
        return Result(self.connection.execute(text(statement), parameters), returning)

    def executescript(self, sql):
        # Only application-owned CREATE TABLE/INDEX scripts use this method.
        self.connection.execute(text("SELECT pg_advisory_xact_lock(82001431)"))
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)


@contextmanager
def connect():
    if db.IS_POSTGRES:
        with db.engine.begin() as connection:
            yield PostgresConnection(connection)
    else:
        connection = sqlite3.connect(config.DB_PATH, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
