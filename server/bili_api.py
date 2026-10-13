# -*- coding: utf-8 -*-
"""纯 API 爬取模块（替代 MediaCrawler 子进程）。

结合自研 WBI 签名 + 库移植的反风控（buvid/bili_ticket/dm_img），
直接 httpx 调 B站 Web API 拿视频和动态。
"""
import asyncio
import logging
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx

import config
import db
from anti_spider import (
    BiliApiError,
    build_cookies,
    check_response,
    classify_error,
    force_refresh_wbi,
    inject_dm_params,
    wbi_sign,
    _HEADERS,
)

logger = logging.getLogger("bili_api")

# ──────────────────────────────────────────
#  Client
# ──────────────────────────────────────────

_client: Optional[httpx.AsyncClient] = None
_cookies: Dict[str, str] = {}


async def _get_client() -> httpx.AsyncClient:
    """获取或创建 httpx client。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=30.0,
            follow_redirects=True,
        )
    return _client


async def _ensure_cookies():
    """确保 cookies 已构建（buvid + ticket + 登录态）。"""
    global _cookies
    if _cookies:
        return
    client = await _get_client()
    _cookies = await build_cookies(
        client,
        sessdata=config.BILI_SESSDATA,
        bili_jct=config.BILI_JCT,
        buvid3_override=config.BILI_BUVID3,
        buvid4_override=config.BILI_BUVID4,
        dedeuserid=config.BILI_DEDEUSERID,
    )
    logger.info(f"[bili_api] Cookies built: keys={list(_cookies.keys())}")


def reset():
    """重置状态（cookie/client），供外部调用。"""
    global _client, _cookies
    _cookies = {}
    if _client and not _client.is_closed:
        # 不阻塞：后台关闭
        asyncio.get_event_loop().create_task(_client.aclose())
    _client = None


async def aclose_client():
    """异步关闭并重置共享 httpx client（供字幕模块等调用方收尾）。"""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def invalidate_cookies():
    """丢弃已构建的 cookie 缓存，下次请求按新配置重建。

    扫码登录成功后调用：config 里的 BILI_SESSDATA 已更新，这里让下一次
    请求重新走 build_cookies（含取 buvid/bili_ticket），无需重启进程。
    """
    global _cookies
    _cookies = {}


async def _delay():
    await asyncio.sleep(random.uniform(config.CRAWL_SLEEP_MIN, config.CRAWL_SLEEP_MAX))


# ──────────────────────────────────────────
#  API 请求（带 WBI + 重试）
# ──────────────────────────────────────────

# 限流/风控类错误码：同一账号在不同时刻会在这几个码之间跳变，都可退避重试
_RISK_CODES = (-352, -412, -799)
# 每次重试前的基准退避秒数（实际取 ±30% 抖动）
_RISK_BACKOFF = (10.0, 25.0, 45.0)
# 整轮冷却：某次请求退避用尽仍失败后，短时间内不再打接口，避免持续触发风控
_risk_cooldown_until = 0.0
# 软限流空包（HTTP 200 + code=0 + items=[]）的重试退避秒数
_EMPTY_BACKOFF = (8.0, 20.0)
# 搜索空包的重试退避：交互式搜索不能等太久，故比 feed 短很多
_LOOKUP_BACKOFF = (2.0, 5.0)


def _trip_cooldown(seconds: float):
    global _risk_cooldown_until
    _risk_cooldown_until = max(_risk_cooldown_until, time.time() + seconds)


async def _respect_cooldown():
    remain = _risk_cooldown_until - time.time()
    if remain > 0:
        logger.warning(f"[bili_api] risk cooldown: waiting {remain:.0f}s before next request")
        await asyncio.sleep(remain)


async def _api_get(url: str, params: dict, *, need_wbi: bool = False,
                   client: Optional[httpx.AsyncClient] = None) -> dict:
    """通用 GET 请求，带风控退避重试。

    返回 data 字段（已 check_response）。
    失败抛出 BiliApiError 或 httpx 异常。
    client=None 时用共享 client（爬取主流程）；传入一次性 client 用于
    独立的轻量查询（搜索/用户信息），避免与运行中的爬虫争抢绑定在
    其他 event loop 上的连接池。

    -352 / -412 / -799 与 HTTP 412 都是限流类瞬时错误（实测同一 UID
    首次 -352、等几秒重试即拿到数据），按 _RISK_BACKOFF 退避重试；
    退避用尽才抛错，并置一段整轮冷却，避免继续硬打接口加重风控。
    """
    global _cookies
    if client is None:
        client = await _get_client()
    if _cookies:
        cookies = _cookies
    else:
        cookies = await build_cookies(
            client,
            sessdata=config.BILI_SESSDATA,
            bili_jct=config.BILI_JCT,
            buvid3_override=config.BILI_BUVID3,
            buvid4_override=config.BILI_BUVID4,
            dedeuserid=config.BILI_DEDEUSERID,
        )
        _cookies = cookies
        logger.info(f"[bili_api] Cookies built: keys={list(_cookies.keys())}")

    if need_wbi:
        params = inject_dm_params(params)
        params = await wbi_sign(params, client)

    async def _refresh_sign():
        """重试前重新注入 dm_img 参数并重签（wts 会过期）。"""
        nonlocal params
        if need_wbi:
            params = inject_dm_params(params)
            params = await wbi_sign(params, client)

    risk_attempt = 0
    net_attempt = 0
    while True:
        await _respect_cooldown()

        try:
            resp = await client.get(url, params=params, cookies=cookies)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            net_attempt += 1
            if net_attempt < 3:
                logger.warning(f"[bili_api] Network error, retry {net_attempt}: {e}")
                await asyncio.sleep(3)
                continue
            raise BiliApiError(412, f"network_error:{type(e).__name__}")

        body = None
        try:
            body = resp.json()
        except Exception:
            pass

        if resp.status_code == 412 or body is None:
            code, message = 412, f"HTTP {resp.status_code} (非 JSON 响应或风控)"
        else:
            code = body.get("code", -1)
            message = body.get("message", "")

        if code == 0 and body is not None:
            return body.get("data", {})

        endpoint = url.rstrip("/").rsplit("/", 1)[-1]

        if code in _RISK_CODES and risk_attempt < len(_RISK_BACKOFF):
            base = _RISK_BACKOFF[risk_attempt]
            wait = random.uniform(base * 0.7, base * 1.3)
            risk_attempt += 1
            _count_risk(f"{endpoint}:{code}")
            logger.warning(
                f"[bili_api] {classify_error(code)} @{endpoint} "
                f"-> retry {risk_attempt}/{len(_RISK_BACKOFF)} after {wait:.0f}s"
            )
            await asyncio.sleep(wait)
            await _refresh_sign()
            continue

        if code == -403 and risk_attempt < len(_RISK_BACKOFF):
            # WBI 密钥过期 → 刷新重试
            risk_attempt += 1
            logger.warning(f"[bili_api] -403, refreshing WBI keys (attempt {risk_attempt})")
            force_refresh_wbi()
            params = inject_dm_params(params)
            params = await wbi_sign(params, client)
            await asyncio.sleep(1)
            continue

        if code in _RISK_CODES:
            # 退避重试用尽仍未通过：置整轮冷却，给账号/IP 一点恢复时间
            _count_risk(f"{endpoint}:{code}:exhausted")
            _trip_cooldown(random.uniform(45, 90))

        raise BiliApiError(code, message)


# ──────────────────────────────────────────
#  登录态检测
# ──────────────────────────────────────────

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# 登录态缓存（避免每次请求都打 nav 接口）
_login_cache_ts = 0.0
_login_cache_value: Optional[bool] = None


async def check_login() -> Optional[bool]:
    """检测 B站登录态是否有效（nav 接口 isLogin）。

    返回 True=已登录 / False=确定未登录(-101) / None=检测不出来（网络等）。

    为什么需要主动检测：SESSDATA 失效后，空间 feed 往往返回 **-352（风控）**
    而不是 -101（未登录）——因为匿名请求的风控阈值极低。只靠 feed 的错误码
    判断会把「登录态失效」误判成「风控限流」，进而徒劳地退避重试。

    这里刻意使用**一次性 client**（不碰共享 client）：本函数会被后台线程
    定期调用，若复用共享 client 会与正在运行的爬取争抢连接池 / 跨 event loop
    复用，甚至把爬虫正在用的 client 关掉。
    """
    global _cookies
    client = httpx.AsyncClient(headers=_HEADERS, timeout=30.0, follow_redirects=True)
    try:
        if _cookies:
            cookies = _cookies
        else:
            cookies = await build_cookies(
                client,
                sessdata=config.BILI_SESSDATA,
                bili_jct=config.BILI_JCT,
                buvid3_override=config.BILI_BUVID3,
                buvid4_override=config.BILI_BUVID4,
                dedeuserid=config.BILI_DEDEUSERID,
            )
            _cookies = cookies

        resp = await client.get(NAV_URL, cookies=cookies)
        body = resp.json()
        code = body.get("code", -1)
        if code == 0:
            return bool((body.get("data") or {}).get("isLogin"))
        if code == -101:
            return False
        logger.warning(f"[bili_api] login check code={code} msg={body.get('message')!r}")
        return None
    except Exception as e:
        logger.warning(f"[bili_api] login check error: {e}")
        return None
    finally:
        await client.aclose()


async def check_login_cached(ttl: float = 120.0) -> Optional[bool]:
    """带 TTL 缓存的登录态检测（供爬取报错时交叉验证使用）。"""
    global _login_cache_ts, _login_cache_value
    now = time.time()
    if _login_cache_value is not None and now - _login_cache_ts < ttl:
        return _login_cache_value
    value = await check_login()
    if value is not None:
        _login_cache_ts = now
        _login_cache_value = value
    return value


def invalidate_login_cache():
    """登录成功后调用：丢弃缓存，下一次检测重新打接口。"""
    global _login_cache_ts, _login_cache_value
    _login_cache_ts = 0.0
    _login_cache_value = None


def login_cache_snapshot() -> dict:
    """给诊断端点用：登录态缓存内容（不含任何凭据）。"""
    return {
        "value": _login_cache_value,
        "age_seconds": (int(time.time() - _login_cache_ts) if _login_cache_ts else None),
    }


# ──────────────────────────────────────────
#  风控计数（排错用：看清是哪类错误、打在哪个接口）
# ──────────────────────────────────────────

_risk_stats: Dict[str, int] = {}


def _count_risk(key: str):
    _risk_stats[key] = _risk_stats.get(key, 0) + 1


def risk_stats() -> dict:
    return dict(sorted(_risk_stats.items()))


def reset_risk_stats():
    _risk_stats.clear()


def risk_cooldown_remaining() -> float:
    return max(0.0, round(_risk_cooldown_until - time.time(), 1))


# ──────────────────────────────────────────
#  空间 feed（视频+动态统一来源）
# ──────────────────────────────────────────
# 注意：视频列表接口 /x/space/wbi/arc/search 已被 B站用 w_webid 封锁
# （返回 -403 访问权限不足，2026-08 实测），UP主投稿默认自动转动态，
# 故视频与动态统一从动态 feed 获取：AV 动态自带 aid/bvid/标题/封面/播放数。

FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"


def _strip_dyn_type(dtype: str) -> str:
    return dtype[len("DYNAMIC_TYPE_"):] if (dtype or "").startswith("DYNAMIC_TYPE_") else dtype


async def fetch_space_feed(uid: str, pages: int = 2) -> List[dict]:
    """拉取 UP主空间动态 feed（带 offset 翻页），返回原始 items。

    首屏空 items 视为软限流：实测同一 UID 可能前一次返回 13 条、下一次
    返回 HTTP 200 + code=0 + items=[]（B站用空包做软限流），故按
    _EMPTY_BACKOFF 退避重试，避免把「限流空包」静默当成「该 UP 主没内容」。
    """
    client = await _get_client()
    await _ensure_cookies()

    items: List[dict] = []
    offset = ""
    for page in range(pages):
        params = {
            "host_mid": uid,
            "offset": offset,
            "platform": "web",
            "features": "itemOpusStyle",
        }
        data = await _api_get(FEED_URL, params)
        batch = data.get("items") or []

        if page == 0:
            for attempt, base in enumerate(_EMPTY_BACKOFF):
                if batch:
                    break
                wait = random.uniform(base * 0.7, base * 1.3)
                _count_risk("feed:empty_payload")
                logger.warning(
                    f"[bili_api] feed uid={uid}: empty payload (soft throttle?) "
                    f"-> retry {attempt + 1}/{len(_EMPTY_BACKOFF)} after {wait:.0f}s"
                )
                await asyncio.sleep(wait)
                data = await _api_get(FEED_URL, params)
                batch = data.get("items") or []

        items.extend(batch)
        if not data.get("has_more"):
            break
        offset = data.get("offset") or ""
        if not offset:
            break
        await asyncio.sleep(random.uniform(1.5, 3.0))
    return items


def _feed_video_row(item: dict, uid: str) -> Optional[dict]:
    """从 AV 动态 item 提取视频行（bvid 主键保持 'av{aid}' 格式与旧数据一致）。"""
    try:
        modules = item.get("modules") or {}
        archive = (modules.get("module_dynamic") or {}).get("major") or {}
        archive = archive.get("archive") or {}
        aid = str(archive.get("aid") or "").strip()
        real_bvid = archive.get("bvid") or ""
        if not aid and not real_bvid:
            return None
        pub_ts = (modules.get("module_author") or {}).get("pub_ts", 0)
        stat = archive.get("stat") or {}
        return {
            "bvid": f"av{aid}" if aid else real_bvid,
            "uid": str(uid),
            "title": archive.get("title", ""),
            "cover": archive.get("pic", ""),
            "desc": "",  # feed 不含简介，enrich 阶段用 view 接口补全
            "pub_ts": int(pub_ts or 0),
            "play": int(stat.get("play") or 0),
            "url": f"https://www.bilibili.com/video/{real_bvid or 'av' + aid}",
        }
    except Exception:
        return None


async def fetch_videos(uid: str, pages: int = 2) -> List[dict]:
    """获取 UP主最新视频（从空间 feed 的 AV 动态提取）。

    返回 list[dict]，每项包含 bvid/title/desc/pub_ts/play/cover/url。
    """
    items = await fetch_space_feed(uid, pages=pages)
    rows, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "DYNAMIC_TYPE_AV":
            continue
        row = _feed_video_row(item, uid)
        if row and row["bvid"] not in seen:
            seen.add(row["bvid"])
            rows.append(row)
    return rows


# ──────────────────────────────────────────
#  搜索接口兜底
# ──────────────────────────────────────────

SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"


def _clean_search_title(title: str) -> str:
    """搜索结果标题带 <em class=\"keyword\"> 高亮标签，去掉。"""
    return re.sub(r"</?em[^>]*>", "", title or "")


