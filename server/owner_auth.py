"""Single-owner MFA and persistent requester limits, independent of Bili cookies."""
import hashlib
import ipaddress
import json
import os
import re
import secrets
import time

import pyotp
from sqlalchemy import select, update

import cloud_runtime as cloud
import db

INVALID = "管理密码或验证代码不正确；已使用的动态码请等待下一组"
LIMITED = "当前网络尝试次数过多，请15分钟后重试，或换一个网络登录"
UNAVAILABLE = "无法确认登录请求来源，请稍后重试"


def source_key(request):
    # Vercel overwrites these headers. On generic servers use only the socket
    # peer, never arbitrary client-supplied forwarding headers.
    if os.environ.get("VERCEL") == "1" and os.environ.get("VERCEL_ENV") != "development":
        address = request.headers.get("x-vercel-forwarded-for") or request.headers.get("x-forwarded-for", "")
        try:
            address = ipaddress.ip_address(address)
        except ValueError:
            return None
    else:
        if not request.client:
            return None
        address = request.client.host
        try:
            address = ipaddress.ip_address(address)
        except ValueError:
            # TestClient/local Unix peers can have a non-IP peer name. This is
            # the server's socket metadata, not a browser-controlled header.
            pass
    if isinstance(address, ipaddress.IPv6Address):
        address = address.ipv4_mapped or ipaddress.ip_network(f"{address}/64", strict=False)
    return "source:" + hashlib.sha256(str(address).encode()).hexdigest()


def _window(session, key, now):
    session.query(cloud.LoginWindow).filter(cloud.LoginWindow.window_ts < now - 900).delete()
    session.execute(db.upsert_insert(cloud.LoginWindow).values(key=key, attempts=0, window_ts=now).on_conflict_do_nothing())
    row = session.execute(select(cloud.LoginWindow).where(cloud.LoginWindow.key == key).with_for_update()).scalar_one()
    if now - row.window_ts >= 900:
        row.attempts, row.window_ts = 0, now
    if row.attempts >= 10:
        return None
    row.attempts += 1
    return row


def _factor(session):
    empty = cloud._cipher().encrypt(b"{}").decode()
    session.execute(db.upsert_insert(cloud.OwnerFactor).values(key="owner", encrypted=empty).on_conflict_do_nothing())
    row = session.execute(select(cloud.OwnerFactor).where(cloud.OwnerFactor.key == "owner").with_for_update()).scalar_one()
    return row, json.loads(cloud._cipher().decrypt(row.encrypted.encode()))


def _save(row, data):
    row.encrypted = cloud._cipher().encrypt(json.dumps(data).encode()).decode()


def status():
    with db.session_scope() as session:
        row = session.get(cloud.OwnerFactor, "owner")
        data = json.loads(cloud._cipher().decrypt(row.encrypted.encode())) if row else {}
        return {"mfa_enabled": bool(data.get("secret"))}


def _otp_step(secret, code, now, last=-1):
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    totp = pyotp.TOTP(secret)
    for step in (now // 30, now // 30 - 1, now // 30 + 1):
        if step > last and secrets.compare_digest(totp.at(step * 30), code):
            return step
    return None


def _proof(data, code, recovery_code, now):
    if code and recovery_code:
        return False, False
    if recovery_code:
        normalized = recovery_code.replace("-", "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", normalized):
            return False, False
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        for stored in data.get("recovery", []):
            if secrets.compare_digest(stored, digest):
                data["recovery"].remove(stored)
                return True, True
        return False, False
    step = _otp_step(data["secret"], code, now, data.get("last_step", -1))
    if step is None:
        return False, False
    data["last_step"] = step
    return True, False


def _revoke(session):
    session.query(cloud.OwnerSession).delete()
    session.execute(update(cloud.LoginChallenge).values(expires_ts=0))


def _issue(session, now):
    session.query(cloud.OwnerSession).filter(cloud.OwnerSession.expires_ts <= now).delete()
    token = secrets.token_urlsafe(32)
    session.add(cloud.OwnerSession(digest=hashlib.sha256(token.encode()).hexdigest(), expires_ts=now + 8 * 3600))
    return token


def login(password, request, code="", recovery_code=""):
    now, key = int(time.time()), source_key(request)
    if not key:
        return None, UNAVAILABLE
    with db.session_scope() as session:
        window = _window(session, key, now)
        if not window:
            return None, LIMITED
        if not cloud._password_matches(password):
            return None, INVALID
        row, data = _factor(session)
        if data.get("secret"):
            valid, recovered = _proof(data, code, recovery_code, now)
            if not valid:
                return None, INVALID
            if recovered:
                _revoke(session)
                data.pop("pending", None)
            _save(row, data)
        window.attempts = 0
        return _issue(session, now), None


def _live_owner(session, request, now):
    row = session.execute(select(cloud.OwnerSession).where(cloud.OwnerSession.digest == cloud._owner_digest(request)).with_for_update()).scalar_one_or_none()
    return bool(row and row.expires_ts > now)


def begin_setup(password, request, code="", recovery_code=""):
    now, key = int(time.time()), source_key(request)
    if not key:
        return None, UNAVAILABLE
    with db.session_scope() as session:
        window = _window(session, key, now)
        if not window:
            return None, LIMITED
        if not cloud._password_matches(password):
            return None, INVALID
        row, data = _factor(session)
        if not _live_owner(session, request, now):
            return None, "管理会话已失效，请重新登录"
        if data.get("secret") and not _proof(data, code, recovery_code, now)[0]:
            return None, INVALID
        secret = pyotp.random_base32()
        recovery = [secrets.token_hex(16) for _ in range(10)]
        data["pending"] = {"secret": secret, "owner": cloud._owner_digest(request), "expires": now + 600,
                           "recovery": [hashlib.sha256(value.encode()).hexdigest() for value in recovery]}
        _save(row, data)
        window.attempts = 0
    from bili_login import _qr_data_uri
    uri = pyotp.TOTP(secret).provisioning_uri(name="管理员", issuer_name="财经哨站")
    return {"qr_image": _qr_data_uri(uri), "manual_key": secret, "expires_in": 600,
            "recovery_codes": ["-".join(value[i:i + 8] for i in range(0, 32, 8)) for value in recovery]}, None


def confirm_setup(password, request, code):
    now, key = int(time.time()), source_key(request)
    if not key:
        return None, UNAVAILABLE
    with db.session_scope() as session:
        window = _window(session, key, now)
        if not window:
            return None, LIMITED
        if not cloud._password_matches(password):
            return None, INVALID
        row, data = _factor(session)
        pending = data.get("pending", {})
        if not _live_owner(session, request, now) or pending.get("owner") != cloud._owner_digest(request) or pending.get("expires", 0) <= now:
            return None, "绑定已失效，请重新开始"
        step = _otp_step(pending["secret"], code, now)
        if step is None:
            return None, INVALID
        _save(row, {"secret": pending["secret"], "recovery": pending["recovery"], "last_step": step})
        _revoke(session)
        window.attempts = 0
        return _issue(session, now), None
