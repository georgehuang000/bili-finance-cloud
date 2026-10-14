"""Durable cloud settings, owner sessions and bounded work queue.

The Vercel function only reads data and queues long jobs. A GitHub runner owns
the queue lease and performs collection/AI calls. No key is returned by APIs.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from sqlalchemy import Column, Integer, String, Text, select, update
from sqlalchemy import inspect

import config
import db
from report_schedule import report_date

COOKIE = "__Host-bili_owner"
SECRET_KEYS = {
    "BILI_SESSDATA", "BILI_JCT",
    "BILI_BUVID3", "BILI_BUVID4", "BILI_DEDEUSERID",
}


class Setting(db.Base):
    __tablename__ = "cloud_settings"
    key = Column(String, primary_key=True)
    encrypted = Column(Text, nullable=False)


class RuntimeState(db.Base):
    __tablename__ = "cloud_state"
    key = Column(String, primary_key=True)
    data = Column(Text, nullable=False)


class OwnerSession(db.Base):
    __tablename__ = "cloud_owner_sessions"
    digest = Column(String, primary_key=True)
    expires_ts = Column(Integer, nullable=False)


class OwnerFactor(db.Base):
    # Separate grants: the collection worker must never read or modify MFA.
    __tablename__ = "cloud_owner_factors"
    key = Column(String, primary_key=True)
    encrypted = Column(Text, nullable=False)


class LoginChallenge(db.Base):
    __tablename__ = "cloud_login_challenges"
    digest = Column(String, primary_key=True)
    owner_digest = Column(String, nullable=False)
    expires_ts = Column(Integer, nullable=False)
    consumed = Column(Integer, nullable=False, default=0)
    lease_ts = Column(Integer, nullable=False, default=0)


class LoginWindow(db.Base):
    __tablename__ = "cloud_login_windows"
    key = Column(String, primary_key=True)
    attempts = Column(Integer, nullable=False)
    window_ts = Column(Integer, nullable=False)


class Job(db.Base):
    __tablename__ = "cloud_jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String, nullable=False)
    uid = Column(String)
    payload = Column(Text, nullable=False)
    status = Column(String, nullable=False, index=True)
    created_ts = Column(Integer, nullable=False)
    started_ts = Column(Integer)
    finished_ts = Column(Integer)
    message = Column(Text)


def ready():
    if not db.IS_POSTGRES:
        return False
    try:
        required = {"uppers", "videos", "dynamics", "summaries", "finance_sources", "finance_claims", "finance_attempts", "finance_runs", "finance_evidence", "finance_evidence_jobs", "cloud_settings", "cloud_state", "cloud_jobs", "cloud_owner_sessions", "cloud_login_windows", "cloud_login_challenges"}
        required.add("cloud_owner_factors")
        return required.issubset(set(inspect(db.engine).get_table_names()))
    except Exception:
        return False


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def _password_matches(password):
    try:
        algorithm, salt, expected = config.ADMIN_PASSWORD_HASH.split("$")
        if algorithm != "scrypt" or len(password) > 512:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def login(password, request, code="", recovery_code=""):
    from owner_auth import login as owner_login
    return owner_login(password, request, code, recovery_code)


def is_owner(request):
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 128 or not ready():
        return False
    with db.session_scope() as session:
        row = session.get(OwnerSession, hashlib.sha256(token.encode()).hexdigest())
        return bool(row and row.expires_ts > time.time())


def logout(request):
    with db.session_scope() as session:
        digest = hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
        row = session.get(OwnerSession, digest)
        if row:
            session.delete(row)
        session.execute(update(LoginChallenge).where(LoginChallenge.owner_digest == digest).values(expires_ts=0))


def _owner_digest(request):
    return hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()


def register_qr(key, request):
    now = int(time.time())
    digest = hashlib.sha256(key.encode()).hexdigest()
    owner = _owner_digest(request)
    with db.session_scope() as session:
        session.query(LoginChallenge).filter(LoginChallenge.expires_ts < now).delete()
        session.execute(update(LoginChallenge).where(LoginChallenge.owner_digest == owner, LoginChallenge.consumed == 0).values(expires_ts=0))
        session.add(LoginChallenge(digest=digest, owner_digest=owner, expires_ts=now + 180, consumed=0, lease_ts=0))


def begin_qr_poll(key, request):
    now = int(time.time())
    with db.session_scope() as session:
        row = session.execute(select(LoginChallenge).where(LoginChallenge.digest == hashlib.sha256(key.encode()).hexdigest()).with_for_update()).scalar_one_or_none()
        if not row or row.owner_digest != _owner_digest(request) or row.expires_ts <= now:
            return "invalid"
        if row.consumed:
            return "complete"
        if row.lease_ts > now:
            return "busy"
        row.lease_ts = now + 120
        return "ready"


def end_qr_poll(key, request):
    with db.session_scope() as session:
        session.execute(update(LoginChallenge).where(LoginChallenge.digest == hashlib.sha256(key.encode()).hexdigest(), LoginChallenge.owner_digest == _owner_digest(request)).values(lease_ts=0))


def complete_qr_cookies(key, request, values):
    if set(values) != SECRET_KEYS or any(not isinstance(value, str) or len(value) > 8192 or "\r" in value or "\n" in value for value in values.values()):
        raise ValueError("登录凭据格式不正确")
    cipher = _cipher()
    with db.session_scope() as session:
        row = session.execute(select(LoginChallenge).where(LoginChallenge.digest == hashlib.sha256(key.encode()).hexdigest()).with_for_update()).scalar_one_or_none()
        owner = session.get(OwnerSession, _owner_digest(request))
        if not row or not owner or owner.expires_ts <= time.time() or row.owner_digest != owner.digest or row.expires_ts <= time.time() or row.consumed:
            raise ValueError("扫码会话已失效，请重新生成二维码")
        for name, value in values.items():
            encrypted = cipher.encrypt(value.encode()).decode()
            session.execute(db.upsert_insert(Setting).values(key=name, encrypted=encrypted).on_conflict_do_update(index_elements=[Setting.key], set_={"encrypted": encrypted}))
        row.consumed, row.lease_ts = 1, 0
    return list(values)


def clear_bili_credentials():
    save_settings({key: "" for key in SECRET_KEYS})
    with db.session_scope() as session:
        session.execute(update(LoginChallenge).values(expires_ts=0))
    refresh_settings()
    state = load_state("crawl")
    state["login_required"] = True
    save_state("crawl", state)


def same_origin(request):
    # Browser fetch metadata cannot be set by cross-site forms. Keep sibling
    # origins blocked even when a request carries forwarded headers.
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True  # Non-browser clients still need the owner session/password.
    scheme, host = request.url.scheme, request.url.netloc
    # Vercel supplies the original host/protocol before forwarding to the
    # service's private port. Never trust these headers on a generic server.
    local_proxy = os.environ.get("BILI_LOCAL_VERCEL_PROXY") == "1" and request.url.hostname in ("localhost", "127.0.0.1") and request.client and request.client.host in ("127.0.0.1", "::1")
    if os.environ.get("VERCEL") == "1" or local_proxy:
        forwarded_host = request.headers.get("x-forwarded-host")
        forwarded_proto = request.headers.get("x-forwarded-proto")
        # CLI 63 Services uses a private Host and exposes its public localhost
        # address in x-vercel-deployment-url instead. This exception applies
        # only to local development and a loopback service connection.
        if not forwarded_host and (local_proxy or os.environ.get("VERCEL_ENV") == "development") and request.url.hostname in ("localhost", "127.0.0.1"):
            local_host = request.headers.get("x-vercel-deployment-url", "")
            try:
                if urlsplit(f"http://{local_host}").hostname in ("localhost", "127.0.0.1") and request.client and request.client.host in ("127.0.0.1", "::1"):
                    forwarded_host, forwarded_proto = local_host, "http"
            except ValueError:
                return False
        if forwarded_host and forwarded_proto:
            if forwarded_proto not in ("http", "https") or any(c in forwarded_host for c in ",/@\\ \r\n"):
                return False
            host, scheme = forwarded_host, forwarded_proto
    try:
        supplied, target = urlsplit(origin), urlsplit(f"{scheme}://{host}")
        if supplied.scheme not in ("http", "https") or supplied.username or supplied.password or supplied.path or supplied.query or supplied.fragment:
            return False
        port = lambda url: url.port or (443 if url.scheme == "https" else 80)
        return (supplied.scheme, supplied.hostname, port(supplied)) == (target.scheme, target.hostname, port(target))
    except ValueError:
        return False


def _cipher():
    from cryptography.fernet import Fernet
    if not config.CREDENTIAL_ENCRYPTION_KEY:
        raise ValueError("云端尚未配置凭据加密密钥")
    return Fernet(config.CREDENTIAL_ENCRYPTION_KEY.encode())


def read_settings():
    with db.session_scope() as session:
        rows = session.execute(select(Setting).where(Setting.key.in_(SECRET_KEYS))).scalars().all()
        if not rows:
            return {}
        cipher = _cipher()
        return {row.key: cipher.decrypt(row.encrypted.encode()).decode() for row in rows}


def save_settings(updates):
    if set(updates) - SECRET_KEYS:
        raise ValueError("不支持的云端配置项")
    if any(not isinstance(value, str) or len(value) > 8192 or "\r" in value or "\n" in value for value in updates.values()):
        raise ValueError("登录凭据格式不正确")
    cipher = _cipher()
    with db.session_scope() as session:
        for key, value in updates.items():
            encrypted = cipher.encrypt(str(value).encode()).decode()
            session.execute(db.upsert_insert(Setting).values(key=key, encrypted=encrypted).on_conflict_do_update(index_elements=[Setting.key], set_={"encrypted": encrypted}))
    return list(updates)


def refresh_settings():
    import bili_api
    values = read_settings()
    changed = False
    for key, value in values.items():
        if getattr(config, key, None) != value:
            setattr(config, key, value)
            changed = True
    if changed:
        bili_api.invalidate_cookies()
        bili_api.invalidate_login_cache()


def save_state(key, data):
    encoded = json.dumps(data, ensure_ascii=False)
    with db.session_scope() as session:
        session.execute(db.upsert_insert(RuntimeState).values(key=key, data=encoded).on_conflict_do_update(index_elements=[RuntimeState.key], set_={"data": encoded}))


def load_state(key):
    with db.session_scope() as session:
        row = session.get(RuntimeState, key)
        return json.loads(row.data) if row else {}


def enqueue(kind, uid=None, payload=None):
    payload = payload or {}
    now = int(time.time())
    with db.session_scope() as session:
        # Serialize enqueue deduplication across function instances.
        from sqlalchemy import text
        session.execute(text("SELECT pg_advisory_xact_lock(82001432)"))
        existing = session.execute(select(Job).where(Job.kind == kind, Job.uid == uid, Job.status.in_(["queued", "running"]))).scalars().first()
        if existing:
            return {"ok": False, "job_id": existing.id, "message": "已有任务排队或运行中"}
        job = Job(kind=kind, uid=uid, payload=json.dumps(payload), status="queued", created_ts=now)
        session.add(job)
        session.flush()
        return {"ok": True, "job_id": job.id, "message": "已加入云端队列，等待执行"}


def enqueue_scheduled(period):
    if period not in ("morning", "evening"):
        raise ValueError("定时日报必须为早报或晚报")
    date = report_date()
    with db.session_scope() as session:
        from sqlalchemy import text
        session.execute(text("SELECT pg_advisory_xact_lock(82001432)"))
        report = session.execute(select(db.Summary.id).where(
            db.Summary.date == date, db.Summary.period == period).limit(1)).scalar()
        if report is not None:
            return {"ok": False, "job_id": None, "message": "本期日报已生成，跳过重复任务"}
        jobs = session.execute(select(Job).where(
            Job.kind == "scheduled", Job.status.in_(["queued", "running", "complete"]))).scalars()
        for existing in jobs:
            payload = json.loads(existing.payload)
            if payload.get("date") == date and payload.get("period") == period:
                return {"ok": False, "job_id": existing.id, "message": "本期日报已提交，跳过重复任务"}
        job = Job(kind="scheduled", uid=None,
                  payload=json.dumps({"date": date, "period": period}),
                  status="queued", created_ts=int(time.time()))
        session.add(job)
        session.flush()
        return {"ok": True, "job_id": job.id, "message": "已提交定时采集与日报任务"}


def take_job():
    now = int(time.time())
    with db.session_scope() as session:
        session.execute(update(Job).where(Job.status == "running", Job.started_ts < now - 3 * 3600).values(status="interrupted", finished_ts=now, message="执行超时，请手动重新发起"))
        row = session.execute(select(Job).where(Job.status == "queued").order_by(Job.id).with_for_update(skip_locked=True).limit(1)).scalars().first()
        if not row:
            return None
        row.status, row.started_ts = "running", now
        return {"id": row.id, "kind": row.kind, "uid": row.uid, "payload": json.loads(row.payload)}


def finish_job(job_id, message="执行完成", failed=False):
    with db.session_scope() as session:
        session.execute(update(Job).where(Job.id == job_id).values(status="failed" if failed else "complete", finished_ts=int(time.time()), message=message))


def jobs():
    with db.session_scope() as session:
        rows = session.execute(select(Job).order_by(Job.id.desc()).limit(10)).scalars().all()
        return [{key: getattr(row, key) for key in ("id", "kind", "uid", "status", "created_ts", "started_ts", "finished_ts", "message")} for row in rows]