async def fetch_videos_by_search(uid: str, keyword: str, page_size: int = 50) -> List[dict]:
    """搜索接口兜底：按 UP名 搜索最新视频，按作者 mid 过滤。

    背景：视频列表接口 /x/space/wbi/arc/search 已被 w_webid 封锁（-403），
    投稿未自动转动态的视频 feed 里看不到，只能靠搜索补齐。
    搜索结果混有其他用户的视频，页宽给大（50，接口上限）避免该 UP 的
    新视频被无关结果挤出首页；搜索索引可能有几分钟延迟，属可接受范围。
    """
    params = {
        "search_type": "video",
        "keyword": keyword,
        "order": "pubdate",
        "page": 1,
        "page_size": page_size,
    }
    data = await _api_get(SEARCH_URL, params, need_wbi=True)
    results = data.get("result") or []

    rows = []
    for it in results:
        if not isinstance(it, dict) or str(it.get("mid", "")) != str(uid):
            continue
        aid = str(it.get("aid") or "").strip()
        real_bvid = it.get("bvid") or ""
        if not aid and not real_bvid:
            continue
        pic = it.get("pic") or ""
        if pic.startswith("//"):
            pic = "https:" + pic
        rows.append({
            "bvid": f"av{aid}" if aid else real_bvid,
            "uid": str(uid),
            "title": _clean_search_title(it.get("title", "")),
            "cover": pic,
            "desc": "",  # 搜索简介是截断版，enrich 阶段用 view 接口回填
            "pub_ts": int(it.get("pubdate") or 0),
            "play": int(it.get("play") or 0),
            "url": f"https://www.bilibili.com/video/{real_bvid or 'av' + aid}",
        })
    return rows


