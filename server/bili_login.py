# -*- coding: utf-8 -*-
"""B站扫码登录：生成二维码 → 轮询结果 → 把登录态写回 .env 并热生效。

B站官方 passport 接口（无需 buvid/WBI，独立 httpx client）：
  1. GET /x/passport-login/web/qrcode/generate
     -> data.url（二维码内容，用 B站 App 扫）+ data.qrcode_key
  2. GET /x/passport-login/web/qrcode/poll?qrcode_key=
     -> data.code: 0=已确认(响应头 Set-Cookie 带上登录态)
                   86101=未扫码  86090=已扫码待确认  86038=已过期

登录成功后：SESSDATA / bili_jct / DedeUserID / buvid3 / buvid4 写回
工作区根目录 .env（保留其它行），并热更新 config + 让 bili_api 丢弃旧
cookie，爬虫下一轮即用新登录态，无需重启进程。

安全：日志里绝不打印 cookie 值。
"""
import base64
import io
import logging
import re

import httpx
from urllib.parse import urljoin, urlsplit

import config
import env_store

logger = logging.getLogger("bili_login")

GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

# B站 cookie 名 -> .env 键名。顺序即写入顺序。
_COOKIE_TO_ENV = {
    "SESSDATA": "BILI_SESSDATA",
    "bili_jct": "BILI_JCT",
    "DedeUserID": "BILI_DEDEUSERID",
    "buvid3": "BILI_BUVID3",
    "buvid4": "BILI_BUVID4",
}

# 已成功消费过的 qrcode_key（避免重复写 .env）
_consumed: set[str] = set()


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=_HEADERS, timeout=20.0, follow_redirects=False, trust_env=False)


def validate_login_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname not in ("passport.bilibili.com", "www.bilibili.com", "api.bilibili.com") or parsed.port not in (None, 443):
        raise ValueError("登录地址不符合安全要求")
    return url


async def follow_login_jump(client, url):
    cookies = {}
    for _ in range(3):
        validate_login_url(url)
        response = await client.get(url)
        cookies.update(_extract_cookies(response))
        if not response.is_redirect:
            return cookies
        url = urljoin(url, response.headers.get("location", ""))
    raise ValueError("登录跳转次数过多")


