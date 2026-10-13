# -*- coding: utf-8 -*-
"""B站反风控工具集。

移植自 bilibili-api-python 17.4.2 (Nemo2011/bilibili-api) 的关键反爬逻辑，
精简为本项目所需的最小集合：

1. buvid3/buvid4 自动获取（SPI 接口）
2. bili_ticket 签名（HMAC-SHA256）
3. WBI 签名（含 -403 自动重试）
4. dm_img 行为参数注入
"""
import hashlib
import hmac
import random
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode, quote

import httpx

# ──────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────

# WBI 混淆表（与 B站前端一致）
_WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 36, 25, 1, 4, 40, 44, 51, 6, 16, 21, 20, 30, 34, 22, 11,
    17, 52, 26, 0, 24, 57, 7, 48, 54, 55, 56, 61, 60, 59, 64, 62,
]

_BILI_TICKET_HMAC_KEY = "XgwSnGZ1p"
_BILI_TICKET_URL = "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.bilibili.com",
}


# ──────────────────────────────────────────
#  1. buvid3/buvid4 获取
# ──────────────────────────────────────────

_buvid3_cache: str = ""
_buvid4_cache: str = ""


async def get_buvid(client: httpx.AsyncClient) -> Tuple[str, str]:
    """从 B站 SPI 接口获取 buvid3/buvid4。

    返回值缓存在模块级别（进程生命周期内有效）。
    失败时返回空字符串（不阻断调用方）。
    """
    global _buvid3_cache, _buvid4_cache
    if _buvid3_cache and _buvid4_cache:
        return _buvid3_cache, _buvid4_cache

    try:
        resp = await client.get(_SPI_URL, headers=_HEADERS)
        if resp.status_code != 200:
            return _buvid3_cache, _buvid4_cache
        data = resp.json()
        if data.get("code") == 0:
            _buvid3_cache = data["data"].get("b_3", "")
            _buvid4_cache = data["data"].get("b_4", "")
    except Exception:
        pass  # 网络失败不阻断
    return _buvid3_cache, _buvid4_cache


def refresh_buvid():
    """强制下次重新获取 buvid。"""
    global _buvid3_cache, _buvid4_cache
    _buvid3_cache = ""
    _buvid4_cache = ""


# ──────────────────────────────────────────
#  2. bili_ticket 签名
# ──────────────────────────────────────────

_bili_ticket_cache: str = ""
_bili_ticket_expires: float = 0


async def get_bili_ticket(client: httpx.AsyncClient) -> str:
    """获取 bili_ticket（3 天有效，自动缓存）。

    失败时返回空字符串（不阻断调用方）。
    """
    global _bili_ticket_cache, _bili_ticket_expires
    if _bili_ticket_cache and time.time() < _bili_ticket_expires:
        return _bili_ticket_cache

    try:
        ts = int(time.time())
        hexsign = hmac.new(
            _BILI_TICKET_HMAC_KEY.encode(),
            f"ts{ts}".encode(),
            hashlib.sha256,
        ).hexdigest()

        params = {
            "key_id": "ec02",
            "hexsign": hexsign,
            "context[ts]": str(ts),
            "csrf": "",
        }
        resp = await client.post(_BILI_TICKET_URL, params=params, headers=_HEADERS)
        if resp.status_code != 200:
            return _bili_ticket_cache
        data = resp.json()
        if data.get("code") == 0:
            _bili_ticket_cache = data["data"]["ticket"]
            _bili_ticket_expires = time.time() + 3 * 86400  # 3天
    except Exception:
        pass  # 网络失败不阻断
    return _bili_ticket_cache


# ──────────────────────────────────────────
#  3. WBI 签名
# ──────────────────────────────────────────

_img_key_cache: str = ""
_sub_key_cache: str = ""
_wbi_key_ts: float = 0
_WBI_KEY_TTL = 3600  # 1小时缓存


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    """对 img_key + sub_key 进行混淆。"""
    orig = img_key + sub_key
    return "".join(orig[i] for i in _WBI_MIXIN_KEY_ENC_TAB if i < len(orig))[:32]