# ──────────────────────────────────────────
#  订阅管理用的用户查询（搜索UP主 / 按UID查信息）
# ──────────────────────────────────────────

CARD_URL = "https://api.bilibili.com/x/web-interface/card"


async def _lookup_get(url: str, params: dict, *, need_wbi: bool = False) -> dict:
    """用一次性 httpx client 发轻量 GET。

    订阅搜索/用户信息查询可能在爬虫运行期间被 API 层调用，而共享 client
    绑定在爬虫线程的 event loop 上，跨 loop 复用会报 "Event loop is closed"；
    这些查询频率极低，独立建 client 的开销可以忽略。
    """
    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=15.0, follow_redirects=True
    ) as client:
        return await _api_get(url, params, need_wbi=need_wbi, client=client)


def _norm_avatar(url: str) -> str:
    url = url or ""
    return ("https:" + url) if url.startswith("//") else url


async def fetch_user_card(uid: str) -> dict:
    """按 UID 查询 UP 主信息（card 接口，无需 WBI）。

    返回 {uid, name, face, fans}。

    「用户不存在」与「接口空包」必须区分开：前者 B站 直接返回 code=-404，
    后者是风控下的空响应（缺失 `card` 键）。若把空包一律当成不存在，用户
    订阅一个真实存在的 UID 时会收到「该 UID 在B站不存在」的错误提示。
    """
    data = {}
    for attempt in range(len(_LOOKUP_BACKOFF) + 1):
        data = await _lookup_get(CARD_URL, {"mid": str(uid), "photo": "false"})
        if "card" in data:
            break
        if attempt >= len(_LOOKUP_BACKOFF):
            _count_risk("user_card:empty_payload:exhausted")
            raise BiliApiError(-1, "用户信息接口返回空数据（疑似限流），请稍后重试")
        wait = random.uniform(_LOOKUP_BACKOFF[attempt] * 0.7, _LOOKUP_BACKOFF[attempt] * 1.3)
        _count_risk("user_card:empty_payload")
        logger.warning(
            f"[bili_api] fetch_user_card uid={uid}: 空包(软限流?) "
            f"-> retry {attempt + 1}/{len(_LOOKUP_BACKOFF)} after {wait:.0f}s"
        )
        await asyncio.sleep(wait)

    card = data.get("card") or {}
    if not card.get("mid") and not card.get("name"):
        raise BiliApiError(-404, "用户不存在")
    return {
        "uid": str(card.get("mid") or uid),
        "name": _clean_search_title(card.get("name", "")),
        "face": _norm_avatar(card.get("face", "")),
        "fans": int(card.get("fans") or 0),
    }


