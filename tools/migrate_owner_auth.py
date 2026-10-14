"""Add the MFA table with an owner connection; never rotate runtime credentials."""
import os
import sys
from pathlib import Path

os.environ["CLOUD_MODE"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from sqlalchemy import inspect, text
import db
import cloud_runtime as cloud


def migrate():
    if not db.IS_POSTGRES:
        raise ValueError("Requires the dedicated cloud PostgreSQL database")
    with db.engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(82001431)"))
        if not {"cloud_owner_sessions", "cloud_login_windows", "cloud_login_challenges"}.issubset(set(inspect(connection).get_table_names())):
            raise ValueError("Existing cloud authentication schema is required")
        for role in ("bili_cloud_api", "bili_cloud_worker"):
            if not connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}):
                raise ValueError("Restricted runtime roles must already exist")
        cloud.OwnerFactor.__table__.create(connection, checkfirst=True)
        connection.exec_driver_sql("REVOKE ALL ON cloud_owner_factors FROM PUBLIC, bili_cloud_worker")
        connection.exec_driver_sql("GRANT SELECT, INSERT, UPDATE, DELETE ON cloud_owner_factors TO bili_cloud_api")


if __name__ == "__main__":
    try:
        migrate()
        print("MFA schema ready; API grants applied, worker access denied.")
    except Exception as error:
        print(f"MFA migration failed ({type(error).__name__}); no credentials printed.", file=sys.stderr)
        raise SystemExit(1)
