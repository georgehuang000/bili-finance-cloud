"""Create restricted API/worker database roles after owner-only migration.

DATABASE_URL here is the migration owner's connection. Runtime role URLs are
written only to protected local files. No role, password, or existing data is
overwritten. Run on a dedicated Bili cloud database, never a shared app DB.
"""
import os
import secrets
import sys
from pathlib import Path

os.environ["CLOUD_MODE"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from sqlalchemy import text
from sqlalchemy.engine import make_url
from psycopg import sql
import config
import db
import cloud_runtime as cloud
from protect_local_secrets import protect

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "cloud-admin-20261013"
CONTENT = ("uppers", "videos", "dynamics", "summaries", "finance_sources", "finance_claims", "finance_attempts", "finance_runs", "finance_evidence", "finance_evidence_jobs")
API_PRIVATE = ("cloud_settings", "cloud_state", "cloud_jobs", "cloud_owner_sessions", "cloud_login_windows", "cloud_login_challenges", "cloud_owner_factors")
WORKER_PRIVATE = ("cloud_settings", "cloud_state", "cloud_jobs")


def provision():
    if not cloud.ready():
        raise ValueError("请先初始化并迁移到专用云端数据库")
    names = ("bili_cloud_api", "bili_cloud_worker")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if not OUTPUT.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Credential output must remain in this workspace")
    files = [OUTPUT / "api-database.env", OUTPUT / "worker-database.env"]
    if any(path.exists() for path in files):
        raise ValueError("已有运行账户连接文件，未覆盖或轮换凭据")
    passwords = {name: secrets.token_urlsafe(32) for name in names}
    with db.engine.begin() as connection:
        for name in names:
            if connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name}):
                raise ValueError("运行角色已经存在，未修改现有权限")
            command = sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(sql.Identifier(name), sql.Literal(passwords[name]))
            connection.exec_driver_sql(command.as_string(connection.connection.driver_connection))
            connection.exec_driver_sql(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(make_url(db.database_url).database), sql.Identifier(name)).as_string())
            connection.exec_driver_sql(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name)).as_string())
            if connection.scalar(text("SELECT has_schema_privilege(:name,'public','CREATE')"), {"name": name}):
                raise ValueError("数据库默认授予了建表权限，请先收紧专用数据库的默认权限")
        api, worker = names
        for role, tables in ((api, CONTENT + API_PRIVATE), (worker, CONTENT + WORKER_PRIVATE)):
            for table in tables:
                connection.exec_driver_sql(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(sql.Identifier(table), sql.Identifier(role)).as_string())
        for role, tables in ((api, ("uppers",) + API_PRIVATE), (worker, CONTENT[1:] + ("cloud_state", "cloud_jobs"))):
            for table in tables:
                connection.exec_driver_sql(sql.SQL("GRANT INSERT,UPDATE,DELETE ON TABLE {} TO {}").format(sql.Identifier(table), sql.Identifier(role)).as_string())
                sequence = connection.scalar(text("SELECT pg_get_serial_sequence(:table,'id')"), {"table": table}) if table in ("summaries", "finance_sources", "finance_claims", "finance_runs", "finance_evidence", "finance_evidence_jobs", "cloud_jobs") else None
                if sequence:
                    connection.exec_driver_sql(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE {} TO {}").format(sql.Identifier(*sequence.split(".")), sql.Identifier(role)).as_string())
        connection.exec_driver_sql(sql.SQL("GRANT UPDATE(face) ON TABLE uppers TO {}").format(sql.Identifier(worker)).as_string())
    for name, path in zip(names, files):
        url = make_url(db.database_url).set(username=name, password=passwords[name]).render_as_string(hide_password=False)
        path.write_text("DATABASE_URL=" + url + "\n", encoding="utf-8")
    protect([OUTPUT])


if __name__ == "__main__":
    try:
        provision()
        print("Created restricted API/worker roles; URLs saved only to protected local files.")
    except Exception as error:
        print(str(error) if isinstance(error, ValueError) else f"Role provisioning failed ({type(error).__name__}); no credentials printed.", file=sys.stderr)
        raise SystemExit(1)