async def search_users(keyword: str, limit: int = 8) -> List[dict]:
    """按关键词搜索 UP 主（wbi 搜索接口, search_type=bili_user）。

    返回 [{uid, name, face, fans, sign, videos}]，按相关度排序取前 limit 个。

    注意：该接口在未登录/风控状态下会**间歇性返回空包**（HTTP 200、code=0，
    但缺失 `result` 字段、且 `numResults` 为 None），实测约半数请求如此。
    若不重试就会把「被限流」误报成「没有找到该 UP 主」，所以这里对空包做短
    退避重试；退避用尽仍为空包时抛出可区分的错误，交由前端提示「稍后重试 /
    改用 UID」。

    三类返回的精确定义（实测得出）：
    - `result` 是列表            -> 正常结果（可能是空列表 = 真的没搜到）
    - 无 `result` 但 numResults=0 -> 搜索成功但确实没有匹配 -> 返回 []
    - 无 `result` 且 numResults=None -> 软限流空包 -> 重试，仍失败则报错
    """
    params = {
        "search_type": "bili_user",
        "keyword": keyword,
        "page": 1,
        "page_size": limit,
    }

    data = {}
    for attempt in range(len(_LOOKUP_BACKOFF) + 1):
        data = await _lookup_get(SEARCH_URL, params, need_wbi=True)
        if isinstance(data.get("result"), list):
            break
        # 真·没有匹配：搜索本身成功，只是命中 0 个
        if data.get("numResults") == 0:
            return []
        if attempt >= len(_LOOKUP_BACKOFF):
            _count_risk("search:empty_payload:exhausted")
            raise BiliApiError(
                -1, "B站搜索返回空数据（疑似限流），请稍后重试或直接用 UID 搜索"
            )
        wait = random.uniform(_LOOKUP_BACKOFF[attempt] * 0.7, _LOOKUP_BACKOFF[attempt] * 1.3)
        _count_risk("search:empty_payload")
        logger.warning(
            f"[bili_api] search_users kw={keyword!r}: 空包(软限流?) "
            f"-> retry {attempt + 1}/{len(_LOOKUP_BACKOFF)} after {wait:.0f}s"
        )
        await asyncio.sleep(wait)

    results = data.get("result") or []

    rows = []
    for it in results[:limit]:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("mid", "")).strip()
        if not mid:
            continue
        rows.append({
            "uid": mid,
            "name": _clean_search_title(it.get("uname", "")),
            "face": _norm_avatar(it.get("face", "")),
            "fans": int(it.get("fans") or 0),
            "sign": _clean_search_title(it.get("usign", "")),
            "videos": int(it.get("videos") or 0),
        })
    return rows