def _qr_data_uri(text: str) -> str:
    """把二维码内容渲染成 SVG data URI（前端用 <img src> 显示，避免注入）。"""
    import qrcode
    import qrcode.image.svg

    qr = qrcode.QRCode(
        box_size=8,
        border=2,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _extract_cookies(resp: httpx.Response) -> dict:
    """从 Set-Cookie 头里挑出需要的登录态字段。"""
    out = {}
    for raw in resp.headers.get_list("set-cookie"):
        name, _, rest = raw.partition("=")
        value = rest.split(";", 1)[0].strip()
        name = name.strip()
        if name in _COOKIE_TO_ENV and value:
            out[name] = value
    return out


def _save_env(cookies: dict) -> list:
    """把 cookie 写回 .env（保留其它行与注释），返回更新的 .env 键名列表。"""
    pairs = {_COOKIE_TO_ENV[k]: v for k, v in cookies.items() if k in _COOKIE_TO_ENV}
    if not pairs:
        return []
    # 统一走 env_store：扫码登录与模型设置共用同一份"保留注释与顺序"的实现
    return env_store.save_env(config.ENV_PATH, pairs)


def _apply_runtime(cookies: dict):
    """热更新运行中的登录态：config 常量 + 丢弃 bili_api 已构建的 cookie。"""
    for ck, env_key in _COOKIE_TO_ENV.items():
        if cookies.get(ck):
            setattr(config, env_key, cookies[ck])

    import bili_api

    bili_api.invalidate_cookies()  # 下一轮请求会带上新 SESSDATA 重建 cookie
    bili_api.invalidate_login_cache()  # 登录态检测缓存作废
    bili_api.reset_risk_stats()  # 风控计数从新登录态开始重新累计

    # 清掉「登录态失效」状态，前端提示条立即消失
    try:
        import crawler_runner

        crawler_runner._record_login_state(True, source="扫码登录成功")
    except Exception as ex:  # 状态清理失败不应影响登录本身
        logger.warning("[bili_login] reset login_required failed: %s", type(ex).__name__)


async def create_qr() -> dict:
    """生成登录二维码，返回 {key, image}。image 为 SVG data URI。"""
    async with _new_client() as client:
        resp = await client.get(GEN_URL)
        body = resp.json()
        data = body.get("data") or {}
        url = data.get("url") or ""
        key = data.get("qrcode_key") or ""
        if body.get("code") != 0 or not url or not key:
            raise RuntimeError("二维码生成失败")
        validate_login_url(url)
        return {"key": key, "image": _qr_data_uri(url)}


async def poll_qr(key: str, store_cookies=None) -> dict:
    """轮询扫码结果。成功后写回 .env 并热生效。

    返回 {status, message, account?, saved?}
    status: waiting(未扫码) / scanned(已扫码待确认) / confirmed(成功) / expired(已过期)
    """
    async with _new_client() as client:
        resp = await client.get(POLL_URL, params={"qrcode_key": key})
        body = resp.json()
        data = body.get("data") or {}
        inner = data.get("code")

        if body.get("code") != 0:
            return {"status": "error", "message": "扫码检查失败，请稍后重试"}

        if inner == 86038:
            return {"status": "expired", "message": "二维码已过期，请刷新后重新扫描"}
        if inner == 86090:
            return {"status": "scanned", "message": "已扫描，请在手机上确认登录"}
        if inner != 0:
            return {"status": "waiting", "message": "等待扫码…"}

        # ── 登录成功 ──
        cookies = _extract_cookies(resp)
        # 部分流程需要再访问一次 data.url 才会下发完整 cookie
        jump = data.get("url") or ""
        if jump:
            try:
                cookies.update(await follow_login_jump(client, jump))
            except Exception as ex:
                logger.warning("[bili_login] follow jump url failed: %s", type(ex).__name__)

        if not cookies.get("SESSDATA"):
            return {"status": "error", "message": "登录已确认，但未取到登录态 Cookie，请重试"}

        if config.CLOUD_MODE or key not in _consumed:
            if config.CLOUD_MODE and store_cookies is None:
                raise ValueError("缺少已验证的扫码会话")
            saved = store_cookies(cookies) if config.CLOUD_MODE else _save_env(cookies)
            _apply_runtime(cookies)
            if not config.CLOUD_MODE:
                _consumed.add(key)
            logger.info(
                "[bili_login] 登录态已安全保存（凭据值不记录）"
            )

        account = await _fetch_account(client)

        return {
            "status": "confirmed",
            "message": "登录成功，登录态已安全保存",
            "account": account or {},
        }


async def _fetch_account(client: httpx.AsyncClient) -> dict:
    """登录后取一下昵称，用于界面反馈。失败不影响登录结果。

    直接复用扫码用的 client（它的 cookie jar 里已有登录态），不要把 jar 转成
    dict 再传：`dict(client.cookies)` 会对每个名字调 `__getitem__`，而登录响应
    里同名 SESSDATA 可能存在多个域作用域（.bilibili.com / passport.bilibili.com），
    httpx 会抛 CookieConflict("Multiple cookies exist with name=SESSDATA")。
    让 client 自己按域匹配即可，既不会冲突也能选对 cookie。
    """
    try:
        resp = await client.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"Referer": "https://www.bilibili.com/"},
        )
        data = (resp.json() or {}).get("data") or {}
        if data.get("isLogin"):
            return {"uname": data.get("uname") or ""}
    except Exception as ex:
        logger.warning("[bili_login] fetch account failed: %s", type(ex).__name__)
    return {}


async def check_status() -> dict:
    """主动检测当前登录态（供前端「重新检测」用）。"""
    import bili_api

    try:
        logged_in = await bili_api.check_login()
    except Exception as ex:
        return {"logged_in": None, "message": "登录状态检测失败，请稍后重试"}
    if logged_in is None:
        return {"logged_in": None, "message": "无法连接 B站，请稍后再试"}
    return {
        "logged_in": logged_in,
        "message": "登录态有效" if logged_in else "登录态已失效，请重新扫码登录",
    }
