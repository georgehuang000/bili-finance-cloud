# -*- coding: utf-8 -*-
"""网页端「模型设置」的后端逻辑：预设 / 掩码读取 / 保存并热生效。

设计要点：

- **密钥不回传**。返回给前端的永远是掩码（``sk-***E768``）。前端重新提交时，
  密钥栏为空或仍是掩码，都表示"不改密钥"。
- **热生效**。保存后立刻 setattr 到 ``config`` 并丢弃 ``llm_client`` 缓存的
  客户端，无需重启服务。
- **可回退**。保存只改 ``LLM_API_KEY`` 等键，不动文件里的其它内容；把
  ``LLM_API_KEY`` 删掉并改回 ``LLM_BASE_URL`` 即可回到上一套配置。
- 写入 .env 的键走白名单，避免前端塞入任意键名。
"""
from __future__ import annotations

import config
import env_store
import llm_client

# 服务商预设。base_url 已用 /models 探测确认存在（两个路径形式 DeepSeek 都接受）。
PROVIDERS = {
    "deepseek": {
        "key": "deepseek",
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "vision": True,
        "note": "deepseek-flash = V4.1-Flash，1M 上下文，支持图片识别。",
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "dashscope": {
        "key": "dashscope",
        "label": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.8-max",
        "vision": True,
        "note": "Qwen3.8 Max：文字总结和图片识别；思考开关按百炼参数传递。",
        "key_url": "https://bailian.console.aliyun.com/",
    },
    "custom": {
        "key": "custom",
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
        "vision": None,
        "note": "任何兼容 OpenAI /chat/completions 的服务都可填（硅基流动、百炼、Ollama 等）。",
        "key_url": "",
    },
}

PROVIDER_ORDER = ["deepseek", "dashscope", "custom"]

# 允许写入 .env 的键
WRITABLE_KEYS = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "VISION_MODEL",
    "LLM_THINKING",
    "VISION_ENABLED",
    "VISION_MAX_IMAGES_PER_ROUND",
)

# 这两类键在 .env 里是字符串，但 setattr 到 config 时要转成真正的 bool/int，
# 否则 config.VISION_ENABLED 会变成 "false" 这种真值为 True 的字符串。
_BOOL_KEYS = ("VISION_ENABLED",)
_INT_KEYS = ("VISION_MAX_IMAGES_PER_ROUND",)

THINKING_VALUES = ("disabled", "low", "high", "max")

# 密钥来源：优先网页写的键名，其次手动写的 deepseek_key
_KEY_SOURCES = ("LLM_API_KEY", "deepseek_key")


def mask(value: str) -> str:
    """把密钥转成可安全展示的形式（``sk-***E768``）。绝不返回原文。"""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 10:
        return "*" * len(v)
    return f"{v[:3]}***{v[-4:]}"


def is_masked(value: str) -> bool:
    """判断前端传来的值是不是掩码（意味着"不改密钥"）。"""
    v = (value or "").strip()
    return ("***" in v) or (v != "" and set(v) == {"*"})


def detect_provider(base_url: str) -> str:
    if llm_client.is_dashscope(base_url):
        return "dashscope"
    b = (base_url or "").strip().lower().rstrip("/")
    if "deepseek.com" in b:
        return "deepseek"
    return "custom"


def key_source() -> str:
    """当前生效的密钥来自哪个 .env 键（只报键名，不报值）。"""
    env = env_store.read_env(config.ENV_PATH)
    for k in _KEY_SOURCES:
        if (env.get(k) or "").strip():
            return k
    return ""


def vision_capable():
    """当前服务商/模型是否可能支持图片识别。

    返回 True / False / None（None=自定义服务商，未知，不拦截）。
    依据两点：服务商预设里的 vision 标记，以及 DeepSeek 的已知事实
    （v4-pro 系列明确不支持图片输入，传图会被 400 拒绝）。

    经验：把图片发给不支持读图的网关会直接 HTTP 400「参数不被支持」，
    所以判定为 False 时直接拦掉，避免每轮白跑一堆失败请求。
    """
    base = (config.LLM_BASE_URL or "").lower()
    preset = PROVIDERS.get(detect_provider(base)) or {}
    model = (config.VISION_MODEL or "").lower()
    if "deepseek" in base and "pro" in model:
        return False
    return preset.get("vision")


def current() -> dict:
    """当前生效的模型配置（密钥只给掩码）。"""
    key = config.LLM_API_KEY or ""
    return {
        "configured": bool(key and config.LLM_BASE_URL and config.LLM_MODEL),
        "provider": detect_provider(config.LLM_BASE_URL),
        "base_url": config.LLM_BASE_URL,
        "model": config.LLM_MODEL,
        "vision_model": config.VISION_MODEL,
        "thinking": config.LLM_THINKING,
        "vision_enabled": bool(config.VISION_ENABLED),
        "vision_max_images": int(config.VISION_MAX_IMAGES_PER_ROUND),
        # True/False/None(未知)：当前服务商能否读图，前端据此提前提醒
        "vision_capable": vision_capable(),
        "key_masked": mask(key),
        "key_set": bool(key),
        "key_source": key_source(),
        "providers": [PROVIDERS[k] for k in (["deepseek"] if config.CLOUD_MODE else PROVIDER_ORDER)],
        "thinking_values": list(THINKING_VALUES),
        "env_path": "云端加密存储" if config.CLOUD_MODE else str(config.ENV_PATH),
    }