# ──────────────────────────────────────────
#  动态列表
# ──────────────────────────────────────────

async def fetch_dynamics(uid: str, pages: int = 2) -> List[dict]:
    """获取 UP主最新动态（空间 feed，带翻页）。

    返回 list[dict]，每项包含 dyn_id/type/text/pub_ts/url。
    """
    items = await fetch_space_feed(uid, pages=pages)

    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dyn_id = item.get("id_str", "")
        if not dyn_id:
            continue
        dyn_type = _strip_dyn_type(item.get("type", ""))
        text = _extract_dynamic_text(item)
        pub_ts = int(item.get("modules", {}).get("module_author", {}).get("pub_ts", 0))
        rows.append({
            "dyn_id": dyn_id,
            "uid": str(uid),
            "type": dyn_type,
            "text": text,
            "pub_ts": pub_ts,
            "url": f"https://t.bilibili.com/{dyn_id}",
        })
    return rows


def _extract_dynamic_text(item: dict) -> str:
    """从动态 item 中提取文字内容。"""
    modules = item.get("modules", {})
    module_dynamic = modules.get("module_dynamic", {})

    # 主文本
    desc = module_dynamic.get("desc")
    text = desc.get("text", "") if desc else ""

    # 视频/专栏等 major
    major = module_dynamic.get("major")
    if major:
        opus = major.get("opus")
        if opus:
            title = opus.get("title", "")
            summary = opus.get("summary", {}).get("text", "")
            if title:
                text = f"[视频] {title}\n{summary}\n{text}"
        archive = major.get("archive")
        if archive:
            text = f"[视频] {archive.get('title','')}\n{text}"

    # 转发
    orig = item.get("orig")
    if orig:
        orig_text = _extract_dynamic_text(orig)
        text = f"{text}\n[转发] {orig_text}"

    return text.strip()


