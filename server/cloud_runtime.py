"""Durable cloud settings, owner sessions and bounded work queue.

The Vercel function only reads data and queues long jobs. A GitHub runner owns
the queue lease and performs collection/AI calls. No key is returned by APIs.
"""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

from sqlalchemy import Column, Integer, String, Text, select, update

import config
import db

COOKIE = "bili_owner"
SECRET_KEYS = {
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "VISION_MODEL", "LLM_THINKING",
    "VISION_ENABLED", "VISION_MAX_IMAGES_PER_ROUND", "BILI_SESSDATA", "BILI_JCT",
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
    return db.IS_POSTGRES


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


def login(password):
    now = int(time.time())
    # Global persistent limit is shared by cold starts and cannot be evaded by
    # supplying forged IP headers. Owner-only site: ten attempts per 15 min.
    with db.session_scope() as session:
        session.execute(db.upsert_insert(LoginWindow).values(key="owner", attempts=0, window_ts=now).on_conflict_do_nothing())
        window = session.execute(select(LoginWindow).where(LoginWindow.key == "owner").with_for_update()).scalar_one()
        if now - window.window_ts > 900:
            window.attempts, window.window_ts = 0, now
        if window.attempts >= 10:
            return None, "尝试次数过多，请15分钟后重试"
        window.attempts += 1
        if not _password_matches(password):
            return None, "管理密码不正确"
        window.attempts = 0
        # Keep the session table bounded, including old logged-out browsers.
        session.query(OwnerSession).filter(OwnerSession.expires_ts < now).delete()
        token = secrets.token_urlsafe(32)
        session.add(OwnerSession(digest=hashlib.sha256(token.encode()).hexdigest(), expires_ts=now + 8 * 3600))
    return token, None


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


def same_origin(request):
    origin = request.headers.get("origin")
    return not origin or urlsplit(origin).netloc == request.url.netloc


def _cipher():
    from cryptography.fernet import Fernet
    if not config.CREDENTIAL_ENCRYPTION_KEY:
        raise ValueError("云端尚未配置凭据加密密钥")
    return Fernet(config.CREDENTIAL_ENCRYPTION_KEY.encode())


def read_settings():
    with db.session_scope() as session:
        rows = session.execute(select(Setting)).scalars().all()
        if not rows:
            return {}
        cipher = _cipher()
        return {row.key: cipher.decrypt(row.encrypted.encode()).decode() for row in rows}


def save_settings(updates):
    if set(updates) - SECRET_KEYS:
        raise ValueError("不支持的云端配置项")
    merged = {**read_settings(), **updates}
    if urlsplit(merged.get("LLM_BASE_URL", config.LLM_BASE_URL)).hostname != "api.deepseek.com":
        raise ValueError("云端测试仅使用 DeepSeek 官方 API")
    cipher = _cipher()
    with db.session_scope() as session:
        for key, value in updates.items():
            encrypted = cipher.encrypt(str(value).encode()).decode()
            session.execute(db.upsert_insert(Setting).values(key=key, encrypted=encrypted).on_conflict_do_update(index_elements=[Setting.key], set_={"encrypted": encrypted}))
    return list(updates)


def refresh_settings():
    import llm_client
    import bili_api
    values = read_settings()
    changed = False
    for key, value in values.items():
        if key == "VISION_ENABLED":
            value = value.lower() in ("1", "true", "yes")
        elif key == "VISION_MAX_IMAGES_PER_ROUND":
            value = int(value)
        if getattr(config, key, None) != value:
            setattr(config, key, value)
            changed = True
    if changed:
        llm_client.invalidate()
        bili_api.invalidate_cookies()


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
