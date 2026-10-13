"""Integration checks against an explicitly named, disposable local Postgres.

Set DATABASE_URL to the local bili_cloud_test fixture database. Never runs
destructive setup against Neon, the Pi database, or another project database.
No test makes an external Bilibili or model request.
"""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import time
import httpx

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from cryptography.fernet import Fernet

import config
import db
import cloud_runtime as cloud
import finance_analysis as analysis
import finance_evidence as evidence
import main

_url = make_url(config.DATABASE_URL) if config.DATABASE_URL else None
_safe = bool(_url and _url.host in ("127.0.0.1", "localhost") and _url.database == "bili_cloud_test" and config.CLOUD_MODE)


@unittest.skipUnless(_safe, "Requires an explicitly named disposable local Postgres")
class CloudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.ADMIN_PASSWORD_HASH = cloud.password_hash("fixture-password")
        config.CREDENTIAL_ENCRYPTION_KEY = Fernet.generate_key().decode()
        config.CLOUD_TEST_MODE = True
        config.CLOUD_API_ONLY = True
        db.init_db()
        analysis.init_store()
        evidence.init_store()

    def setUp(self):
        tables = inspect(db.engine).get_table_names()
        with db.engine.begin() as connection:
            connection.execute(text("TRUNCATE " + ",".join(f'"{name}"' for name in tables) + " RESTART IDENTITY"))
        db.add_upper("1000", "测试作者")
        self.client = TestClient(main.app, base_url="https://testserver")

    def owner(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"password": "fixture-password"}).status_code, 200)

    def test_public_can_read_but_cannot_search_manage_or_run_models(self):
        self.assertEqual(self.client.get("/api/uppers").status_code, 200)
        self.assertEqual(self.client.get("/api/uppers/1000/videos").status_code, 200)
        self.assertEqual(self.client.get("/api/uppers/2000/videos").status_code, 404)
        for method, path in [("get", "/api/uppers/search?q=测试"), ("get", "/api/llm/settings"), ("get", "/api/login/qrcode/poll?key=12345678"), ("get", "/api/diagnostics"), ("post", "/api/uppers"), ("delete", "/api/uppers/1000"), ("post", "/api/crawl"), ("post", "/api/llm/test"), ("post", "/api/research/analyze"), ("post", "/api/research/evidence")]:
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)

    def test_owner_session_secure_logout_and_origin(self):
        self.owner()
        self.assertTrue(self.client.get("/api/auth/session").json()["can_manage"])
        self.assertIn("HttpOnly", self.client.post("/api/auth/login", json={"password": "fixture-password"}).headers["set-cookie"])
        self.assertEqual(self.client.post("/api/crawl", headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/crawl", json={"uid": "1000"}).status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/crawl").status_code, 401)

    def test_persistent_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/api/auth/login", json={"password": "wrong"}).status_code, 401)
        result = self.client.post("/api/auth/login", json={"password": "fixture-password"})
        self.assertIn("15分钟", result.json()["detail"])

    def test_cookies_are_encrypted_and_model_configuration_is_environment_only(self):
        cloud.save_settings({"BILI_SESSDATA": "synthetic-cookie-secret"})
        with db.session_scope() as session:
            self.assertNotIn("synthetic-cookie-secret", session.get(cloud.Setting, "BILI_SESSDATA").encrypted)
            # Ignore old model rows even if a legacy deployment once wrote them.
            session.add(cloud.Setting(key="LLM_API_KEY", encrypted="legacy-row-not-decrypted"))
        self.assertEqual(cloud.read_settings()["BILI_SESSDATA"], "synthetic-cookie-secret")
        with self.assertRaises(ValueError):
            cloud.save_settings({"LLM_API_KEY": "attempted-model-key"})
        with patch.object(config, "LLM_API_KEY", "deployment-only-key"):
            cloud.refresh_settings()
            self.assertEqual(config.LLM_API_KEY, "deployment-only-key")

    def test_model_test_cannot_be_redirected_and_is_not_run_anonymously(self):
        self.owner()
        with patch("llm_client.test_connection", return_value={"ok": True}) as call, patch.object(config, "LLM_API_KEY", "server-only-model-key"):
            result = self.client.post("/api/llm/test", json={"api_key": "must-not-echo", "base_url": "https://evil.example"})
            self.assertEqual(result.status_code, 422)
            self.assertNotIn("must-not-echo", result.text)
            call.assert_not_called()
            self.assertEqual(self.client.post("/api/llm/test", json={}).status_code, 200)
            self.assertEqual(call.call_args.args[1], "https://api.deepseek.com/v1")
            self.assertEqual(call.call_args.args[0], "server-only-model-key")
            for method, path in (("get", "/api/llm/settings"), ("post", "/api/llm/settings"), ("post", "/api/llm/models")):
                self.assertIn(getattr(self.client, method)(path, **({"json": {"api_key": "attempt"}} if method == "post" else {})).status_code, (404, 405))
            self.assertFalse(any(getattr(route, "path", "") == "/api/llm/settings" for route in main.app.routes))

    def test_proxy_origin_is_trusted_only_on_vercel_and_cross_site_stays_blocked(self):
        request = SimpleNamespace(url=httpx.URL("http://internal.test:9000/api/auth/login"), headers={
            "origin": "https://public.example", "x-forwarded-host": "public.example",
            "x-forwarded-proto": "https", "sec-fetch-site": "same-origin"})
        with patch.dict(os.environ, {"VERCEL": "0"}):
            self.assertFalse(cloud.same_origin(request))
        with patch.dict(os.environ, {"VERCEL": "1"}):
            self.assertTrue(cloud.same_origin(request))
            for changes in ({"origin": "https://outside.example"}, {"sec-fetch-site": "cross-site"}, {"x-forwarded-host": "public.example,evil.example"}, {"origin": "null"}):
                with patch.dict(request.headers, changes):
                    self.assertFalse(cloud.same_origin(request))
        from starlette.requests import Request
        local = Request({"type": "http", "scheme": "http", "path": "/api/auth/login", "server": ("127.0.0.1", 9000), "client": ("127.0.0.1", 1234), "headers": [(b"origin", b"http://127.0.0.1:19004"), (b"x-vercel-deployment-url", b"127.0.0.1:19004")]})
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "development"}):
            self.assertTrue(cloud.same_origin(local))
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}):
            self.assertFalse(cloud.same_origin(local))
        with patch.dict(os.environ, {"VERCEL": "0", "BILI_LOCAL_VERCEL_PROXY": "1"}):
            self.assertTrue(cloud.same_origin(local))

    def test_qr_is_session_bound_expires_and_does_not_accept_url_queries(self):
        self.owner()
        key = "fixture-qr-key-1234"
        with patch("bili_login.create_qr", new=AsyncMock(return_value={"key": key, "image": "data:image/svg+xml;base64,fixture"})):
            self.assertEqual(self.client.post("/api/login/qrcode").status_code, 200)
        other = TestClient(main.app, base_url="https://testserver")
        other.post("/api/auth/login", json={"password": "fixture-password"})
        with patch("bili_login.poll_qr", new=AsyncMock()) as poll:
            self.assertEqual(other.post("/api/login/qrcode/poll", json={"key": key}).status_code, 403)
            self.assertIn(self.client.get("/api/login/qrcode/poll?key=" + key).status_code, (404, 405))
            with db.session_scope() as session:
                session.query(cloud.LoginChallenge).update({"expires_ts": int(time.time()) - 1})
            self.assertEqual(self.client.post("/api/login/qrcode/poll", json={"key": key}).status_code, 403)
            poll.assert_not_awaited()

    def test_qr_confirmation_atomically_encrypts_cookie_and_replay_is_harmless(self):
        self.owner()
        key = "fixture-confirm-key"
        cloud.register_qr(key, SimpleNamespace(cookies={cloud.COOKIE: self.client.cookies.get(cloud.COOKIE)}))
        async def confirmed(qr_key, store_cookies=None):
            store_cookies({"SESSDATA": "synthetic-session-cookie", "bili_jct": "synthetic-csrf-cookie"})
            return {"status": "confirmed", "message": "登录态已安全保存"}
        with patch("bili_login.poll_qr", new=AsyncMock(side_effect=confirmed)) as poll:
            response = self.client.post("/api/login/qrcode/poll", json={"key": key})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("synthetic-session-cookie", response.text)
            self.assertEqual(self.client.post("/api/login/qrcode/poll", json={"key": key}).status_code, 200)
            self.assertEqual(poll.await_count, 1)
        with db.session_scope() as session:
            self.assertNotIn("synthetic-session-cookie", session.get(cloud.Setting, "BILI_SESSDATA").encrypted)
        self.assertEqual(self.client.delete("/api/login").status_code, 200)
        self.assertEqual(cloud.read_settings()["BILI_SESSDATA"], "")
        self.assertEqual(self.client.post("/api/login/qrcode/poll", json={"key": key}).status_code, 403)

    def test_expired_admin_session_cannot_modify_cookie_or_queue_tasks(self):
        self.owner()
        with db.session_scope() as session:
            session.query(cloud.OwnerSession).update({"expires_ts": 1})
        for path in ("/api/login/qrcode", "/api/login/qrcode/poll", "/api/crawl", "/api/llm/test"):
            self.assertEqual(self.client.post(path, json={}).status_code, 401)
        self.assertEqual(self.client.delete("/api/login").status_code, 401)

    def test_public_responses_and_file_paths_do_not_expose_private_runtime_data(self):
        cloud.save_settings({"BILI_SESSDATA": "private-cookie"})
        cloud.save_state("crawl", {"login_required": True, "uppers": {"1000": {"last_error": "private-operation-error"}}})
        cloud.enqueue("analyze", "1000")
        status = self.client.get("/api/status")
        self.assertNotIn("login_required", status.json())
        self.assertNotIn("private-operation-error", status.text)
        research = self.client.get("/api/research")
        self.assertEqual(research.json()["cloud_jobs"], [])
        self.assertEqual(research.json()["evidence_jobs"], [])
        self.assertEqual(status.headers["cache-control"], "no-store")
        for path in ("/.env", "/server/data.db", "/outputs/cloud-admin-20261013/.env"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_remote_database_tls_checks_hostname_and_bili_cookie_is_domain_scoped(self):
        options = db.connection_options("postgresql+psycopg://fixture:fixture@db.example.test/fixture")
        self.assertEqual(options["sslmode"], "verify-full")
        self.assertTrue(os.path.isfile(options["sslrootcert"]))
        import bili_api
        import bili_login
        with httpx.Client(cookies=bili_api.scoped_cookies({"SESSDATA": "cookie-fixture"})) as client:
            self.assertIn("cookie-fixture", client.build_request("GET", "https://api.bilibili.com/").headers.get("cookie", ""))
            self.assertNotIn("cookie-fixture", client.build_request("GET", "https://outside.example/").headers.get("cookie", ""))
        for url in ("http://passport.bilibili.com/", "https://bilibili.com.evil.test/", "https://evil.test/", "https://user:pass@passport.bilibili.com/", "https://127.0.0.1/"):
            with self.assertRaises(ValueError):
                bili_login.validate_login_url(url)

    def test_upserts_preserve_subtitle_and_image_recognition(self):
        db.upsert_videos([{"bvid": "av1", "uid": "1000", "title": "first", "subtitle": "retained", "image_desc": "retained"}])
        db.upsert_videos([{"bvid": "av1", "uid": "1000", "title": "second"}])
        with db.session_scope() as session:
            row = session.get(db.Video, "av1")
            self.assertEqual((row.title, row.subtitle, row.image_desc), ("second", "retained", "retained"))

    def test_finance_ids_provenance_and_transaction_rollback_on_postgres(self):
        sentence = "半导体设备订单继续增长，需求保持强劲。"
        source = {"kind": "video", "source_id": "av1", "pub_ts": 100, "title": "fixture", "url": "https://www.bilibili.com/video/av1", "text": sentence, "subtitle_segments": None}
        result = {"claims": [{"topic": "半导体", "subject": "半导体设备", "kind": "产业判断", "claim": "作者认为设备订单增长", "quote": sentence}]}
        with patch.object(analysis, "call_json", return_value=(result, "fixture-model")):
            analysis.analyze_source("1000", source)
        claim = analysis.list_claims("1000")[0]
        self.assertGreater(claim["id"], 0)
        self.assertEqual(analysis.get_claim(claim["id"])["quote"], sentence)
        with self.assertRaises(RuntimeError):
            with analysis.connect() as connection:
                connection.execute("INSERT INTO finance_attempts VALUES(?,?,?,?)", ("rollback", 1, "fixture", 1))
                raise RuntimeError("rollback")
        with analysis.connect() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM finance_attempts WHERE input_hash=?", ("rollback",)).fetchone())

    def test_queue_deduplicates_parallel_calls_and_cold_start_preserves_running(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: cloud.enqueue("analyze", "1000", {"per_upper": 1}), range(4)))
        self.assertEqual(sum(r["ok"] for r in results), 1)
        job = cloud.take_job()
        self.assertEqual(job["uid"], "1000")
        self.assertIsNone(cloud.take_job())
        with analysis.connect() as connection:
            connection.execute("INSERT INTO finance_runs(uid,started_ts,status) VALUES(?,?,'running')", ("1000", 100))
        analysis.init_store()
        self.assertEqual(analysis.status()["last_run"]["status"], "running")
        cloud.finish_job(job["id"])
        self.assertEqual(cloud.jobs()[0]["status"], "complete")

    def test_cloud_test_requires_one_author_and_queues_one_item(self):
        self.owner()
        with patch("llm_client.is_configured", return_value=True):
            self.assertEqual(self.client.post("/api/research/analyze", json={}).status_code, 400)
            result = self.client.post("/api/research/analyze", json={"uid": "1000"})
            self.assertEqual(result.status_code, 200)
        job = cloud.take_job()
        self.assertEqual(job["payload"]["per_upper"], 1)


if __name__ == "__main__":
    unittest.main()