def _extract_dynamic_pics(item: dict) -> list:
    """提取动态配图 URL（只取内容图，不含作者装饰）。

    实测路径：图文动态 = modules.module_dynamic.major.opus.pics[].url；
    旧版 schema = major.draw.items[].src。
    注意 major 在"充电专属"动态里是 None（B站会把整块内容剥掉），所以必须 or {}。

    刻意排除这些"不是内容图"的字段（实测它们每一条动态都有，混进来会污染识别）：
    module_author.face / avatar.fallback_layers... / pendant.image / vip.label.* /
    icon_badge.* / 正文表情 rich_text_nodes[].emoji.icon_url。
    """
    out: list = []
    modules = item.get("modules") or {}
    md = modules.get("module_dynamic") or {}
    major = md.get("major") or {}

    opus = major.get("opus") or {}
    for p in opus.get("pics") or []:
        u = p.get("url") if isinstance(p, dict) else None
        if u:
            out.append(u)
    if not out:
        draw = major.get("draw") or {}
        for d in draw.get("items") or []:
            u = d.get("src") if isinstance(d, dict) else None
            if u:
                out.append(u)

    orig = item.get("orig")
    if orig:
        for u in _extract_dynamic_pics(orig):
            if u not in out:
                out.append(u)

    # 统一 https（feed 里 cover/某些 URL 是 http://），去重保序
    seen, clean = set(), []
    for u in out:
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("http://"):
            u = "https://" + u[7:]
        if u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