async def _refresh_wbi_keys(client: httpx.AsyncClient):
    """从 /x/web-interface/nav 获取最新 WBI 密钥。"""
    global _img_key_cache, _sub_key_cache, _wbi_key_ts
    try:
        resp = await client.get(_NAV_URL, headers=_HEADERS)
        if resp.status_code != 200:
            return
        data = resp.json()
        if data.get("code") == 0:
            wbi_img = data["data"]["wbi_img"]
            _img_key_cache = wbi_img["img_url"].rsplit("/", 1)[-1].split(".")[0]
            _sub_key_cache = wbi_img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
            _wbi_key_ts = time.time()
    except Exception:
        pass


async def wbi_sign(
    params: dict,
    client: httpx.AsyncClient,
    *,
    retry_on_403: bool = True,
) -> dict:
    """对请求参数添加 WBI 签名（wts + w_rid）。

    如果密钥过期会自动刷新；支持 -403 时重试一次。
    """
    global _img_key_cache, _sub_key_cache, _wbi_key_ts

    # 确保密钥可用
    if not _img_key_cache or time.time() - _wbi_key_ts > _WBI_KEY_TTL:
        await _refresh_wbi_keys(client)

    if not _img_key_cache:
        return params  # 拿不到密钥时降级

    mixin_key = _get_mixin_key(_img_key_cache, _sub_key_cache)
    return _do_sign(params, mixin_key)


def _do_sign(params: dict, mixin_key: str) -> dict:
    """实际签名计算。"""
    params = dict(params)  # 不修改原 dict
    params.pop("w_rid", None)
    params["wts"] = int(time.time())
    # web_location 某些接口需要
    if "web_location" not in params:
        params["web_location"] = 1550101
    query = urlencode(sorted(params.items()), quote_via=quote)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def force_refresh_wbi():
    """强制下次重新获取 WBI 密钥（用于 -403 重试）。"""
    global _wbi_key_ts
    _wbi_key_ts = 0


# ──────────────────────────────────────────
#  4. dm_img 行为参数
# ──────────────────────────────────────────

def inject_dm_params(params: dict) -> dict:
    """注入 dm_img 系列参数（模拟浏览器鼠标/键盘行为记录）。"""
    dm_rand = "ABCDEFGHIJK"
    params["dm_img_list"] = "[]"
    params["dm_img_str"] = "".join(random.sample(dm_rand, 2))
    params["dm_cover_img_str"] = "".join(random.sample(dm_rand, 2))
    params["dm_img_inter"] = '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'
    return params


# ──────────────────────────────────────────
#  5. 组合 Cookie 构建
# ──────────────────────────────────────────

async def build_cookies(
    client: httpx.AsyncClient,
    sessdata: str = "",
    bili_jct: str = "",
    buvid3_override: str = "",
    buvid4_override: str = "",
    dedeuserid: str = "",
) -> Dict[str, str]:
    """构建完整的请求 Cookie dict。

    - 若传入 sessdata 则使用登录态
    - 自动获取 buvid3/buvid4（也可用 .env 里的浏览器原值覆盖，保持指纹一致）
    - 自动获取 bili_ticket
    """
    b3, b4 = await get_buvid(client)
    ticket = await get_bili_ticket(client)

    cookies: Dict[str, str] = {}
    # buvid
    cookies["buvid3"] = buvid3_override or b3
    cookies["buvid4"] = buvid4_override or b4
    # bili_ticket
    if ticket:
        cookies["bili_ticket"] = ticket
    # 登录态
    if sessdata:
        cookies["SESSDATA"] = sessdata
    if bili_jct:
        cookies["bili_jct"] = bili_jct
    if dedeuserid:
        cookies["DedeUserID"] = dedeuserid
    return cookies


# ──────────────────────────────────────────
#  6. 错误码判断
# ──────────────────────────────────────────

class BiliApiError(Exception):
    """B站 API 业务错误。"""
    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def check_response(data: dict) -> dict:
    """检查 API 返回的 code，非 0 抛出 BiliApiError。"""
    code = data.get("code", -1)
    if code == 0:
        return data.get("data", {})
    raise BiliApiError(code, data.get("message", ""))


def classify_error(code: int) -> str:
    """将 API 错误码分类为可读标签（用于写入 crawl_state）。"""
    if code == -101:
        return "cookie_expired"
    elif code == -352:
        return "risk_control(-352)"
    elif code == -403:
        return "wbi_expired(-403)"
    elif code == -412 or code == 412:
        return "risk_control(412)"
    elif code == -799:
        return "request_too_frequent"
    else:
        return f"api_error({code})"