def find_provider(key: str) -> dict:
    return PROVIDERS.get((key or "").strip().lower(), PROVIDERS["custom"])


def apply_to_config(updates: dict) -> list:
    """把更新应用到运行中的 config 模块（下次调用即生效）。"""
    applied = []
    for k, v in updates.items():
        if k in WRITABLE_KEYS and hasattr(config, k):
            if k in _BOOL_KEYS:
                v = str(v).strip().lower() in ("1", "true", "yes", "on")
            elif k in _INT_KEYS:
                try:
                    v = int(str(v).strip())
                except (TypeError, ValueError):
                    continue
            setattr(config, k, v)
            applied.append(k)
    # 密钥/地址变了，必须丢弃已建好的客户端
    llm_client.invalidate("settings changed")
    # VISION_MODEL 默认跟随 LLM_MODEL：这里做一次同步，避免换类型后图片仍打旧模型
    if "LLM_MODEL" in updates and "VISION_MODEL" not in updates:
        if not env_store.read_env(config.ENV_PATH).get("VISION_MODEL"):
            config.VISION_MODEL = config.LLM_MODEL
    return applied


def _validate(payload: dict) -> dict:
    """校验并整理待写入的键值。抛 ValueError 表示用户输入有误。"""
    updates = {}

    base_url = (payload.get("base_url") or "").strip()
    if base_url:
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        updates["LLM_BASE_URL"] = base_url.rstrip("/")

    for field, env_key, label in (
        ("model", "LLM_MODEL", "模型名"),
        ("vision_model", "VISION_MODEL", "图片识别模型"),
    ):
        if field in payload:
            v = (payload.get(field) or "").strip()
            if v and (" " in v or "\t" in v):
                raise ValueError(f"{label}不能包含空格")
            updates[env_key] = v

    if "thinking" in payload:
        t = (payload.get("thinking") or "").strip().lower()
        if t and t not in THINKING_VALUES:
            raise ValueError(f"思考模式只能是 {'/'.join(THINKING_VALUES)}")
        updates["LLM_THINKING"] = t or "disabled"

    # 图片识别开关 + 每轮配额
    if "vision_enabled" in payload:
        on = payload.get("vision_enabled")
        updates["VISION_ENABLED"] = (
            "true" if (on is True or str(on).strip().lower() in ("1", "true", "yes", "on")) else "false"
        )
    if "vision_max_images" in payload:
        raw_n = payload.get("vision_max_images")
        try:
            n = int(str(raw_n).strip())
        except (TypeError, ValueError):
            raise ValueError("每轮图片识别数量必须是整数") from None
        if n < 0 or n > 200:
            raise ValueError("每轮图片识别数量请填 0~200 之间")
        updates["VISION_MAX_IMAGES_PER_ROUND"] = str(n)

    # 密钥：空值或掩码都表示"不改"
    if "api_key" in payload:
        raw = (payload.get("api_key") or "").strip()
        if raw and not is_masked(raw):
            if len(raw) < 8:
                raise ValueError("API Key 看起来太短了，请检查是否复制完整")
            updates["LLM_API_KEY"] = raw

    return updates


def save(payload: dict) -> dict:
    """保存设置：写回 .env -> 热更新 config -> 丢弃客户端。返回最新配置。"""
    provider_key = (payload.get("provider") or "").strip().lower()
    provider_changed = False

    # 切服务商：没显式填 base_url/模型时，用该预设的默认值
    if provider_key and provider_key != "custom":
        preset = find_provider(provider_key)
        provider_changed = detect_provider(config.LLM_BASE_URL) != provider_key
        payload = dict(payload)
        if not (payload.get("base_url") or "").strip():
            payload["base_url"] = preset["base_url"]
        if not (payload.get("model") or "").strip():
            payload["model"] = preset["model"]

    updates = _validate(payload)
    if not updates:
        raise ValueError("没有需要保存的改动")

    written = env_store.save_env(config.ENV_PATH, updates)
    apply_to_config(updates)

    result = current()
    result["written"] = written

    # 切了服务商但没给新 key —— 旧 key 很可能不被新服务商接受，明确提示
    if provider_changed and "LLM_API_KEY" not in updates:
        result["warning"] = (
            "已切换服务商，但未填写新的 API Key。旧 Key 通常不被新服务商接受，"
            "请填好 Key 后点「测试连接」确认。"
        )
    return result