def _feed_dyn_row(item: dict, uid: str) -> Optional[dict]:
    """从 feed item 提取动态行。"""
    dyn_id = item.get("id_str", "")
    if not dyn_id:
        return None
    return {
        "dyn_id": dyn_id,
        "uid": str(uid),
        "type": _strip_dyn_type(item.get("type", "")),
        "text": _extract_dynamic_text(item),
        "pub_ts": int(item.get("modules", {}).get("module_author", {}).get("pub_ts", 0)),
        "url": f"https://t.bilibili.com/{dyn_id}",
        # 实测：feed 里这个列表绝大多数时候是空的（纯文字贴 / 充电专属被剥离），
        # 解析它零成本，将来订阅到发公开图文的 UP 就能自动用上
        "pics": _extract_dynamic_pics(item),
    }


def _parse_feed(items: List[dict], uid: str) -> Tuple[List[dict], List[dict]]:
    """把 feed items 拆分为（动态行, 视频行）。"""
    dyn_rows, vid_rows, seen = [], [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        row = _feed_dyn_row(item, uid)
        if row:
            dyn_rows.append(row)
        if item.get("type") == "DYNAMIC_TYPE_AV":
            v = _feed_video_row(item, uid)
            if v and v["bvid"] not in seen:
                seen.add(v["bvid"])
                vid_rows.append(v)
    return dyn_rows, vid_rows


# ──────────────────────────────────────────
#  统一入口
# ──────────────────────────────────────────

def _has_history(uid: str) -> bool:
    """该 UID 在库里是否已有数据。

    用于区分「风控软限流返回空包」与「这个 UP 主本来就没内容」：
    只有前者才该记错误，避免给新订阅的空白 UP 主打上风控标签。
    """
    try:
        return bool(db.list_videos(uid, limit=1) or db.list_dynamics(uid, limit=1))
    except Exception:
        return False


async def crawl_one_uid(uid: str) -> dict:
    """爬取单个 UID 的视频 + 动态（一次 feed 拉取拆分两者）。

    返回:
        {
            "ok": bool,
            "videos": [...],
            "dynamics": [...],
            "video_count": int,
            "dynamic_count": int,
            "error": str | None,
            "login_required": bool,
        }
    """
    result = {
        "ok": False,
        "videos": [],
        "dynamics": [],
        "video_count": 0,
        "dynamic_count": 0,
        "error": None,
        "login_required": False,
    }

    try:
        items = await fetch_space_feed(uid)
        dyn_rows, vid_rows = _parse_feed(items, uid)

        if not items:
            # 首页重试后依然为空：库里已有数据的 UP 主判定为「软限流空包」，
            # 显式记录错误——否则「0 条且无错误」会被当成正常结束，前端
            # 看不到任何异常，表现为「爬取像失效了」。
            if _has_history(uid):
                result["error"] = "empty_feed(soft_throttle)"
                logger.warning(f"[bili_api] feed uid={uid}: empty_feed(soft_throttle)")
            return result

        # 搜索兜底：补齐投稿未自动转动态的视频（arc/search 已被 w_webid 封锁）
        try:
            await asyncio.sleep(random.uniform(1.5, 3.0))
            extra = await fetch_videos_by_search(uid, config.up_name(uid))
            if extra:
                seen = {r["bvid"] for r in vid_rows}
                vid_rows.extend(r for r in extra if r["bvid"] not in seen)
        except Exception as e:
            logger.warning(f"[bili_api] search fallback uid={uid}: {e}")

        result["dynamics"] = dyn_rows
        result["videos"] = vid_rows
        result["dynamic_count"] = len(dyn_rows)
        result["video_count"] = len(vid_rows)
    except BiliApiError as e:
        err = classify_error(e.code)
        if e.code in _RISK_CODES:
            # -352 往往不是真限流，而是登录态掉了（匿名请求风控阈值极低）。
            # 交叉验证一次登录态，避免把「未登录」误报成「风控」而白等半天。
            logged = await check_login_cached()
            if logged is False:
                err = "login_required"
                result["login_required"] = True
                logger.warning(
                    f"[bili_api] feed uid={uid}: 实为登录态失效（feed 报 {e.code}），"
                    "请在网页扫码登录"
                )
        result["error"] = err
        if e.code == -101:
            result["login_required"] = True
        logger.warning(f"[bili_api] feed uid={uid}: {err}")
    except Exception as e:
        result["error"] = f"exception:{e}"
        logger.error(f"[bili_api] feed uid={uid}: {e}")

    result["ok"] = (result["video_count"] > 0 or result["dynamic_count"] > 0) and result["error"] is None
    return result
