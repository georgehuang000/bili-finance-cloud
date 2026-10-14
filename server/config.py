# -*- coding: utf-8 -*-
"""Configuration for the server backend.

Loads the workspace-root .env (../.env relative to this file) and exposes
constants used across the app. The LLM secret (`LLM_API_KEY`, or the
equivalent manual name `deepseek_key`) is read here but never printed or logged.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# --- paths ---
SERVER_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SERVER_DIR.parent
ENV_PATH = WORKSPACE_ROOT / ".env"

# MediaCrawler integration paths (legacy fallback mode only; the pure API
# mode never touches these). venv layout differs between Windows and Linux.
MEDIACRAWLER_DIR = WORKSPACE_ROOT / "external" / "MediaCrawler"
if os.name == "nt":
    MC_PYTHON = MEDIACRAWLER_DIR / ".venv" / "Scripts" / "python.exe"
else:
    MC_PYTHON = MEDIACRAWLER_DIR / ".venv" / "bin" / "python"
MC_CONFIG_FILE = MEDIACRAWLER_DIR / "config" / "bilibili_config.py"
MC_JSON_DIR = MEDIACRAWLER_DIR / "data" / "bili" / "json"
MC_LOGIN_DATA_DIR = MEDIACRAWLER_DIR / "browser_data" / "bili_user_data_dir"

# local server data
DB_PATH = SERVER_DIR / "data.db"
TMP_DIR = SERVER_DIR / "tmp"

# load env from workspace root
if not (os.getenv("CLOUD_MODE", "").lower() in ("1", "true", "yes") or os.getenv("VERCEL") == "1"):
    load_dotenv(dotenv_path=ENV_PATH)


def _get(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val is not None else default


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except (TypeError, ValueError):
        return default


# Cloud API functions never run a scheduler or keep durable files locally.
CLOUD_MODE = _get("VERCEL") == "1" or _get("CLOUD_MODE", "false").lower() in ("1", "true", "yes")
CLOUD_WORKER = _get("CLOUD_WORKER", "false").lower() in ("1", "true", "yes")
CLOUD_API_ONLY = _get("CLOUD_API_ONLY", "false").lower() in ("1", "true", "yes")
CLOUD_TEST_MODE = _get("CLOUD_TEST_MODE", "true").lower() in ("1", "true", "yes")
DATABASE_URL = _get("DATABASE_URL")
ADMIN_PASSWORD_HASH = _get("ADMIN_PASSWORD_HASH")
CREDENTIAL_ENCRYPTION_KEY = _get("CREDENTIAL_ENCRYPTION_KEY")
if CLOUD_MODE:
    import tempfile
    TMP_DIR = Path(tempfile.gettempdir()) / "bili-monitor"


# --- LLM（OpenAI 兼容协议，默认指向 DeepSeek 官方）---
# 密钥读取优先级：LLM_API_KEY（网页「模型设置」写入的键名）
#                 > deepseek_key（手动写在 .env 里的键名）
# 两者都支持，先命中的生效。
# The secret is read here but MUST NOT be printed/logged anywhere.
CLOUD_LLM_BASES = {
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
LLM_PROVIDER = _get("LLM_PROVIDER", "deepseek").lower()
if CLOUD_MODE and LLM_PROVIDER not in CLOUD_LLM_BASES:
    raise ValueError("云端分析服务仅支持已配置的官方服务商")
provider_key = "DASHSCOPE_API_KEY" if LLM_PROVIDER == "dashscope" else "DEEPSEEK_API_KEY"
LLM_API_KEY = (_get(provider_key) or _get("LLM_API_KEY")) if CLOUD_MODE else (_get("LLM_API_KEY") or _get("deepseek_key"))
# 兼容别名：老代码/诊断里用的是这个名字
SENSETIME_KEY = LLM_API_KEY

LLM_BASE_URL = _get("LLM_BASE_URL", "https://api.deepseek.com/v1")
if CLOUD_MODE:
    # Never accept a browser-supplied or arbitrary deployment destination.
    LLM_BASE_URL = CLOUD_LLM_BASES[LLM_PROVIDER]
# deepseek-flash = DeepSeek-V4.1-Flash：1M 上下文、384K 输出、支持图片识别、最便宜。
# 注意 `deepseek-v4-flash` 是已退役的旧名（仍被接受，但由 V4.1-Flash 承接）。
LLM_MODEL = _get("LLM_MODEL", "qwen3.8-max" if CLOUD_MODE and LLM_PROVIDER == "dashscope" else "deepseek-flash")
# 不做兜底模型：只用上面这一个（LLM_MODEL_FALLBACK 已废弃；.env 里残留的该键会被忽略）
# 图片识别用的模型。默认跟随 LLM_MODEL（在 DeepSeek 官方就是 deepseek-flash）。
VISION_MODEL = _get("VISION_MODEL") or LLM_MODEL
# 思考模式：disabled（默认）/ low / high / max
# DeepSeek 的思考模式**默认开启且 effort=high**，对"把字幕压成摘要"这类任务
# 又慢又贵（输出 token 计费），因此这里默认关闭，需要深度推理时再调高。
LLM_THINKING = _get("LLM_THINKING", "disabled")
FINANCE_AUTO_ANALYSIS = _get("FINANCE_AUTO_ANALYSIS", "false" if CLOUD_MODE else "true").lower() in ("1", "true", "yes")

# --- 图片识别（vision）---
# 是否对新增内容里的图片做识别。默认开；没有可用 key 时会自动跳过，不会报错。
VISION_ENABLED = _get("VISION_ENABLED", "false" if CLOUD_MODE and CLOUD_TEST_MODE else "true").lower() in ("1", "true", "yes")
# 每轮最多识别几张图。图片成本很低（单图最多约 1024 tokens，约 ¥0.001），
# 但耗时是串行的，所以要有个闸门，避免一轮里堆几百张把爬取拖住。
VISION_MAX_IMAGES_PER_ROUND = _get_int("VISION_MAX_IMAGES_PER_ROUND", 20)
# 送图时往回追溯多少天内的内容（只处理新内容，不回头重刷历史）
VISION_LOOKBACK_DAYS = _get_int("VISION_LOOKBACK_DAYS", 7)
# detail 档位：留空=不发送该参数（非 DeepSeek 网关更安全）；
# DeepSeek 官方会自动用 low（长边压到 512px，省 token）
VISION_DETAIL = _get("VISION_DETAIL", "").strip()
# 单次图片识别请求的超时（秒）：读图比纯文本慢
VISION_TIMEOUT = _get_int("VISION_TIMEOUT", 90)

# --- UP master list (uid -> name) ---
# 仅作首次启动的引导种子：init_db 时把这里(或 .env UP_UIDS)的 UID 写入
# uppers 表。此后订阅名单以数据库为准，增删请在网页端「监控名单」操作；
# 这里或 .env 的改动不会重新激活已取消订阅的 UP 主。
UP_NAMES = {
    "525121722": "莫大韭菜",
    "11473291": "笨笨的韭菜",
    "3546929266952873": "擒龙先生",
    "322005137": "史诗级韭菜",
    "396958144": "七加一不怕",
    "11430504": "来去由心",
    "3546976515786791": "机构一手调研-福总",
}

# UP_UIDS: prefer .env order if present, else the dict order above
_env_uids = _get("UP_UIDS")
if _env_uids:
    UP_UIDS = [u.strip() for u in _env_uids.split(",") if u.strip()]
else:
    UP_UIDS = list(UP_NAMES.keys())


def up_name(uid: str) -> str:
    return UP_NAMES.get(str(uid), str(uid))


# --- scheduler ---
CRAWL_MORNING = _get("CRAWL_MORNING", "08:00")
CRAWL_EVENING = _get("CRAWL_EVENING", "18:00")
AUTO_CRAWL_ON_START = _get("AUTO_CRAWL_ON_START", "false").lower() in ("1", "true", "yes")

# --- logging ---
# 应用日志级别。默认 INFO：把爬取/enrich 轨迹打到 stdout，便于 journalctl 排错。
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()

# --- crawler ---
CRAWL_TIMEOUT_PER_UID = 300  # seconds (5 minutes)
# UID 之间的请求间隔。空间 feed 接口对连续请求很敏感（实测同一轮里前几个
# UID 成功、后面的陆续 -352），间隔给足可显著降低触发率；.env 可覆盖。
CRAWL_SLEEP_MIN = _get_int("CRAWL_SLEEP_MIN", 12)
CRAWL_SLEEP_MAX = _get_int("CRAWL_SLEEP_MAX", 20)
# dynamics pass uses larger spacing to dodge -352 rate-tripping on
# back-to-back feed/space calls
DYN_SLEEP_MIN = _get_int("DYN_SLEEP_MIN", 10)
DYN_SLEEP_MAX = _get_int("DYN_SLEEP_MAX", 20)

# --- summarizer ---
SUMMARY_INPUT_MAX_CHARS = _get_int("SUMMARY_INPUT_MAX_CHARS", 200000)
SUMMARY_LOOKBACK_HOURS = 24
# 视频没有 AI 摘要时（例如刚抓到、字幕还没抓完），用字幕开头一段兜底，
# 避免该视频只剩一个标题进日报
SUMMARY_SUBTITLE_FALLBACK_CHARS = _get_int("SUMMARY_SUBTITLE_FALLBACK_CHARS", 1200)
# 逐视频摘要：送入 LLM 的字幕截断长度（8万字符≈4.5小时口播，v4-flash 128k上下文可容纳）
SUMMARY_VIDEO_INPUT_MAX_CHARS = _get_int("SUMMARY_VIDEO_INPUT_MAX_CHARS", 80000)
# 字幕入库全文上限（20万字符≈8小时直播回放）
SUBTITLE_MAX_CHARS = _get_int("SUBTITLE_MAX_CHARS", 200000)

# --- Bilibili Cookie (for pure API crawler) ---
# 从 .env 读取登录态，如：BILI_SESSDATA=xxx\nBILI_JCT=xxx\nBILI_BUVID3=xxx\nBILI_DEDEUSERID=xxx
BILI_SESSDATA = _get("BILI_SESSDATA")
BILI_JCT = _get("BILI_JCT")
BILI_BUVID3 = _get("BILI_BUVID3")
BILI_BUVID4 = _get("BILI_BUVID4")
BILI_DEDEUSERID = _get("BILI_DEDEUSERID")

# 是否启用纯 API 模式（True=不用 MediaCrawler 子进程）
USE_PURE_API = _get("USE_PURE_API", "true").lower() in ("1", "true", "yes")

# --- CORS ---
CORS_ORIGINS = ["http://localhost:5173"]
