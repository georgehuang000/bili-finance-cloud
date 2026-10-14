"""One-time creator archive import from the owner's trusted desktop network.

Uses existing private worker DB credentials and encrypted Bili cookies. No
model requests, secret CLI arguments, cookie files or browser exports.
Run only while the cloud collector is paused to avoid overlapping writers.
"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import random
import sys

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", required=True)
    args = parser.parse_args()
    if not args.uid.isdigit():
        raise ValueError("Numeric subscribed UID required")
    root = Path(__file__).resolve().parents[1]
    private = root / "outputs/cloud-admin-20261013"
    os.environ.update(CLOUD_MODE="true", CLOUD_WORKER="true",
                      DATABASE_URL=dotenv_values(private / "worker-database.env")["DATABASE_URL"],
                      CREDENTIAL_ENCRYPTION_KEY=dotenv_values(private / ".env")["CREDENTIAL_ENCRYPTION_KEY"])
    sys.path.insert(0, str(root / "server"))
    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: original(*a, **{**k, "trust_env": False})
    import config
    import db
    import bili_api
    import cloud_runtime as cloud
    from sqlalchemy.engine import make_url
    url = make_url(config.DATABASE_URL)
    if url.username != "bili_cloud_worker" or not (url.host or "").endswith(".neon.tech"):
        raise ValueError("Expected the restricted Bili worker database")
    if args.uid not in db.subscribed_uids():
        raise ValueError("Only subscribed creators can be imported")
    cloud.refresh_settings()
    logging.disable(logging.CRITICAL)

    async def collect():
        seen = set()
        try:
            for page_number in range(1, 1001):
                page = await bili_api.fetch_archive_page(args.uid, page=page_number, source="legacy")
                if not page["rows"] and page["has_more"]:
                    raise ValueError("Archive page did not progress")
                db.upsert_videos(page["rows"])
                seen.update(row["bvid"] for row in page["rows"])
                print(json.dumps({"page": page_number, "unique_fetched": len(seen),
                                  "stored": db.count_videos(args.uid), "declared_total": page["total"]}), flush=True)
                if not page["has_more"]:
                    if len(seen) < page["total"]:
                        raise ValueError("Archive source ended before declared scope")
                    print(json.dumps({"result": "complete", "uid": args.uid,
                                      "source_total": page["total"], "stored": db.count_videos(args.uid)}), flush=True)
                    return
                await asyncio.sleep(random.uniform(3, 6))
            raise ValueError("Archive page safety budget reached")
        finally:
            await bili_api.aclose_client()
            db.engine.dispose()
    asyncio.run(collect())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"import_failed": type(error).__name__, "api_error_code": getattr(error, "code", None), "private_details": "suppressed"}), flush=True)
        raise SystemExit(1)
