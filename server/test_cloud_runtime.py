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
import pyotp

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

    def test_new_subscription_queues_history_and_only_owner_can_backfill(self):
        self.assertEqual(self.client.post("/api/crawl", json={"uid": "1000", "full_history": True}).status_code, 401)
        self.owner()
        with patch.object(config, "CLOUD_TEST_MODE", False), patch("bili_api.fetch_user_card", new=AsyncMock(return_value={"name": "新作者"})):
            self.assertEqual(self.client.post("/api/uppers", json={"uid": "2000", "name": "新作者"}).status_code, 200)
        job = cloud.take_job()
        self.assertEqual(job["uid"], "2000")
        self.assertEqual(job["payload"], {"full_history": True})
        self.assertEqual(self.client.post("/api/crawl", json={"full_history": True}).status_code, 400)
        self.assertEqual(self.client.post("/api/crawl", json={"uid": "1000", "full_history": "true"}).status_code, 422)
        self.assertEqual(self.client.post("/api/crawl", json={"uid": "1000", "full_history": True}).status_code, 200)
        job = cloud.take_job()
        self.assertEqual(job["payload"], {"full_history": True})

    def test_worker_job_message_distinguishes_partial_history_from_full_completion(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import cloud_worker
        job = {"id": 99, "kind": "crawl", "uid": "1000", "payload": {}}
        with patch("sys.argv", ["cloud_worker.py"]), \
             patch.object(cloud, "ready", return_value=True), patch.object(cloud, "load_state", return_value={}), \
             patch.object(cloud, "take_job", side_effect=[job, None]), \
             patch.object(cloud_worker, "execute", return_value=[]), \
             patch.dict(cloud_worker.crawler_runner.STATE, {"uppers": {"1000": {"history": {"status": "limited"}}}}), \
             patch.object(cloud_worker.crawler_runner, "_save_state"), patch.object(cloud, "finish_job") as finish:
            cloud_worker.main()
        self.assertIn("尚未核全", finish.call_args.args[1])

    def test_persistent_login_limits_do_not_lock_another_source(self):
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}):
            attacker = {"x-vercel-forwarded-for": "192.0.2.10"}
            owner = {"x-vercel-forwarded-for": "192.0.2.20"}
            for _ in range(10):
                self.assertEqual(self.client.post("/api/auth/login", headers=attacker, json={"password": "wrong"}).status_code, 401)
            # A different function/client instance shares the same persisted cap.
            other = TestClient(main.app, base_url="https://testserver")
            blocked = other.post("/api/auth/login", headers=attacker, json={"password": "fixture-password"})
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(self.client.post("/api/auth/login", headers=owner, json={"password": "fixture-password"}).status_code, 200)
            with patch("owner_auth.time.time", return_value=time.time() + 901):
                self.assertEqual(other.post("/api/auth/login", headers=attacker, json={"password": "fixture-password"}).status_code, 200)

    def test_source_headers_cannot_bypass_limits_and_ipv6_is_canonical(self):
        import owner_auth as auth
        request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"), headers={"x-vercel-forwarded-for": "198.51.100.10"})
        with patch.dict(os.environ, {"VERCEL": "0"}):
            first = auth.source_key(request)
            request.headers["x-vercel-forwarded-for"] = "198.51.100.11"
            self.assertEqual(auth.source_key(request), first)
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}):
            request.headers["x-vercel-forwarded-for"] = "2001:db8::1"
            first = auth.source_key(request)
            for address in ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::2"):
                request.headers["x-vercel-forwarded-for"] = address
                self.assertEqual(auth.source_key(request), first)
            for address in ("192.0.2.1, 192.0.2.2", "invalid", " 192.0.2.1"):
                request.headers["x-vercel-forwarded-for"] = address
                self.assertIsNone(auth.source_key(request))
            request.headers["x-vercel-forwarded-for"] = "::ffff:192.0.2.10"
            mapped = auth.source_key(request)
            request.headers["x-vercel-forwarded-for"] = "192.0.2.10"
            self.assertEqual(auth.source_key(request), mapped)

    def enroll(self):
        self.owner()
        result = self.client.post("/api/auth/mfa/setup", json={"password": "fixture-password"})
        self.assertEqual(result.status_code, 200)
        return result.json()

    def activate(self, setup):
        code = pyotp.TOTP(setup["manual_key"]).now()
        result = self.client.post("/api/auth/mfa/confirm", json={"password": "fixture-password", "code": code})
        self.assertEqual(result.status_code, 200)
        return code

    def test_mfa_pending_is_owner_bound_expiring_and_requires_password(self):
        self.assertEqual(self.client.post("/api/auth/mfa/setup", json={"password": "fixture-password"}).status_code, 401)
        setup = self.enroll()
        self.assertFalse(self.client.get("/api/auth/session").json()["mfa_enabled"])
        self.assertEqual(self.client.post("/api/auth/mfa/setup", json={"password": "wrong"}).status_code, 401)
        second = TestClient(main.app, base_url="https://testserver")
        self.assertEqual(second.post("/api/auth/login", json={"password": "fixture-password"}).status_code, 200)
        code = pyotp.TOTP(setup["manual_key"]).now()
        self.assertEqual(second.post("/api/auth/mfa/confirm", json={"password": "fixture-password", "code": code}).status_code, 401)
        with patch("owner_auth.time.time", return_value=time.time() + 601):
            self.assertEqual(self.client.post("/api/auth/mfa/confirm", json={"password": "fixture-password", "code": code}).status_code, 401)
        self.assertFalse(self.client.get("/api/auth/session").json()["mfa_enabled"])
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/mfa/confirm", json={"password": "fixture-password", "code": code}).status_code, 401)

    def test_mfa_activation_revokes_sessions_qr_and_secrets_are_private(self):
        setup = self.enroll()
        original_cookie = self.client.cookies.get(cloud.COOKIE)
        request = SimpleNamespace(cookies={cloud.COOKIE: original_cookie})
        cloud.register_qr("fixture-challenge-key", request)
        self.activate(setup)
        self.assertTrue(self.client.get("/api/auth/session").json()["mfa_enabled"])
        self.assertFalse(cloud.is_owner(request))
        self.assertEqual(cloud.begin_qr_poll("fixture-challenge-key", request), "invalid")
        with db.session_scope() as session:
            encrypted = session.get(cloud.OwnerFactor, "owner").encrypted
            self.assertNotIn(setup["manual_key"], encrypted)
            for value in setup["recovery_codes"]:
                self.assertNotIn(value.replace("-", ""), cloud._cipher().decrypt(encrypted.encode()).decode())
        self.assertEqual(cloud.read_settings(), {})
        public = TestClient(main.app, base_url="https://testserver")
        self.assertEqual(set(public.get("/api/auth/session").json()), {"cloud", "can_manage", "test_mode", "mfa_enabled"})
        self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password"}).status_code, 401)
        self.assertEqual(public.post("/api/auth/mfa/setup", json={"password": "fixture-password"}).status_code, 401)

    def test_mfa_replay_unicode_and_concurrent_recovery_are_rejected(self):
        setup = self.enroll()
        used_code = self.activate(setup)
        public = TestClient(main.app, base_url="https://testserver")
        for code in (used_code, "１２３４５６"):
            self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password", "code": code}).status_code, 401)
        future = int(time.time()) + 60
        with patch("owner_auth.time.time", return_value=future):
            new_code = pyotp.TOTP(setup["manual_key"]).at(future)
            self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password", "code": new_code}).status_code, 200)
            self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password", "code": new_code}).status_code, 401)
        # One recovery code may mint exactly one session, even concurrently.
        def redeem(_):
            client = TestClient(main.app, base_url="https://testserver")
            return client.post("/api/auth/login", json={"password": "fixture-password", "recovery_code": setup["recovery_codes"][0]}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(redeem, range(2)))
        self.assertEqual(sorted(results), [200, 401])
        self.assertFalse(self.client.get("/api/auth/session").json()["can_manage"])

    def test_mfa_replacement_requires_old_factor_and_keeps_it_until_confirm(self):
        setup = self.enroll()
        self.activate(setup)
        self.assertEqual(self.client.post("/api/auth/mfa/setup", json={"password": "fixture-password"}).status_code, 401)
        result = self.client.post("/api/auth/mfa/setup", json={"password": "fixture-password", "recovery_code": setup["recovery_codes"][0]})
        self.assertEqual(result.status_code, 200)
        replacement = result.json()
        with db.session_scope() as session:
            data = json.loads(cloud._cipher().decrypt(session.get(cloud.OwnerFactor, "owner").encrypted.encode()))
            self.assertEqual(data["secret"], setup["manual_key"])
        self.activate(replacement)
        public = TestClient(main.app, base_url="https://testserver")
        self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password", "recovery_code": setup["recovery_codes"][1]}).status_code, 401)
        self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password", "recovery_code": replacement["recovery_codes"][0]}).status_code, 200)

    def test_mfa_migration_is_idempotent_and_worker_has_no_factor_grants(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("mfa_migration", Path(__file__).resolve().parents[1] / "tools/migrate_owner_auth.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with db.engine.begin() as connection:
            connection.exec_driver_sql("CREATE ROLE bili_cloud_api NOLOGIN")
            connection.exec_driver_sql("CREATE ROLE bili_cloud_worker NOLOGIN")
        try:
            migration.migrate()
            migration.migrate()
            with db.engine.begin() as connection:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    self.assertTrue(connection.scalar(text("SELECT has_table_privilege('bili_cloud_api','cloud_owner_factors',:privilege)"), {"privilege": privilege}))
                    self.assertFalse(connection.scalar(text("SELECT has_table_privilege('bili_cloud_worker','cloud_owner_factors',:privilege)"), {"privilege": privilege}))
        finally:
            with db.engine.begin() as connection:
                connection.exec_driver_sql("DROP OWNED BY bili_cloud_api, bili_cloud_worker")
                connection.exec_driver_sql("DROP ROLE bili_cloud_api, bili_cloud_worker")

    def test_shared_network_lockout_cannot_block_binding_or_valid_recovery(self):
        import owner_auth as auth
        self.owner()
        public = TestClient(main.app, base_url="https://testserver")
        for _ in range(10):
            self.assertEqual(public.post("/api/auth/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password"}).status_code, 429)
        setup = self.client.post("/api/auth/mfa/setup", json={"password": "fixture-password"}).json()
        self.activate(setup)
        with patch.object(cloud, "_password_matches", wraps=cloud._password_matches) as verifier:
            invalid = public.post("/api/auth/login", json={"password": "fixture-password", "recovery_code": "0" * 32})
            self.assertEqual(invalid.status_code, 429)
            verifier.assert_not_called()
        recovery = setup["recovery_codes"][0]
        # A wrong password does not consume or discard the recovery proof.
        self.assertEqual(public.post("/api/auth/login", json={"password": "wrong", "recovery_code": recovery}).status_code, 401)
        self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password", "recovery_code": recovery}).status_code, 200)
        self.assertEqual(public.post("/api/auth/login", json={"password": "fixture-password", "recovery_code": recovery}).status_code, 429)
        self.assertFalse(self.client.get("/api/auth/session").json()["can_manage"])

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
        import certifi
        from pathlib import Path
        self.assertEqual(Path(options["sslrootcert"]).read_bytes(), Path(certifi.where()).read_bytes())
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

    def test_scheduled_reports_deduplicate_each_beijing_date_and_period(self):
        with patch.object(cloud, "report_date", return_value="2026-10-14"):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: cloud.enqueue_scheduled("morning"), range(4)))
            self.assertEqual(sum(r["ok"] for r in results), 1)
            job = cloud.take_job()
            self.assertEqual(job["payload"], {"date": "2026-10-14", "period": "morning"})
            cloud.finish_job(job["id"], failed=True)
            self.assertTrue(cloud.enqueue_scheduled("morning")["ok"])
            db.insert_summary(date="2026-10-14", period="morning", content="fixture", model="fixture")
            self.assertFalse(cloud.enqueue_scheduled("morning")["ok"])
            self.assertTrue(cloud.enqueue_scheduled("evening")["ok"])
        with patch.object(cloud, "report_date", return_value="2026-10-15"):
            self.assertTrue(cloud.enqueue_scheduled("morning")["ok"])

    def test_scheduled_enqueue_only_never_claims_or_runs_a_job(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("vps_worker_check", Path(__file__).resolve().parents[1] / "tools/cloud_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with patch("sys.argv", ["cloud_worker.py", "--scheduled", "--period", "morning", "--enqueue-only"]), patch.dict(os.environ, {"CLOUD_SCHEDULE_ENABLED": "true"}), patch.object(config, "CLOUD_TEST_MODE", False), patch.object(cloud, "enqueue_scheduled", return_value={"ok": True, "message": "fixture"}) as enqueue, patch.object(cloud, "take_job") as claim:
            worker.main()
            enqueue.assert_called_once_with("morning")
            claim.assert_not_called()

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
